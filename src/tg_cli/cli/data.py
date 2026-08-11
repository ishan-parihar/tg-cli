"""Data commands — export, purge."""

import click

from ..console import console
from ..db import MessageDB
from ._chat import resolve_chat_id_or_print
from ._output import (
    dump_structured,
    emit_error,
    emit_structured,
    get_help_hints,
    structured_output_options,
)


@click.group("data")
def data_group():
    """Data management commands (registered at top-level)."""


@data_group.command("export")
@click.argument("chat")
@click.option(
    "-f", "--format", "fmt", type=click.Choice(["text", "json", "yaml", "toon"]), default="text"
)
@click.option("-o", "--output", "output_file", help="Output file path")
@click.option("--hours", type=int, help="Only export last N hours")
@click.option("--full", is_flag=True, help="Show full content without truncation")
@structured_output_options
def export(
    chat: str,
    fmt: str,
    output_file: str | None,
    hours: int | None,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool,
    fields: str | None,
    full: bool,
):
    """Export messages from CHAT to text, JSON, YAML, or TOON."""
    with MessageDB() as db:
        chat_id = resolve_chat_id_or_print(db, chat)
        if chat_id is None:
            return

        if hours:
            msgs = db.get_recent(chat_id=chat_id, hours=hours, limit=100000)
        else:
            msgs = db.get_recent(chat_id=chat_id, hours=None, limit=100000)

    if not msgs:
        if emit_error(
            "no_messages",
            f"No messages found for '{chat}'.",
            as_json=as_json,
            as_yaml=as_yaml,
            as_toon=as_toon,
        ):
            raise SystemExit(1)
        console.print(f"[yellow]No messages found for '{chat}'.[/yellow]")
        return

    # Apply field filtering if specified
    if fields:
        field_list = [f.strip() for f in fields.split(",") if f.strip()]
        filtered_msgs = []
        for msg in msgs:
            filtered = {k: msg.get(k) for k in field_list if k in msg}
            if not full and "content" in filtered:
                filtered["content"] = _truncate_content(filtered["content"])
            filtered_msgs.append(filtered)
        msgs = filtered_msgs
    elif not full:
        # Default truncation for content
        msgs = [{**m, "content": _truncate_content(m.get("content", ""))} for m in msgs]

    # Determine output format
    if fmt in {"json", "yaml", "toon"}:
        if fmt == "json":
            content = dump_structured(msgs, fmt="json")
        elif fmt == "yaml":
            content = dump_structured(msgs, fmt="yaml")
        else:
            from ._output import dump_toon

            content = dump_toon(msgs)
    else:
        lines = []
        for msg in msgs:
            ts = (msg.get("timestamp") or "")[:19]
            sender = msg.get("sender_name") or "Unknown"
            text = msg.get("content") or ""
            lines.append(f"[{ts}] {sender}: {text}")
        content = "\n".join(lines)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(content)
        console.print(f"[green]\u2713[/green] Exported {len(msgs)} messages to {output_file}")
    else:
        console.print(content)

    for hint in get_help_hints("export"):
        console.print(f"[dim]help: {hint}[/dim]")


@data_group.command("purge")
@click.argument("chat")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@structured_output_options
def purge(chat: str, yes: bool, as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Delete all stored messages for CHAT."""
    with MessageDB() as db:
        chat_id = resolve_chat_id_or_print(db, chat)
        if chat_id is None:
            return

        if not yes:
            count = db.count(chat_id)
            if not click.confirm(f"Delete {count} messages from chat {chat_id}?"):
                return

        deleted = db.delete_chat(chat_id)

    payload = {"deleted": True, "count": deleted, "chat": chat}
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return

    console.print(f"[green]\u2713[/green] Deleted {deleted} messages")
    for hint in get_help_hints("purge"):
        console.print(f"[dim]help: {hint}[/dim]")


def _truncate_content(content: str, max_len: int = 500) -> str:
    """Truncate content with indicator."""
    if len(content) <= max_len:
        return content
    return content[:max_len] + f"\n... (truncated, {len(content)} chars total)"
