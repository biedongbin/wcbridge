#!/usr/bin/env python3
"""dingbridge — 钉钉 ↔ 任意 AI CLI 纯消息桥。

与 wechatbridge/feishubridge 同构：inbox/outbox + 本地 HTTP 接口，零业务逻辑。
使用钉钉「企业内部应用 + Stream(WebSocket)」模式收消息，本地可跑，无需公网。

前置准备（在 https://open-dev.dingtalk.com 创建企业内部应用）：
  1. 开通「机器人」能力，配置消息推送模式 = Stream 模式
  2. 事件订阅 → 订阅「聊天消息」，权限：企业内机器人发送消息
  3. 拿到 App Key / App Secret，填到 data/credential.json

依赖：pip install dingtalk-stream requests

用法：
  python dingbridge.py            常驻运行（Stream 收消息 + HTTP 接口）
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

HTTP_PORT = 7656  # 钉钉桥用 7656

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


def append_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


STATE = {"inbox": [], "inbox_seq": 0, "outbox": [], "lock": threading.Lock(),
         "user": None}


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
            self._json(200, {"ok": True, "user": STATE["user"], "platform": "dingtalk"})
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
    threading.Thread(target=srv.serve_forever, daemon=True, name="ding-http").start()
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
    if not cred or not cred.get("app_key") or not cred.get("app_secret"):
        print("[run] 无凭证。请在 https://open-dev.dingtalk.com 创建企业内部应用，")
        print("      开通机器人 + Stream 模式消息推送，")
        print(f"      把 App Key / App Secret 填到 {CREDENTIAL_FILE}：")
        print('      {"app_key":"dingxxx","app_secret":"xxx"}')
        return
    try:
        import dingtalk_stream
        from dingtalk_stream import DingTalkStreamClient, ChatbotHandler, AckMessageStatus
        from dingtalk_stream.chatbot import ChatbotMessage
    except ImportError:
        print("[run] 缺依赖：pip install dingtalk-stream")
        return

    start_http_server()
    STATE["user"] = _load_linked_user()

    # 钉钉 access_token 用于主动发消息（机器人回调里也能直接 reply）
    import requests
    DINGTALK_API = "https://api.dingtalk.com"
    _token_cache = {"token": None, "expire": 0}

    def get_access_token():
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expire"] - 60:
            return _token_cache["token"]
        r = requests.post(f"{DINGTALK_API}/v1.0/oauth2/accessToken", json={
            "appKey": cred["app_key"], "appSecret": cred["app_secret"]}, timeout=10)
        r.raise_for_status()
        d = r.json()
        _token_cache["token"] = d["accessToken"]
        _token_cache["expire"] = now + d.get("expireIn", 7200)
        return _token_cache["token"]

    def send_text(conversation_id, to_open_id, text):
        """通过钉钉机器人接口发消息（orgGroupSendMsg）。"""
        token = get_access_token()
        r = requests.post(
            f"{DINGTALK_API}/v1.0/robot/oToMessages/batchSend",
            headers={"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"},
            json={"robotCode": cred["app_key"], "userIds": [to_open_id],
                  "msgKey": "sampleText", "msgParam": json.dumps({"content": text})},
            timeout=10)
        r.raise_for_status()

    # 用全局记录最近一条消息的发件人 + conversation，供 outbox 回复
    LAST = {"open_id": None, "conversation": None}

    class _Handler2(ChatbotHandler):
        async def process(self, callback: dingtalk_stream.CallbackMessage):
            try:
                msg = ChatbotMessage.from_dict(callback.data)
                open_id = msg.sender_staff_id or msg.sender_id
                if not STATE["user"]:
                    with open(SESSION_LINK, "w", encoding="utf-8") as f:
                        f.write(open_id)
                    STATE["user"] = open_id
                    print(f"[link] 关联钉钉用户: {open_id}")
                LAST["open_id"] = open_id
                entry = {"ts": int(time.time()), "from": open_id, "type": "text",
                         "text": msg.text.content if msg.text and msg.text.content else "",
                         "conversation": msg.conversation_id}
                append_line(INBOX, entry)
                with STATE["lock"]:
                    STATE["inbox_seq"] += 1
                    e2 = dict(entry); e2["seq"] = STATE["inbox_seq"]
                    STATE["inbox"].append(e2)
                print(f"[inbound] text: {entry['text'][:60]}")
                return self.ACK
            except Exception as e:
                print(f"[msg-error] {e}")
                return self.ACK

    # outbox 消费
    def outbox_loop():
        while True:
            items = []
            with STATE["lock"]:
                if STATE["outbox"]:
                    items = STATE["outbox"][:]; STATE["outbox"].clear()
            for it in items:
                oid = LAST["open_id"] or STATE["user"]
                if not oid:
                    break
                try:
                    send_text(None, oid, it["text"])
                    print(f"[outbound] {it['text'][:60]}")
                except Exception as e:
                    print(f"[send-fail] {e}")
            time.sleep(1)
    threading.Thread(target=outbox_loop, daemon=True, name="ding-outbox").start()

    print(f"[run] 钉钉 Stream 启动，监听中… (known_user={STATE['user']})")
    client = DingTalkStreamClient(cred["app_key"], cred["app_secret"])
    client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, _Handler2())
    client.start_forever()


if __name__ == "__main__":
    run()
