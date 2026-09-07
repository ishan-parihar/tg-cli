# tg-cli

<!-- T2I HERO SPEC — Subject: a local-first Telegram mirror — your Telegram account (MTProto) on the left syncing into a local SQLite cache vault, with search/export/agent-query beams (JSON, YAML, TOON) fanning out to an AI agent on the right; a clock/automation ring around the vault. Composition: left-to-right sync pipeline, vault as the center of gravity. Palette: Telegram sky #229ed9 → deep slate #0f172a → cache emerald #34d399 → agent violet #8b5cf6. Style: dark flat vector, glowing sync pulses, no text. 16:9. -->

[![CI](https://github.com/ishan-parihar/tg-cli/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ishan-parihar/tg-cli/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/kabi-tg-cli)](https://pypi.org/project/kabi-tg-cli/)
![LOC](https://img.shields.io/badge/LOC-7.2K-informational?style=flat-square)
![Status](https://img.shields.io/badge/Status-Active-brightgreen)
![Language](https://img.shields.io/badge/Python-3.11-blue?logo=python)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Version](https://img.shields.io/pypi/v/kabi-tg-cli?label=version)](https://pypi.org/project/kabi-tg-cli/)

> **PyPI package:** [`kabi-tg-cli`](https://pypi.org/project/kabi-tg-cli/) — install with `uv tool install kabi-tg-cli`

Telethon-powered Telegram CLI for local-first sync, search, export, and agent-friendly retrieval.

---

## What it does

`tg-cli` uses your own Telegram account over MTProto (not Bot API). It syncs messages into a local SQLite cache so humans and AI agents can query the same data quickly with `--json`, `--yaml`, or `--toon` output.

## How it compares

| Capability | **tg-cli** | Telegram Desktop | Telethon scripts | tgt / other CLIs |
|---|---|---|---|---|
| **Local SQLite mirror** | ✅ full-text searchable cache | ⚠️ local DB, not queryable | ❌ ad-hoc | ⚠️ |
| **Agent-friendly output** | ✅ `--json` / `--yaml` / `--toon` | ❌ GUI | ⚠️ custom | ⚠️ |
| **Scheduled automation** | ✅ built-in scheduling | ❌ | ⚠️ | ⚠️ |
| **Your account, MTProto** | ✅ no bot limits | ✅ | ✅ | ✅ |
| **Search + export + sync** | ✅ one tool | ⚠️ export only | ⚠️ | ✅ |
| **Installable as agent skill** | ✅ | ❌ | ❌ | ❌ |

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

> **0.7+ recommendation:** use the daemon instead of `tg listen`. It delivers
> queued writes too, so `tg send "team" "deploy done"` is instant whether you
> typed it or an agent did. See [Daemon & queue](#daemon--queue).

---

## Daemon & queue (agent-safe writes)

Failures are **soft**: auth, cooldown, and rate-limit problems return
`{ok: false, ...}` with exit 0 — never a crash. Writes take `--queue` to
defer instead of failing.

```bash
# One persistent session — live updates + queue delivery, sole session owner.
tg daemon start
# (logs at <data-dir>/daemon.log; supervised by your init: systemd, tmux, docker)

# Writes are instant: the daemon owns the Telegram session, so agents never
# open a second connection (which would corrupt the session file).
tg send "Team" "deploy done"
tg send "Team" "x" --queue      # enqueue when rate-limited instead of failing
tg refresh --queue             # enqueue when the 2h cooldown is active

# Inspect / manage the queue.
tg queue status --yaml         # {pending, running, done, failed, oldest_pending_at}
tg queue clear                 # drop finished jobs (pending kept)

# Lifecycle.
tg daemon status               # heartbeat freshness + pid
tg daemon stop
```

**How it works:**

| Surface | Who opens it | Why |
|---|---|---|
| `tg-cli` (agent / cron) | Never the Telegram session file | Only writes to local SQLite + JSONL queue |
| `tg daemon run` | Only owner of `*.session` | Telethon sessions cannot be shared between processes |
| `<data-dir>/daemon.json` | heartbeat (pid, started_at, updated_at) | `is_daemon_alive()` check before any agent write |
| `<data-dir>/queue/jobs.jsonl` | JSONL with `fcntl.flock` advisory lock | Cross-process IPC, atomic writes with `fsync` |

**Queue contract:**

- `send`/`edit`/`delete`/`refresh` enqueue immediately when the daemon is
  alive (no second session, no `connect()` roundtrip).
- When the daemon is **not** alive: command runs directly with `connect()`,
  catching `TelegramRateLimitedError` and returning `{ok: false, code: rate_limited,
  details: {retry_after_seconds: ...}}` on throttle. With `--queue`, the
  refused call is enqueued instead of failing.
- `queue drain` refuses when the daemon is alive (avoid the session collision).
- Job kinds: `send`, `edit`, `delete`, `refresh`. `MAX_PENDING=100`.

**Error codes returned by the CLI:**

| `error.code` | When | CLI command | Exits |
|---|---|---|---|
| `refresh_cooldown` | `refresh` inside 2h | `tg refresh` | 0 |
| `rate_limited` | Telegram 429 / flood / breaker open | `tg send`/`edit`/`delete`/`refresh` | 0 |
| `queue_full` | `MAX_PENDING` reached | `tg send --queue` | 0 |
| `daemon_running` | `queue drain` while daemon alive | `tg queue drain` | 0 |
| `not_running` | `daemon stop` with no heartbeat | `tg daemon stop` | 0 |
| `auth_required` | first-run, no session | any write command | 0 |

The full structured envelope (`{ok, schema_version, data|error}`) is documented
in [SCHEMA.md](./SCHEMA.md).

**systemd unit:**

```bash
mkdir -p ~/.config/systemd/user
cp systemd/tg-cli-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tg-cli-daemon.service
systemctl --user status tg-cli-daemon.service
journalctl --user -u tg-cli-daemon.service -f
```

The unit runs `tg daemon run --interval 5`, restarts on failure, and
auto-restarts after crashes.

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
| `send` / `edit` / `delete` | Write operations (`--queue` = enqueue when limited) |
| `listen` | Real-time listener (`--persist` = auto-reconnect) |
| `queue status` / `queue drain` / `queue clear` | Durable write queue (the agent-safe path) |
| `daemon status` / `run` / `start` / `stop` | Persistent client: live updates + queue delivery |

All query commands support `--sync-first` to refresh before reading.

---

## Output formats (agent-friendly)

| Flag | Description |
|------|-------------|
| `--toon` | Token-Oriented Object Notation (~40% smaller than JSON) — opt-in via `--toon` |
| `--yaml` | **Default for non-TTY** — token-efficient, human-readable |
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

2. **Run the daemon instead of cron `tg refresh`** — a single persistent session
   looks like Telegram Desktop left open (lowest-suspicion profile). Periodic
   refresh sweeps are exactly the traffic shape Telegram's heuristics watch for.

3. **Limit sync frequency** — `tg refresh` ≤ 1–2×/day when used directly.

4. **Use `--delay` and `--max-chats`**:
   ```bash
   tg refresh --delay 3.0 --max-chats 30
   ```

5. **Prefer established accounts** — new/inactive accounts flag easier.

6. **Prefer read operations** — `tg send` carries higher risk. Use `--queue` to
   defer when the rate limit / breaker is open.

---

## Limitations

- **Full mutation surface** — send/edit/delete verified end-to-end (message send → edit → delete round-trip; deleting an already-deleted message returns `MessageIdInvalidError` confirming the delete landed).
- **Deletion is soft** — Telegram deletes messages rather than permanently wiping history server-side; deleted messages vanish from sync but may remain recoverable server-side depending on account settings.
- **MTProto rate limits** — Telegram's flood-control limits (spam-triggered `FLOOD_WAIT`) apply; `--delay` between sends mitigates it.
- **Self-chat only for verification** — messaging yourself (`peer` = your own id) is the safe test surface; mass unsolicited sends trigger Telegram's anti-spam.

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

---

## ☕ Support & Sponsorship

If you find this project useful, consider supporting ongoing development:

[![Sponsor](https://img.shields.io/badge/Sponsor-GitHub%20Sponsors-ea4aaa?style=flat-square&logo=github)](https://github.com/sponsors/ishan-parihar)
[![Donate](https://img.shields.io/badge/Donate-Razorpay-3395FF?style=flat-square)](https://rzp.io/rzp/ishan-parihar)

Your support funds new features, releases, and infrastructure for the whole ecosystem.