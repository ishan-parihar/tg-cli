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

## Field filtering

`--fields a,b,c` narrows list items / dicts to the requested keys (default schemas
are listed in `query.py` / `tg.py` as `*_DEFAULT_FIELDS`).
