# wcbridge

**A minimal bridge between WeChat and Claude Code.**

WeChat → `wcbridge` → Claude Code, and back. A pure message channel — no business logic, no database, no dependencies beyond `requests`.

一个把微信与 Claude Code 打通的极简桥。微信 → `wcbridge` → Claude Code，再原路返回。纯消息通道，零业务逻辑，零数据库，依赖只有 `requests`。

---

## 设计理念 / Design Philosophy

**极简、纯通道、文件解耦。**

- **Pure channel** — wcbridge does one thing: move text between WeChat and your local tools. No skills, no business rules, no persistence beyond login.
- **Dual interface** — a tiny local HTTP server for second-latency reads/writes, plus plain log files for maximum compatibility.
- **Single file** — the whole bridge is one ~300-line Python file you can read end to end.
- **No external state** — the WeChat identity of the person you link to is written to one `session.link` file. That's it.

**Minimal, pure-channel, file-decoupled.**

- **纯通道** — wcbridge 只做一件事：在微信和你的本地工具之间搬运文本。没有 skill，没有业务规则，除了登录凭证不持久化任何东西。
- **双通道** — 一个微型本地 HTTP 服务提供秒级读写，外加纯 log 文件保证最大兼容。
- **单文件** — 整个桥就是一个约 300 行的 Python 文件，能从头读到尾。
- **无外部状态** — 你关联的微信身份写进一个 `session.link` 文件，仅此而已。

---

## 技术方案 / How It Works

```
WeChat (手机微信)
   │  iLink long-poll (35s)
   ▼
wcbridge (本机常驻)
   │  ├─ inbox.log   +  in-memory queue  →  HTTP GET /inbox?since=N
   │  └─ outbox.log  +  in-memory queue  ←  HTTP POST /outbox {text}
   ▼
Claude Code (或任何本地脚本)
```

| 环节 | 方式 | 延迟 |
|---|---|---|
| 微信 → wcbridge | iLink 协议长轮询（有消息即返回，35s 超时） | 实时 |
| wcbridge → 你的工具 | 本地 HTTP（`GET /inbox?since=N`）或读 `inbox.log` | 秒级 |
| 你的工具 → 微信 | 本地 HTTP（`POST /outbox {"text":"..."}`）或写 `outbox.log` | 秒级 |

**三个 HTTP 接口 / Three HTTP endpoints** (`http://127.0.0.1:7654`):

- `GET /health` — 存活检查 / liveness
- `GET /inbox?since=N` — 拉取序号 > N 的新消息 / pull messages newer than N
- `POST /outbox` — body `{"text":"..."}` 发一条消息到微信 / send a message to WeChat

> wcbridge uses the iLink bot protocol (same login flow as a WeChat bot). You scan a QR code once, the credential is saved locally, and the bridge long-polls for incoming messages.

> wcbridge 使用 iLink bot 协议（与微信机器人相同的登录流程）。扫码一次，凭证存本地，之后长轮询拉取消息。

---

## 使用场景 / Use Cases

- **Remote-control Claude Code from your phone** — leave your desk, send a task via WeChat, Claude Code picks it up and replies. This is what it was built for.
- **WeChat ↔ any local script** — a generic bridge: anything that can read a file or call HTTP can talk to WeChat.
- **Lightweight personal WeChat bot** — when a full bot framework is overkill.

- **手机远程驱动 Claude Code** — 离开电脑，微信发个任务，Claude Code 接收并回复。这正是它的诞生场景。
- **微信 ↔ 任意本地脚本** — 通用桥：任何能读文件或调 HTTP 的程序都能和微信对话。
- **轻量个人微信 bot** — 当完整 bot 框架太重时。

---

## 从零开始 / Getting Started

### 1. 环境要求 / Prerequisites

- Python 3.9+
- A WeChat account (to scan the QR code)
- 一部能扫码的微信

### 2. 克隆 / Clone

```bash
git clone https://github.com/<your-user>/wcbridge.git
cd wcbridge
```

### 3. 安装依赖 / Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # just `requests`
```

### 4. 登录（扫码）/ Log in (scan QR)

```bash
python wcbridge.py --login
```

Prints a QR code + URL. Scan with WeChat, confirm on your phone. Credential saved to `data/credential.json`.

打印二维码 + 地址，微信扫码，手机确认。凭证存到 `data/credential.json`。

### 5. 启动桥 / Run

```bash
python wcbridge.py
```

You'll see `[run] 启动，监听中…`. The first message you send from WeChat links your identity (`session.link`).

看到 `[run] 启动，监听中…`。你从微信发的第一条消息会建立关联（写入 `session.link`）。

### 6. 收发消息 / Send & receive

From another terminal (this simulates what Claude Code does):

```bash
# Check liveness
curl http://127.0.0.1:7654/health

# Pull new WeChat messages
curl "http://127.0.0.1:7654/inbox?since=0"

# Send a reply back to WeChat
curl -X POST http://127.0.0.1:7654/outbox \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello from Claude Code"}'
```

---

## 项目结构 / Project Layout

```
wcbridge/
├── wcbridge.py        # 全部代码 / the whole bridge
├── requirements.txt   # requests
├── LICENSE            # MIT
├── README.md          # 本文件 / this file
├── .gitignore
├── data/              # 凭证（已 gitignore）/ credentials (ignored)
│   └── credential.json
├── inbox.log          # 微信→本地（已 gitignore）/ inbound (ignored)
├── outbox.log         # 本地→微信（已 gitignore）/ outbound (ignored)
└── session.link       # 关联的微信身份（已 gitignore）/ linked user (ignored)
```

---

## 安全提示 / Security Notes

- `data/credential.json` contains your WeChat bot token — **never commit it** (already in `.gitignore`).
- `inbox.log` / `outbox.log` contain message content — also ignored.
- The HTTP server binds to `127.0.0.1` only (localhost), not exposed to the network.

- `data/credential.json` 含微信 bot token —— **绝不要提交**（已在 `.gitignore`）。
- `inbox.log` / `outbox.log` 含消息内容 —— 同样已忽略。
- HTTP 服务只绑定 `127.0.0.1`（本机），不对外网暴露。

---

## License

[MIT](LICENSE)
