# wcbridge

[English](README.md) | **[中文](README.zh-CN.md)**

一个把微信与任意 AI CLI 打通的极简桥。

微信 → `wcbridge` → 你的 agent（Claude Code / opencode / codex，或任何能 `curl` 或读文件的工具），再原路返回。纯消息通道，**支持文本、图片、文件**，零业务逻辑，零数据库，依赖仅 `requests` + `cryptography`。

---

## 设计理念

**极简、纯通道、文件解耦。**

- **纯通道** — wcbridge 只做一件事：在微信和你的本地工具之间搬运文本、图片、文件。没有 skill，没有业务规则，除了登录凭证不持久化任何东西。
- **双通道** — 一个微型本地 HTTP 服务提供秒级读写，外加纯 log 文件保证最大兼容。
- **单文件** — 整个桥就是一个可读的 Python 文件。
- **无外部状态** — 你关联的微信身份写进一个 `session.link` 文件，仅此而已。

## 技术方案

```
微信 (手机)
   │  iLink 长轮询 (35s)
   ▼
wcbridge (本机常驻)
   │  ├─ inbox.log   +  内存队列  →  HTTP GET /inbox?since=N
   │  └─ outbox.log  +  内存队列  ←  HTTP POST /outbox {text}
   ▼
Claude Code / opencode / codex / 任意本地工具
```

| 环节 | 方式 | 延迟 |
|---|---|---|
| 微信 → wcbridge | iLink 长轮询（有消息即返回，35s 超时） | 实时 |
| wcbridge → 你的工具 | 本地 HTTP（`GET /inbox?since=N`）或读 `inbox.log` | 秒级 |
| 你的工具 → 微信 | 本地 HTTP（`POST /outbox {"text":"..."}`）或写 `outbox.log` | 秒级 |

**三个 HTTP 接口**（`http://127.0.0.1:7654`）：

- `GET /health` — 存活检查
- `GET /inbox?since=N` — 拉取序号 > N 的新消息
- `POST /outbox` — body `{"text":"..."}` 发一条消息到微信

### 消息类型

inbox 里的每条消息带 `type` 字段：

| type | 含义 | 额外字段 |
|---|---|---|
| `text` | 文本消息 | `text` |
| `image` | 图片（已下载解密落盘） | `path`, `ext`, `size` |
| `file` | 文件（已下载解密落盘） | `path`, `name`, `ext`, `size` |

图片和文件通过 iLink 的 AES-128-ECB 加密媒体通道下载、解密后落到 `data/media/`。inbox 只返回本地路径，agent 直接读文件即可。一条微信消息含多个 item（文本+图片+文件混合）会被拆成多条 inbox 条目。

> wcbridge 使用 iLink bot 协议。扫码一次，凭证存本地，之后长轮询拉取消息。

## 使用场景

- **手机远程驱动 Claude Code** — 离开电脑，微信发个任务，Claude Code 接收并回复。这正是它的诞生场景。
- **微信 ↔ 任意本地脚本** — 通用桥：任何能读文件或调 HTTP 的程序都能和微信对话。
- **轻量个人微信 bot** — 当完整 bot 框架太重时。

---

## 适配任意 AI CLI

wcbridge 是**协议中立**的：任何能 `curl` 或读文件的工具都能接入。

### 通用接入模式

任何 agent 只需做两件事：拉指令、回结果。

1. **拉**：定时 `GET /inbox?since=<last_seq>`（或 `tail -f inbox.log`）
2. **回**：`POST /outbox {"text":"..."}`（或 append `outbox.log`）

### Claude Code

用 `CronCreate` 每分钟调度一个值守 prompt（或写 hook）：

```bash
curl -s "http://127.0.0.1:7654/inbox?since=$(cat /tmp/wc_last_seq)"
curl -X POST http://127.0.0.1:7654/outbox -H 'Content-Type: application/json' -d '{"text":"完成"}'
```

### opencode / codex / 其它 CLI

这些 CLI 没有内置定时器，用 cron 或后台脚本桥接：

```bash
LAST=$(cat /tmp/wc_last_seq 2>/dev/null || echo 0)
RESP=$(curl -s "http://127.0.0.1:7654/inbox?since=$LAST")
echo "$RESP" | python3 -c "import sys,json;[print(m['text']) for m in json.load(sys.stdin)['msgs']]" \
  | opencode chat   # 或: codex exec -  / 任意 CLI
echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['last_seq'])" > /tmp/wc_last_seq
```

或最简单：让 CLI 直接读 `inbox.log`、写 `outbox.log`，连 curl 都不用。

### 桌面端 / GUI 应用

任何能向 localhost 发 HTTP 的桌面应用都行（Electron / Tauri / 浏览器）：

```js
const r = await fetch("http://127.0.0.1:7654/inbox?since=" + lastSeq);
await fetch("http://127.0.0.1:7654/outbox", {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({text: "你好"})
});
```

### MCP

HTTP 接口天然映射为 MCP tool（`get_inbox` / `send_reply`），包一层任何 MCP 客户端就能驱动微信。

---

## 从零开始

### 环境要求

- Python 3.9+
- 一个能扫码的微信

### 克隆

```bash
git clone https://github.com/biedongbin/wcbridge.git
cd wcbridge
```

### 安装依赖

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # requests + cryptography
```

### 登录（扫码）

```bash
python wcbridge.py --login
```

打印二维码 + 地址，微信扫码，手机确认。凭证存到 `data/credential.json`。

### 启动

```bash
python wcbridge.py
```

看到 `[run] 启动，监听中…`。你从微信发的第一条消息会建立关联（写入 `session.link`）。

### 收发消息（另一个终端）

```bash
curl http://127.0.0.1:7654/health
curl "http://127.0.0.1:7654/inbox?since=0"
curl -X POST http://127.0.0.1:7654/outbox \
  -H 'Content-Type: application/json' \
  -d '{"text":"来自我的 agent"}'
```

---

## 项目结构

```
wcbridge/
├── wcbridge.py        # 全部代码
├── requirements.txt   # requests + cryptography
├── LICENSE            # MIT
├── README.md          # 英文
├── README.zh-CN.md    # 中文（本文件）
├── .gitignore
├── data/              # 凭证 + 媒体（已 gitignore）
│   ├── credential.json
│   └── media/         # 下载的图片/文件
├── inbox.log          # 微信→本地（已 gitignore）
├── outbox.log         # 本地→微信（已 gitignore）
└── session.link       # 关联的微信身份（已 gitignore）
```

## 安全提示

- `data/credential.json` 含微信 bot token —— **绝不要提交**（已在 `.gitignore`）。
- `inbox.log` / `outbox.log` 含消息内容 —— 同样已忽略。
- HTTP 服务只绑定 `127.0.0.1`（本机），不对外网暴露。

## 协议

[MIT](LICENSE)
