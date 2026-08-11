"""Shared structured output helpers for CLI commands."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any

import click
import yaml

_OUTPUT_ENV = "OUTPUT"
_SCHEMA_VERSION = "1"


def _toon_escape(value: str) -> str:
    """Escape a string for TOON format."""
    # Escape backslashes first
    value = value.replace("\\", "\\\\")
    # Escape double quotes
    value = value.replace('"', '\\"')
    # Escape newlines
    value = value.replace("\n", "\\n")
    # Escape tabs
    value = value.replace("\t", "\\t")
    return value


def _toon_format_value(value: Any) -> str:
    """Format a Python value as TOON."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return f'"{_toon_escape(value)}"'
    if isinstance(value, list):
        items = [_toon_format_value(v) for v in value]
        return f"[{', '.join(items)}]"
    if isinstance(value, dict):
        pairs = [f"{k}: {_toon_format_value(v)}" for k, v in value.items()]
        return f"{{{', '.join(pairs)}}}"
    # Fallback to string representation
    return f'"{_toon_escape(str(value))}"'


def dump_toon(data: Any, *, fields: list[str] | None = None) -> str:
    """Serialize structured data to TOON format.

    For lists of objects, emits TOON array with field schema header.
    For single objects, emits TOON object.
    """
    if isinstance(data, list):
        if not data:
            return "[]"

        # Determine fields from first object if not specified
        if fields is None and data and isinstance(data[0], dict):
            fields = list(data[0].keys())

        if fields:
            # Emit TOON array with schema header
            field_spec = "{" + ",".join(fields) + "}"
            lines = [f"items{field_spec}:"]
            for item in data:
                if isinstance(item, dict):
                    values = [_toon_format_value(item.get(f, "")) for f in fields]
                    lines.append(f"  {', '.join(values)}")
                else:
                    values = [_toon_format_value(item)]
                    lines.append(f"  {', '.join(values)}")
            return "\n".join(lines)
        else:
            # Simple array
            return "[" + ", ".join(_toon_format_value(v) for v in data) + "]"

    elif isinstance(data, dict):
        if fields:
            # Filter dict to only include specified fields
            pairs = [f"{k}: {_toon_format_value(v)}" for k, v in data.items() if k in fields]
        else:
            pairs = [f"{k}: {_toon_format_value(v)}" for k, v in data.items()]
        return "{" + ", ".join(pairs) + "}"

    else:
        return _toon_format_value(data)


def default_structured_format(
    *,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool = False,
) -> str | None:
    """Resolve explicit flags first, then fall back to env and TTY defaults."""
    explicit_count = sum([as_json, as_yaml, as_toon])
    if explicit_count > 1:
        raise click.UsageError("Use only one of --json, --yaml, or --toon.")
    if as_yaml:
        return "yaml"
    if as_json:
        return "json"
    if as_toon:
        return "toon"
    output_mode = os.getenv(_OUTPUT_ENV, "auto").strip().lower()
    if output_mode == "yaml":
        return "yaml"
    if output_mode == "json":
        return "json"
    if output_mode == "toon":
        return "toon"
    if output_mode == "rich":
        return None
    if not sys.stdout.isatty():
        # Non-interactive (machine-readable): default to YAML per SCHEMA.md.
        # TOON remains available via --toon / OUTPUT=toon.
        return "yaml"
    return None


def structured_output_options(command: Callable) -> Callable:
    """Add --json/--yaml/--toon/--fields flags to a click command."""
    command = click.option("--yaml", "as_yaml", is_flag=True, help="Output as YAML")(command)
    command = click.option("--json", "as_json", is_flag=True, help="Output as JSON")(command)
    command = click.option(
        "--toon", "as_toon", is_flag=True, help="Output as TOON (default for non-TTY)"
    )(command)
    command = click.option(
        "--fields",
        "fields",
        help="Comma-separated list of fields to include (default: minimal schema)",
    )(command)
    return command


def emit_structured(
    data: Any,
    *,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool = False,
) -> bool:
    """Emit structured output and return True when a structured format was used.

    Note: Field filtering is handled by individual commands via --fields flag.
    This function only handles format serialization.
    """
    fmt = default_structured_format(as_json=as_json, as_yaml=as_yaml, as_toon=as_toon)
    if fmt is None:
        return False

    payload = _normalize_success_payload(data)
    if fmt == "toon":
        click.echo(dump_toon(payload.get("data", payload)))
    else:
        click.echo(dump_structured(payload, fmt=fmt))
    return True


def dump_structured(data: Any, *, fmt: str) -> str:
    """Serialize structured data to JSON or YAML text."""
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if fmt == "yaml":
        return yaml.safe_dump(
            data,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    raise ValueError(f"Unsupported structured format: {fmt}")


def success_payload(data: Any) -> dict[str, Any]:
    """Wrap structured success data in the shared agent schema."""
    return {
        "ok": True,
        "schema_version": _SCHEMA_VERSION,
        "data": data,
    }


def error_payload(code: str, message: str, *, details: Any | None = None) -> dict[str, Any]:
    """Wrap structured error data in the shared agent schema."""
    error = {
        "code": code,
        "message": message,
    }
    if details is not None:
        error["details"] = details
    return {
        "ok": False,
        "schema_version": _SCHEMA_VERSION,
        "error": error,
    }


def _normalize_success_payload(data: Any) -> Any:
    """Wrap plain structured data in the shared agent success schema."""
    if isinstance(data, dict) and data.get("schema_version") == _SCHEMA_VERSION and "ok" in data:
        return data
    return success_payload(data)


def emit_error(
    code: str,
    message: str,
    *,
    as_json: bool | None = None,
    as_yaml: bool | None = None,
    as_toon: bool | None = None,
    details: Any | None = None,
) -> bool:
    """Emit a structured error when the active output mode is machine-readable."""
    if as_json is None or as_yaml is None or as_toon is None:
        ctx = click.get_current_context(silent=True)
        params = ctx.params if ctx is not None else {}
        as_json = bool(params.get("as_json", False)) if as_json is None else as_json
        as_yaml = bool(params.get("as_yaml", False)) if as_yaml is None else as_yaml
        as_toon = bool(params.get("as_toon", False)) if as_toon is None else as_toon

    fmt = default_structured_format(
        as_json=bool(as_json), as_yaml=bool(as_yaml), as_toon=bool(as_toon)
    )
    if fmt is None:
        return False
    payload = error_payload(code, message, details=details)
    if fmt == "toon":
        click.echo(dump_toon(payload))
    else:
        click.echo(dump_structured(payload, fmt=fmt))
    return True


def get_help_hints(command_name: str, context: dict | None = None) -> list[str]:
    """Generate contextual help hints for a command."""
    hints = []

    if command_name == "chats":
        hints.append("Run `tg info <chat>` for details on a specific chat")
        hints.append("Run `tg history <chat>` to fetch messages")
    elif command_name == "history":
        hints.append("Run `tg search <keyword> --chat <chat>` to search within this chat")
        hints.append("Run `tg recent --chat <chat>` for recent messages without keyword")
    elif command_name == "search":
        hints.append("Run `tg filter <keywords>` for OR-logic multi-keyword search")
        hints.append("Run `tg recent --chat <chat>` to browse recent messages")
    elif command_name == "recent":
        hints.append("Run `tg search <keyword> --chat <chat>` to search messages")
        hints.append("Run `tg today --chat <chat>` for today's messages")
    elif command_name == "stats":
        hints.append("Run `tg top` to see most active senders")
        hints.append("Run `tg timeline` for activity over time")
    elif command_name == "top":
        hints.append("Run `tg stats` for per-chat message counts")
        hints.append("Run `tg timeline` for activity over time")
    elif command_name == "timeline":
        hints.append("Run `tg stats` for per-chat message counts")
        hints.append("Run `tg top` to see most active senders")
    elif command_name == "today":
        hints.append("Run `tg recent --hours 24` for last 24 hours")
        hints.append("Run `tg filter <keywords>` to search today's messages")
    elif command_name == "filter":
        hints.append("Run `tg search <keyword>` for single-keyword search")
        hints.append("Run `tg recent` to browse without filtering")
    elif command_name == "sync":
        hints.append("Run `tg sync-all` to sync all chats")
        hints.append("Run `tg refresh` to refresh local cache from all dialogs")
    elif command_name == "sync-all":
        hints.append("Run `tg refresh` to refresh local cache from all dialogs")
        hints.append("Run `tg stats` to see updated counts")
    elif command_name == "refresh":
        hints.append("Run `tg stats` to see updated counts")
        hints.append("Run `tg today` to see today's new messages")
    elif command_name == "info":
        hints.append("Run `tg history <chat>` to fetch messages")
        hints.append("Run `tg recent --chat <chat>` for recent messages")
    elif command_name == "whoami":
        hints.append("Run `tg status` for auth status")
        hints.append("Run `tg chats` to see joined chats")
    elif command_name == "status":
        hints.append("Run `tg whoami` for user details")
        hints.append("Run `tg chats` to see joined chats")
    elif command_name == "send":
        hints.append("Run `tg edit <chat> <msg_id> <new_text>` to edit sent message")
        hints.append("Run `tg delete <chat> <msg_id>` to delete a message")
    elif command_name == "edit":
        hints.append("Run `tg send <chat> <message>` to send a new message")
        hints.append("Run `tg delete <chat> <msg_id>` to delete instead")
    elif command_name == "delete":
        hints.append("Run `tg edit <chat> <msg_id> <new_text>` to edit instead")
        hints.append("Run `tg send <chat> <message>` to send a new message")
    elif command_name == "export":
        hints.append("Run `tg purge <chat>` to delete all stored messages")
        hints.append("Run `tg history <chat>` to refetch messages")
    elif command_name == "purge":
        hints.append("Run `tg sync <chat>` to refetch messages")
        hints.append("Run `tg export <chat>` to export before purging")

    return hints
