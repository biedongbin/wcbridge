# wcbridge

**[English](README.md)** | [中文](README.zh-CN.md)

A minimal bridge between WeChat and any AI CLI.

WeChat → `wcbridge` → your agent (Claude Code, opencode, codex, or any tool that can `curl` or read a file), and back. A pure message channel — supports **text, images, and files**. No business logic, no database; dependencies are just `requests` + `cryptography`.

---

## Design Philosophy

**Minimal, pure-channel, file-decoupled.**

- **Pure channel** — wcbridge does one thing: move text, images, and files between WeChat and your local tools. No skills, no business rules, no persistence beyond login.
- **Dual interface** — a tiny local HTTP server for second-latency reads/writes, plus plain log files for maximum compatibility.
- **Single file** — the whole bridge is one readable Python file.
- **No external state** — the linked WeChat identity is written to one `session.link` file. That's it.

## How It Works

```
WeChat (phone)
   │  iLink long-poll (35s)
   ▼
wcbridge (localhost daemon)
   │  ├─ inbox.log   +  in-memory queue  →  HTTP GET /inbox?since=N
   │  └─ outbox.log  +  in-memory queue  ←  HTTP POST /outbox {text}
   ▼
Claude Code / opencode / codex / any local tool
```

| Step | Method | Latency |
|---|---|---|
| WeChat → wcbridge | iLink long-poll (returns immediately on message, 35s timeout) | realtime |
| wcbridge → your tool | local HTTP (`GET /inbox?since=N`) or read `inbox.log` | seconds |
| your tool → WeChat | local HTTP (`POST /outbox {"text":"..."}`) or append `outbox.log` | seconds |

**Three HTTP endpoints** (`http://127.0.0.1:7654`):

- `GET /health` — liveness
- `GET /inbox?since=N` — pull messages newer than N
- `POST /outbox` — body `{"text":"..."}` sends a message to WeChat

### Message types

Each inbox message carries a `type`:

| type | meaning | extra fields |
|---|---|---|
| `text` | text message | `text` |
| `image` | image (downloaded, decrypted, saved) | `path`, `ext`, `size` |
| `file` | file (downloaded, decrypted, saved) | `path`, `name`, `ext`, `size` |

Images and files arrive via iLink's AES-128-ECB encrypted media channel, get decrypted, and land in `data/media/`. The inbox returns a local `path`; the agent just reads the file. A single WeChat message with multiple items (text + image + file) becomes multiple inbox entries.

> wcbridge uses the iLink bot protocol. Scan a QR code once, the credential is saved locally, and the bridge long-polls for incoming messages.

## Use Cases

- **Remote-control Claude Code from your phone** — leave your desk, send a task via WeChat, Claude Code picks it up and replies. This is what it was built for.
- **WeChat ↔ any local script** — a generic bridge: anything that can read a file or call HTTP can talk to WeChat.
- **Lightweight personal WeChat bot** — when a full bot framework is overkill.

---

## Works With Any AI CLI

wcbridge is **protocol-agnostic**: anything that can `curl` or read a file can hook in.

### Universal pattern

Any agent does just two things: pull instructions, push results.

1. **Pull**: poll `GET /inbox?since=<last_seq>` (or `tail -f inbox.log`)
2. **Push**: `POST /outbox {"text":"..."}` (or append `outbox.log`)

### Claude Code

Schedule a watch prompt with `CronCreate` every minute (or a hook):

```bash
curl -s "http://127.0.0.1:7654/inbox?since=$(cat /tmp/wc_last_seq)"
curl -X POST http://127.0.0.1:7654/outbox -H 'Content-Type: application/json' -d '{"text":"done"}'
```

### opencode / codex / other CLIs

These have no built-in scheduler — bridge with cron or a background script:

```bash
LAST=$(cat /tmp/wc_last_seq 2>/dev/null || echo 0)
RESP=$(curl -s "http://127.0.0.1:7654/inbox?since=$LAST")
echo "$RESP" | python3 -c "import sys,json;[print(m['text']) for m in json.load(sys.stdin)['msgs']]" \
  | opencode chat   # or: codex exec -  /  any CLI
echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['last_seq'])" > /tmp/wc_last_seq
```

Or simplest: let the CLI read `inbox.log` and write `outbox.log` directly — no curl needed.

### Desktop / GUI apps

Anything that can HTTP fetch to localhost works (Electron / Tauri / browser):

```js
const r = await fetch("http://127.0.0.1:7654/inbox?since=" + lastSeq);
await fetch("http://127.0.0.1:7654/outbox", {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({text: "Hello"})
});
```

### MCP

The HTTP endpoints map naturally to MCP tools (`get_inbox` / `send_reply`). Wrap them and any MCP client can drive WeChat.

---

## Getting Started

### Prerequisites

- Python 3.9+
- A WeChat account (to scan the QR code)

### Clone

```bash
git clone https://github.com/biedongbin/wcbridge.git
cd wcbridge
```

### Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # requests + cryptography
```

### Log in (scan QR)

```bash
python wcbridge.py --login
```

Prints a QR code + URL. Scan with WeChat, confirm on your phone. Credential saved to `data/credential.json`.

### Run

```bash
python wcbridge.py
```

You'll see `[run] 启动，监听中…`. The first message you send from WeChat links your identity (`session.link`).

### Send & receive (from another terminal)

```bash
curl http://127.0.0.1:7654/health
curl "http://127.0.0.1:7654/inbox?since=0"
curl -X POST http://127.0.0.1:7654/outbox \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello from my agent"}'
```

---

## Project Layout

```
wcbridge/
├── wcbridge.py        # the whole bridge
├── requirements.txt   # requests + cryptography
├── LICENSE            # MIT
├── README.md          # English (this file)
├── README.zh-CN.md    # 中文
├── .gitignore
├── data/              # credentials + media (ignored)
│   ├── credential.json
│   └── media/         # downloaded images & files
├── inbox.log          # inbound (ignored)
├── outbox.log         # outbound (ignored)
└── session.link       # linked user (ignored)
```

## Security Notes

- `data/credential.json` holds your WeChat bot token — **never commit it** (in `.gitignore`).
- `inbox.log` / `outbox.log` contain message content — also ignored.
- The HTTP server binds to `127.0.0.1` only, not exposed to the network.

## License

[MIT](LICENSE)
