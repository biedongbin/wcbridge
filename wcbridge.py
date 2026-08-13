#!/usr/bin/env python3
"""wcbridge — 微信 ↔ Claude Code 纯消息桥。

零业务逻辑，纯通道。两个 log 文件解耦微信与 Claude Code：
  inbox.log   微信消息流入（CC 轮询读，当用户指令执行）
  outbox.log  CC 回复流出（本脚本轮询读 → 发微信）

用法：
  python wcbridge.py --login          扫码登录（凭证落 data/credential.json）
  python wcbridge.py                  常驻运行（长轮询微信 + 读 outbox 回推）
"""
import argparse
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CREDENTIAL_FILE = os.path.join(DATA_DIR, "credential.json")
INBOX = os.path.join(BASE_DIR, "inbox.log")
OUTBOX = os.path.join(BASE_DIR, "outbox.log")
SESSION_LINK = os.path.join(BASE_DIR, "session.link")

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_BOT_TYPE = "3"
LONG_POLL_TIMEOUT = 35
API_TIMEOUT = 35
LOGIN_DEADLINE = 480
MAX_QR_REFRESH = 3

# HTTP 服务端口（CC 通过此端口秒级读写，替代轮询 log）
HTTP_PORT = 7654

os.makedirs(DATA_DIR, exist_ok=True)


# ---------- iLink 协议（复刻自 wechat-bridge，已实测可用）----------

def _headers(token=None):
    import base64, random
    uin = str(random.randint(0, 2**31 - 1))
    h = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
         "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode()}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def get_qr_code():
    r = requests.get(f"{ILINK_BASE_URL}/ilink/bot/get_bot_qrcode",
                     params={"bot_type": ILINK_BOT_TYPE}, timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_qr_code_status(qrcode):
    r = requests.get(f"{ILINK_BASE_URL}/ilink/bot/get_qrcode_status", params={"qrcode": qrcode},
                     headers={"iLink-App-ClientVersion": "1"}, timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_updates(token, base_url, buffer=""):
    r = requests.post(f"{base_url}/ilink/bot/getupdates", json={"get_updates_buf": buffer or ""},
                      headers=_headers(token), timeout=LONG_POLL_TIMEOUT + 10)
    r.raise_for_status()
    return r.json()


def send_text(token, base_url, to_user, text, context_token=None):
    client_id = f"bot-{int(time.time()*1000)}-{uuid.uuid4().hex[:12]}"
    msg = {"from_user_id": "", "to_user_id": to_user, "client_id": client_id,
           "message_type": 2, "message_state": 2,
           "item_list": [{"type": 1, "text_item": {"text": text}}]}
    if context_token:
        msg["context_token"] = context_token
    requests.post(f"{base_url}/ilink/bot/sendmessage", json={"msg": msg},
                  headers=_headers(token), timeout=API_TIMEOUT).raise_for_status()


# ---------- 凭证 ----------

def load_credential():
    if not os.path.exists(CREDENTIAL_FILE):
        return None
    try:
        with open(CREDENTIAL_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_credential(cred):
    with open(CREDENTIAL_FILE, "w", encoding="utf-8") as f:
        json.dump(cred, f, ensure_ascii=False, indent=2)


# ---------- 登录 ----------

def do_login():
    qr = get_qr_code()
    # 二维码内容（可扫码）+ 图片地址
    print(f"\n[QR-CONTENT] {qr.get('qrcode_img_content', '')}")
    print(f"[QR-IMAGE] {qr.get('qrcode_img_url', qr.get('qrcode', ''))}\n")
    deadline = time.time() + LOGIN_DEADLINE
    refresh = 0
    qrcode = qr.get("qrcode")
    while time.time() < deadline:
        try:
            st = get_qr_code_status(qrcode)
        except Exception as e:
            print(f"[status-timeout] {e}")
            time.sleep(1)
            continue
        s = st.get("status")
        if s == "scaned":
            print("[status] 已扫码，请手机确认…")
        elif s == "expired":
            refresh += 1
            if refresh >= MAX_QR_REFRESH:
                print("[login] 二维码多次过期，退出")
                return
            print(f"[status] 过期，刷新 ({refresh}/{MAX_QR_REFRESH})")
            qr = get_qr_code()
            print(f"[QR-CONTENT] {qr.get('qrcode_img_content', '')}")
            qrcode = qr.get("qrcode")
        elif s == "confirmed":
            cred = {"bot_token": st.get("bot_token"), "base_url": st.get("baseurl") or ILINK_BASE_URL,
                    "ilink_bot_id": st.get("ilink_bot_id"), "ilink_user_id": st.get("ilink_user_id")}
            if not cred["bot_token"]:
                print("[login] 确认但无 token，退出")
                return
            save_credential(cred)
            print(f"[login] ✅ 登录成功，凭证已存 {CREDENTIAL_FILE}")
            return
        time.sleep(1)
    print("[login] 超时")


# ---------- log 文件解耦 ----------

def append_line(path, line):
    """追加一行 JSON 到 log（带换行，原子写）。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def read_new_lines(path, pos):
    """从 pos 读新增行，返回 (lines, new_pos)。"""
    if not os.path.exists(path):
        return [], 0
    sz = os.path.getsize(path)
    if sz < pos:  # 文件被截断/轮转
        pos = 0
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        f.seek(pos)
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                lines.append(json.loads(ln))
            except Exception:
                pass
        return lines, f.tell()
    return lines, pos


# ---------- HTTP 接口（供 CC 秒级读写，替代 log 轮询）----------
# 运行时状态：HTTP handler 与主循环通过此 dict 交互
STATE = {
    "inbox": [],        # 待 CC 读取的消息队列 [{ts,from,text}]
    "inbox_seq": 0,     # 单调递增序号
    "outbox": [],       # 待发送队列 [{ts,text}]（HTTP POST 入队，主循环消费）
    "lock": threading.Lock(),
    "user": None,       # 已关联微信用户
}


def _enqueue_outbox(text):
    """CC POST /outbox 调用：入队一条待发消息。"""
    with STATE["lock"]:
        STATE["outbox"].append({"ts": int(time.time()), "text": text})
    return True


class _Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass  # 静音默认访问日志

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            self._json(200, {"ok": True, "user": STATE["user"]})
            return
        if u.path == "/inbox":
            # CC 传 since=N，返回 seq>N 的所有消息
            q = parse_qs(u.query)
            since = int(q.get("since", ["0"])[0])
            with STATE["lock"]:
                msgs = [m for m in STATE["inbox"] if m["seq"] > since]
            self._json(200, {"msgs": msgs, "last_seq": STATE["inbox_seq"]})
            return
        if u.path == "/outbox/peek":
            # 调试用：看待发队列
            with STATE["lock"]:
                self._json(200, {"pending": list(STATE["outbox"])})
            return
        self._json(404, {"err": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/outbox":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                data = json.loads(raw)
            except Exception:
                self._json(400, {"err": "bad json"})
                return
            text = (data.get("text") or "").strip()
            if not text:
                self._json(400, {"err": "empty text"})
                return
            _enqueue_outbox(text)
            self._json(200, {"ok": True})
            return
        self._json(404, {"err": "not found"})


def start_http_server():
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="wcbridge-http").start()
    print(f"[http] 接口服务监听 http://127.0.0.1:{HTTP_PORT} (/inbox /outbox /health)")


# ---------- 主循环 ----------

def _load_linked_user():
    """每次读 session.link（启动后也可能被外部写入）。"""
    if os.path.exists(SESSION_LINK):
        try:
            with open(SESSION_LINK, encoding="utf-8") as f:
                return f.read().strip() or None
        except Exception:
            pass
    return None


def run():
    cred = load_credential()
    if not cred:
        print("[run] 无凭证，请先 --login")
        return
    token = cred["bot_token"]
    base_url = cred.get("base_url", ILINK_BASE_URL)
    cursor = ""
    seen = set()
    outbox_pos = 0
    context_tokens = {}
    # 启动 HTTP 接口服务
    start_http_server()
    STATE["user"] = _load_linked_user()
    print(f"[run] 启动，监听中… (known_user={STATE['user']})")
    while True:
        # 当前关联用户（每轮动态读，支持外部写入 session.link）
        to_user = STATE["user"] = _load_linked_user() or STATE["user"]
        # 1) 长轮询微信 → inbox（同时写 log 和内存 STATE，供 CC 两种方式读）
        try:
            resp = get_updates(token, base_url, cursor)
            if resp.get("ret") not in (None, 0):
                print(f"[poll] ret={resp.get('ret')} err={resp.get('errmsg')}")
                time.sleep(3)
                continue
            if resp.get("get_updates_buf"):
                cursor = resp["get_updates_buf"]
            for m in resp.get("msgs", []) or []:
                if m.get("message_type") != 1:  # 仅 USER 消息
                    continue
                mid = m.get("message_id")
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                uid = m.get("from_user_id")
                if not uid:
                    continue
                if m.get("context_token"):
                    context_tokens[uid] = m["context_token"]
                # 首次互动 → 记录关联
                if not to_user:
                    with open(SESSION_LINK, "w", encoding="utf-8") as f:
                        f.write(uid)
                    to_user = uid
                    STATE["user"] = uid
                    print(f"[link] 关联微信用户: {uid}")
                # 提取文本
                text = ""
                for it in m.get("item_list") or []:
                    if it.get("type") == 1 and it.get("text_item"):
                        text = it["text_item"].get("text", "")
                        break
                if not text:
                    continue
                entry = {"ts": int(time.time()), "from": uid, "text": text}
                append_line(INBOX, entry)  # log 方式（兼容）
                with STATE["lock"]:        # 内存方式（HTTP 秒级读）
                    STATE["inbox_seq"] += 1
                    entry2 = dict(entry)
                    entry2["seq"] = STATE["inbox_seq"]
                    STATE["inbox"].append(entry2)
                print(f"[inbound] {text[:60]}")
        except Exception as e:
            print(f"[poll-error] {e}")
            time.sleep(3)
        # 2) 读 outbox（log 新行 + HTTP POST 内存队列）→ 发微信
        to_send = []
        try:
            lines, outbox_pos = read_new_lines(OUTBOX, outbox_pos)
            to_send = [ln.get("text", "") for ln in lines if ln.get("text")]
        except Exception as e:
            print(f"[outbox-log-error] {e}")
        with STATE["lock"]:
            if STATE["outbox"]:
                to_send.extend(m["text"] for m in STATE["outbox"])
                STATE["outbox"].clear()
        for text in to_send:
            if not to_user:
                print("[send-skip] 无关联用户，丢弃")
                break
            try:
                send_text(token, base_url, to_user, text, context_tokens.get(to_user))
                print(f"[outbound] {text[:60]}")
            except Exception as e:
                print(f"[send-fail] {e}")


def main():
    ap = argparse.ArgumentParser(description="wcbridge 微信↔Claude Code 纯消息桥")
    ap.add_argument("--login", action="store_true", help="扫码登录")
    args = ap.parse_args()
    if args.login:
        do_login()
    else:
        run()


if __name__ == "__main__":
    main()
