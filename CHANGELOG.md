# Changelog

All notable changes to this project will be documented in this file.

## 0.7.0 - 2026-09-07

### Persistent client + agent-safe queue
- **`tg daemon run` / `start` / `stop` / `status`** — long-lived Telegram session that catches updates live and delivers queued writes. Sole owner of the `*.session` file (Telethon sessions cannot be shared between processes), so agents never collide.
- **`tg queue status` / `queue drain` / `queue clear`** — durable JSONL queue (`<data-dir>/queue/jobs.jsonl`) with `fcntl.flock` advisory locking and `os.fsync` for crash durability.
- **`--queue` flag on `tg send` / `edit` / `delete` / `refresh`** — when the daemon is alive, writes auto-enqueue; when it's not, refused calls enqueue with `not_before` instead of failing.

### Soft-fail envelope
- All previously-hard exits (`auth_required`, `refresh_cooldown`, `rate_limited`, `queue_full`, `daemon_running`, `not_running`) now return `{ok: false, code: ...}` with **exit 0**. Agents never crash their run loop on a Telegram-side restriction.
- New error codes documented in README + SCHEMA.md.

### Anti-abuse
- **Proactive rate-limit gate** (`tg_cli.ratelimit`): sliding-window account buckets keyed by `(phone, category)`, per-peer send limiter (1 msg /5s to a private chat, 20/min to groups/channels), reactive circuit breaker that suspends a category after 3 floods for 5 min. Refunds the account slot when the peer refuses.
- **Guarded `send` / `edit` / `delete`**: every write goes through the gate, handles `FloodWaitError` with one retry, surfaces `TelegramRateLimitedError` after `max_defer`.
- **2-hour `refresh` cooldown** persisted in `<data-dir>/cooldowns.json`. `--force` bypasses it; `--queue` defers it.
- **Listener** now uses Telethon's cached `event.chat` / `msg._sender` (no extra `get_chat()` / `get_sender()` RPC per inbound message). `--persist` reconnect uses exponential backoff with ±30% jitter.

### Plumbing
- SQLite `PRAGMA busy_timeout=5000` so daemon writers + CLI readers coexist.
- New `update_message()` / `delete_message()` DB methods for the daemon's edit/delete handlers.
- Listener-edit handler now upserts (inserts the message if the daemon missed the original).

### Tests
- 49 red-team probes in `tests/test_redteam.py` covering concurrency races, file ownership, edge cases, security, stress, drain loop termination. Concurrent enqueue race fixed: 50×20 enqueues now land 1000 jobs (previously dropped ~97%).
- Full suite: 199 tests pass, ruff clean.

### Ops
- `systemd/tg-cli-daemon.service` — Type=simple user unit, `Restart=on-failure`, `RestartSec=10`.

## 0.4.3 - 2026-03-11

- Use Telegram Desktop built-in API credentials (API_ID=2040) as defaults; users no longer need to apply for their own app credentials
- Updated README and SKILL.md to reflect zero-config authentication

## 0.4.1 - 2026-03-10

- Fixed GitHub publish workflow permissions so PyPI checkout can read repository contents
- Fixed ClawHub publish workflow to use the Node.js payload workaround for `acceptLicenseTerms`

## 0.4.0 - 2026-03-10

- Switched the project license to Apache-2.0
- Removed built-in Telegram app credentials; users now provide `TG_API_ID` and `TG_API_HASH`
- Added YAML output support and documented YAML as the preferred agent format
- Added `tg recent`
- Added regex search with `tg search --regex`
- Added `tg refresh` as the recommended daily refresh entrypoint
- Added `--sync-first` to query commands
- Added `tg listen --persist` for automatic reconnect
- Improved local query safety with chat ambiguity detection and clearer `today` hints
- Added cron and systemd examples for scheduled refresh
