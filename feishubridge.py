#!/usr/bin/env python3
"""feishubridge — 飞书 ↔ 任意 AI CLI 纯消息桥。

与 wechatbridge 同构：inbox/outbox + 本地 HTTP 接口，零业务逻辑。
使用飞书「自建应用 + 长连接(WebSocket)」模式收消息，本地可跑，无需公网。

前置准备（在 https://open.feishu.cn/app 创建自建应用）：
  1. 开通「机器人」能力
  2. 事件订阅 → 选择「使用长连接接收事件」→ 订阅 im.message.receive_v1
  3. 权限：im:message, im:message:send_as_bot, im:resource（图片/文件）
  4. 拿到 App ID / App Secret，填到 data/credential.json

依赖：pip install lark-oapi requests

用法：
  python feishubridge.py            常驻运行（长连接收消息 + HTTP 接口）
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
CREDENTIAL_FILE = os.path.join(DATA_DIR, "credential.json")
INBOX = os.path.join(BASE_DIR, "inbox.log")
OUTBOX = os.path.join(BASE_DIR, "outbox.log")
SESSION_LINK = os.path.join(BASE_DIR, "session.link")

HTTP_PORT = 7655  # 飞书桥用 7655，与微信桥 7654 区分

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)


def load_credential():
    if not os.path.exists(CREDENTIAL_FILE):
        return None
    try:
        with open(CREDENTIAL_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------- log + HTTP（与 wechatbridge 完全一致）----------

def append_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


STATE = {"inbox": [], "inbox_seq": 0, "outbox": [], "lock": threading.Lock(),
         "user": None, "send_fn": None}  # send_fn 由主循环注入（需 app client）


class _Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            self._json(200, {"ok": True, "user": STATE["user"], "platform": "feishu"})
            return
        if u.path == "/inbox":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            with STATE["lock"]:
                msgs = [m for m in STATE["inbox"] if m["seq"] > since]
            self._json(200, {"msgs": msgs, "last_seq": STATE["inbox_seq"]})
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
            with STATE["lock"]:
                STATE["outbox"].append({"ts": int(time.time()), "text": text})
            self._json(200, {"ok": True})
            return
        self._json(404, {"err": "not found"})


def start_http_server():
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="feishu-http").start()
    print(f"[http] 接口服务监听 http://127.0.0.1:{HTTP_PORT} (/inbox /outbox /health)")


def _load_linked_user():
    if os.path.exists(SESSION_LINK):
        try:
            with open(SESSION_LINK, encoding="utf-8") as f:
                return f.read().strip() or None
        except Exception:
            pass
    return None


def run():
    cred = load_credential()
    if not cred or not cred.get("app_id") or not cred.get("app_secret"):
        print("[run] 无凭证。请在 https://open.feishu.cn/app 创建自建应用，")
        print("      开通机器人 + 长连接事件订阅(im.message.receive_v1)，")
        print(f"      把 App ID / App Secret 填到 {CREDENTIAL_FILE}：")
        print('      {"app_id":"cli_xxx","app_secret":"xxx"}')
        return
    try:
        import lark_oapi as lark
        from lark_oapi import ws
        from lark_oapi.api.im.v1 import (
            P2ImMessageReceiveV1, CreateMessageRequest, CreateMessageRequestBody,
            CreateImageRequest, CreateImageRequestBody, CreateFileRequest, CreateFileRequestBody,
        )
    except ImportError:
        print("[run] 缺依赖：pip install lark-oapi")
        return

    start_http_server()
    STATE["user"] = _load_linked_user()

    # 构建 app client（发消息用）
    app = lark.Client.builder().app_id(cred["app_id"]).app_secret(cred["app_secret"]).build()

    def send_text(to_open_id, text):
        """通过飞书 API 发文本消息给用户。"""
        req = (CreateMessageRequest.builder()
               .receive_id_type("open_id")
               .request_body(CreateMessageRequestBody.builder()
                              .receive_id(to_open_id)
                              .msg_type("text")
                              .content(json.dumps({"text": text})).build()).build())
        app.im.v1.message.create(req)

    def on_message(ctx, conf, event: P2ImMessageReceiveV1):
        try:
            msg = event.event.message
            sender = event.event.sender.sender_id.open_id
            chat_type = msg.chat_type  # p2p / group
            # 只处理单聊；群聊需 @ 机器人，这里简化
            if chat_type != "p2p":
                return
            if not STATE["user"]:
                with open(SESSION_LINK, "w", encoding="utf-8") as f:
                    f.write(sender)
                STATE["user"] = sender
                print(f"[link] 关联飞书用户: {sender}")
            mtype = msg.message_type  # text / image / file
            content = json.loads(msg.content) if msg.content else {}
            entry = {"ts": int(time.time()), "from": sender, "type": "text"}
            if mtype == "text":
                entry["text"] = content.get("text", "").strip()
                if not entry["text"]:
                    return
            elif mtype == "image":
                entry = {"type": "image", "image_key": content.get("image_key", "")}
            elif mtype == "file":
                entry = {"type": "file", "file_key": content.get("file_key", ""),
                         "name": content.get("file_name", "")}
            else:
                entry = {"type": mtype, "raw": content}
            append_line(INBOX, entry)
            with STATE["lock"]:
                STATE["inbox_seq"] += 1
                e2 = dict(entry); e2["seq"] = STATE["inbox_seq"]
                STATE["inbox"].append(e2)
            print(f"[inbound] {entry.get('type')}: {entry.get('text','')[:60]}")
        except Exception as e:
            print(f"[msg-error] {e}")

    # 长连接客户端
    cli = ws.Client(lark.WSClientCliParam.builder()
                    .app_id(cred["app_id"]).app_secret(cred["app_secret"])
                    .event_handler(on_message).build())

    # 后台线程：消费 outbox → 发飞书消息
    def outbox_loop():
        while True:
            to_user = STATE["user"] or _load_linked_user()
            items = []
            with STATE["lock"]:
                if STATE["outbox"]:
                    items = STATE["outbox"][:]; STATE["outbox"].clear()
            for it in items:
                if not to_user:
                    break
                try:
                    send_text(to_user, it["text"])
                    print(f"[outbound] {it['text'][:60]}")
                except Exception as e:
                    print(f"[send-fail] {e}")
            time.sleep(1)
    threading.Thread(target=outbox_loop, daemon=True, name="feishu-outbox").start()

    print(f"[run] 飞书长连接启动，监听中… (known_user={STATE['user']})")
    cli.start()  # 阻塞，自动重连


if __name__ == "__main__":
    run()
