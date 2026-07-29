# tg-cli

[![CI](https://github.com/ishan-parihar/tg-cli/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ishan-parihar/tg-cli/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/kabi-tg-cli)](https://pypi.org/project/kabi-tg-cli/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

> **PyPI package:** [`kabi-tg-cli`](https://pypi.org/project/kabi-tg-cli/) — install with `uv tool install kabi-tg-cli`

Telethon-powered Telegram CLI for local-first sync, search, export, and agent-friendly retrieval.

---

## What it does

`tg-cli` uses your own Telegram account over MTProto (not Bot API). It syncs messages into a local SQLite cache so humans and AI agents can query the same data quickly with `--json`, `--yaml`, or `--toon` output.

**First successful action:**

```bash
tg refresh           # sync all chats to local DB
tg today --yaml      # see today's messages
```

---

## Proof

```
$ tg today --yaml
chats:
  "Rust Programming":
    - chat_name: "Rust Programming"
      msg_count: 47
      messages:
        - id: 18923
          timestamp: "2025-07-29T14:32:11+00:00"
          sender_name: "Alex"
          content: "New async RFC merged! 🎉"
        - id: 18924
          timestamp: "2025-07-29T14:35:44+00:00"
          sender_name: "Sam"
          content: "Great, now we can..."
  "Team Standup":
    - chat_name: "Team Standup"
      msg_count: 12
      messages:
        - id: 9012
          timestamp: "2025-07-29T09:00:00+00:00"
          sender_name: "Bot"
          content: "Daily standup: 10am UTC"
```

---

## Why it's different

| | Bot API tools | tg-cli |
|---|---|---|
| **Auth** | Bot token | Your user account (MTProto) |
| **Scope** | Bot-added chats only | All your dialogs |
| **History** | Limited by API | Full local SQLite archive |
| **Output** | JSON only | TOON / YAML / JSON / Rich |
| **Agents** | Hard to parse | Structured schemas, truncation, aggregates |

Mechanism: local-first. Queries hit SQLite by default. `tg refresh` (or `--sync-first`) pulls from Telegram before reading.

---

## Install

```bash
# Recommended: uv tool (isolated, fast, auto-updates)
uv tool install kabi-tg-cli

# Or: pipx / pip
pipx install kabi-tg-cli
pip install kabi-tg-cli
```

Upgrade:

```bash
uv tool upgrade kabi-tg-cli
# or: pipx upgrade kabi-tg-cli
```

From GitHub (latest main):

```bash
uv tool install git+https://github.com/ishan-parihar/tg-cli.git
```

From source:

```bash
git clone https://github.com/ishan-parihar/tg-cli.git
cd tg-cli
uv sync --extra dev
```

---

## Quick start

```bash
# First login (uses Telegram Desktop built-in credentials by default)
tg chats

# Verify account
tg status
tg whoami

# Daily driver: refresh local cache
tg refresh

# Read & search
tg today
tg recent --hours 24 --limit 20 --yaml
tg search "Rust" --hours 48
tg filter "Rust,Golang,remote" --hours 48 --sync-first --yaml

# Near-real-time cache
tg listen --persist
```

---

## Commands

| Command | Purpose |
|---------|---------|
| `chats` | List joined dialogs |
| `status` / `whoami` | Auth info |
| `refresh` | Sync all dialogs (recommended daily) |
| `sync-all` | Lower-level full sync |
| `sync <chat>` | Incremental single-chat sync |
| `today` | Today's messages, grouped by chat |
| `recent` | Browse recent messages |
| `search` | Keyword/regex search |
| `filter` | OR-logic multi-keyword filter |
| `top` | Most active senders |
| `timeline` | Activity over time (day/hour) |
| `stats` | Per-chat message counts |
| `export` | Export to text/JSON/YAML/TOON |
| `send` / `edit` / `delete` | Write operations |
| `listen` | Real-time listener (`--persist` = auto-reconnect) |

All query commands support `--sync-first` to refresh before reading.

---

## Output formats (agent-friendly)

| Flag | Description |
|------|-------------|
| `--toon` | **Default for non-TTY** — Token-Oriented Object Notation (~40% smaller than JSON) |
| `--yaml` | Token-efficient, human-readable |
| `--json` | Strict JSON for `jq` / downstream schemas |
| `--fields id,timestamp,content` | Minimal schema (3-4 cols) |
| `--full` | Disable content truncation (default: 500 chars) |

Structured output contract: [SCHEMA.md](./SCHEMA.md)

**Agent workflow:**

```bash
tg refresh --yaml
tg chats --yaml
tg recent --hours 24 --sync-first --yaml
tg search "keyword" --chat "GroupName" --sync-first --yaml
```

---

## Scheduling

### cron

See [examples/tg-refresh.cron](./examples/tg-refresh.cron)

### systemd user timer

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/tg-refresh.service ~/.config/systemd/user/
cp examples/systemd/tg-refresh.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tg-refresh.timer
```

---

## Account safety

`tg-cli` uses your personal account via MTProto. To reduce restriction risk:

1. **Use your own API credentials** — create at [my.telegram.org](https://my.telegram.org):
   ```bash
   export TG_API_ID=12345678
   export TG_API_HASH="your_hash_here"
   ```
   Default `api_id=2040` (Telegram Desktop) is shared and more scrutinized.

2. **Limit sync frequency** — `tg refresh` ≤ 1–2×/day.

3. **Use `--delay` and `--max-chats`**:
   ```bash
   tg refresh --delay 3.0 --max-chats 30
   ```

4. **Prefer established accounts** — new/inactive accounts flag easier.

5. **Prefer read operations** — `tg send` carries higher risk.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `No messages today` | Run `tg refresh` or use `tg today --sync-first` |
| `Chat '...' not found in database` | Run `tg refresh` first, or use numeric `chat_id` from `tg chats --yaml` |
| Auth prompt appears | Set `TG_API_ID`/`TG_API_HASH`, ensure session file exists |
| `FloodWaitError` | Increase `--delay`, reduce `--max-chats` |

---

## AI Agent Skill

Ships with [`SKILL.md`](./SKILL.md) for agent integration. Recommended agent output: `--yaml` (token-efficient, parseable).

---

## License

Apache-2.0