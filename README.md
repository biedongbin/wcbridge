# wcbridge

**A minimal bridge between WeChat and any AI CLI.**

WeChat → `wcbridge` → your agent (Claude Code, opencode, codex, or any tool that can `curl` or read a file), and back. A pure message channel — supports **text, images, and files**. No business logic, no database, dependencies are just `requests` + `cryptography`.

把微信与任意 AI CLI 打通的极简桥。微信 → `wcbridge` → 你的 agent（Claude Code / opencode / codex，或任何能 `curl` 或读文件的工具），再原路返回。纯消息通道，**支持文本、图片、文件**，零业务逻辑，零数据库，依赖仅 `requests` + `cryptography`。

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

### 消息类型 / Message types

inbox 里的每条消息带 `type` 字段：

| type | 含义 | 额外字段 |
|---|---|---|
| `text` | 文本消息 | `text` |
| `image` | 图片（已下载解密落盘） | `path`, `ext`, `size` |
| `file` | 文件（已下载解密落盘） | `path`, `name`, `ext`, `size` |

图片和文件通过 iLink 的 AES-128-ECB 加密媒体通道下载、解密后落到 `data/media/`，inbox 只返回本地路径，agent 直接读文件即可。

Images and files are downloaded via iLink's AES-128-ECB encrypted media channel, decrypted, and saved to `data/media/`. The inbox returns a local `path`; the agent just reads the file.

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

## 适配任意 AI CLI / Works With Any AI CLI

wcbridge 是**协议中立**的：只要你的工具能 `curl` 或读文件，就能接入。下面是几种常见 AI CLI 的接法。

wcbridge is **protocol-agnostic**: anything that can `curl` or read a file can hook in.

### 通用接入模式 / Universal pattern

任何 agent 只需做两件事：
1. **拉指令**：定时 `GET /inbox?since=<last_seq>`（或 tail `inbox.log`）
2. **回结果**：`POST /outbox {"text":"..."}`（或 append `outbox.log`）

Any agent does just two things: pull instructions, push results.

### Claude Code

用 `CronCreate` 每分钟调度一个值守 prompt（或写个 hook）：

```bash
# 拉新消息
curl -s "http://127.0.0.1:7654/inbox?since=$(cat /tmp/wc_last_seq)"
# 回复
curl -X POST http://127.0.0.1:7654/outbox -H 'Content-Type: application/json' -d '{"text":"done"}'
```

### opencode / codex / 其它 CLI

这些 CLI 没有内置定时器，用一个 cron 或后台脚本桥接：

```bash
# poll.sh — cron 每分钟跑，把微信消息喂给 opencode/codex 的 stdin 或会话
LAST=$(cat /tmp/wc_last_seq 2>/dev/null || echo 0)
RESP=$(curl -s "http://127.0.0.1:7654/inbox?since=$LAST")
SEQ=$(echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['last_seq'])")
echo "$RESP" | python3 -c "import sys,json;[print(m['text']) for m in json.load(sys.stdin)['msgs']]" \
  | opencode chat   # 或: codex exec -  / 任意 CLI
echo "$SEQ" > /tmp/wc_last_seq
```

或更简单：让 AI CLI 直接读 `inbox.log`、写 `outbox.log`，连 curl 都不用。

### 桌面端 / GUI 应用

任何能发 HTTP 请求的桌面应用（Electron / Tauri / 甚至浏览器 fetch 到 localhost）都能用：

```js
// 拉消息
const r = await fetch("http://127.0.0.1:7654/inbox?since=" + lastSeq);
// 发消息
await fetch("http://127.0.0.1:7654/outbox", {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({text: "Hello"})
});
```

### MCP 集成 / MCP

wcbridge 的 HTTP 接口天然适配 MCP——把它包成一个 MCP tool（`get_inbox` / `send_reply`）即可被任何 MCP 客户端调用。示例工具定义：

```json
{"name": "wechat_inbox", "description": "拉取微信新消息",
 "inputSchema": {"type":"object","properties":{"since":{"type":"number","default":0}}}}
```

实现 = 转发到 `GET /inbox?since=N`。

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
├── data/              # 凭证 + 媒体（已 gitignore）/ credentials + media (ignored)
│   ├── credential.json
│   └── media/         # 下载的图片/文件 / downloaded images & files
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
