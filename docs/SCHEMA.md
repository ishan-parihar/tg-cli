# Structured Output Schema

`tg-cli` uses a shared agent-friendly envelope for machine-readable output.

## Success

```yaml
ok: true
schema_version: "1"
data: ...
```

## Error

```yaml
ok: false
schema_version: "1"
error:
  code: chat_not_found
  message: Chat 'foo' not found in database.
```

## Notes

- `--yaml` and `--json` both use this envelope
- non-TTY stdout defaults to YAML (`OUTPUT=toon` / `--toon` opts into TOON)
- `--toon` unwraps the envelope and emits only `data` (TOON object/array)

## Per-command `data` shapes

| Command | `data` shape |
|---------|--------------|
| `search` | list of messages (`id`, `timestamp`, `sender_name`, `chat_name`, `content`) |
| `recent` | list of messages (same schema as `search`) |
| `chats` | list of chats (`id`, `name`, `type`, `unread`) |
| `stats` | dict: `{total, chats: [{chat_id, chat_name, msg_count, first_msg, last_msg}]}` |
| `top` | dict: `{count, total, senders: [{sender_name, msg_count, first_msg, last_msg}]}` |
| `timeline` | dict: `{granularity, hours, periods: [{period, msg_count}]}` |
| `today` | dict: `{total_messages, chat_count, latest_timestamp, chats: {chat_name: [messages]}}` |
| `filter` | dict: `{total_scanned, matched, keywords, chat_count, chats: {chat_name: [messages]}}` |
| `info` | dict of chat fields (`Title`, `ID`, `Type`, …) |
| `status` | dict: `{authenticated, user: {id, name, username, phone}}` |
| `whoami` | dict: `{user: {id, name, username, phone}}` |
| `history` / `sync` / `sync-all` / `refresh` / `send` / `edit` / `delete` | small result dicts (`stored`, `synced`, `new_messages`, `sent`, …) |
| `queue status` | `{pending, running, done, failed, total, oldest_pending_at}` |
| `queue drain` | `{done, failed, deferred}` |
| `queue clear` | `{cleared}` |
| `daemon status` | `{alive, heartbeat: {pid, started_at, updated_at} \| null}` |
| `daemon start` | `{started, pid?, log?, reason?}` |
| `daemon stop` | `{stopped, pid?, reason?}` |

### Queue / daemon payloads

When `--queue` enqueues a write, the response is:

```yaml
ok: true
schema_version: "1"
data:
  queued: true
  kind: send          # send | edit | delete | refresh
  job_id: a1b2c3d4e5f6
  eta_seconds: 0
```

## Error codes (v0.7+)

Every CLI command now exits 0 even on failure; the agent reads the envelope.

| `error.code` | Command(s) | When | `details` |
|---|---|---|---|
| `auth_required` | any write command | no session, first run | — |
| `chat_not_found` | `info`, `history`, etc. | chat identifier not in local cache | — |
| `refresh_cooldown` | `refresh` | ran inside the 2-hour cooldown | `{retry_after_seconds}` |
| `rate_limited` | `send`/`edit`/`delete`/`refresh` | Telegram 429, flood, breaker open | `{retry_after_seconds, phone?, category?}` |
| `queue_full` | `send --queue` etc. | 100 pending jobs queued | — |
| `daemon_running` | `queue drain` | daemon heartbeat is fresh | — |
| `not_running` | `daemon stop` | no heartbeat on disk | — |

## Field filtering

`--fields a,b,c` narrows list items / dicts to the requested keys (default schemas
are listed in `query.py` / `tg.py` as `*_DEFAULT_FIELDS`).
