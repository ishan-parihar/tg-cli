"""Telegram subcommands — send, edit, delete, and more."""

import asyncio
import time

import click
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..client import authenticate, connect, fetch_history, get_chat_info, list_chats, listen
from ..console import console
from ..db import MessageDB
from ._chat import _parse_chat, resolve_chat_id_or_print
from ._output import (
    default_structured_format,
    dump_structured,
    dump_toon,
    emit_structured,
    error_payload,
    structured_output_options,
    success_payload,
    get_help_hints,
)
from ._sync import sync_all_dialogs, sync_chat_dialog


async def _run_with_auth(coro, *, as_json: bool, as_yaml: bool, as_toon: bool):
    """Run an async operation that requires auth, handling auth errors with structured output."""
    try:
        return await coro
    except RuntimeError as exc:
        if "Not authenticated" in str(exc):
            fmt = default_structured_format(as_json=as_json, as_yaml=as_yaml, as_toon=as_toon)
            if fmt:
                click.echo(dump_toon(error_payload("auth_required", str(exc))))
                return None
        raise


# Minimal default schemas (3-4 fields per AXI spec)
CHATS_DEFAULT_FIELDS = ["id", "name", "type", "unread"]
MESSAGE_DEFAULT_FIELDS = ["id", "timestamp", "sender_name", "content"]
SYNC_RESULT_FIELDS = ["chat", "new_messages", "total_messages"]
USER_DEFAULT_FIELDS = ["id", "name", "username", "phone"]


def _parse_fields(fields_param: str | None, default_fields: list[str]) -> list[str] | None:
    """Parse --fields parameter or return default fields."""
    if fields_param:
        return [f.strip() for f in fields_param.split(",") if f.strip()]
    return default_fields


def _telegram_user_payload(me) -> dict[str, str | int]:
    """Normalize Telegram user info for structured agent output."""
    name = " ".join(part for part in [me.first_name, me.last_name] if part).strip()
    return {
        "id": me.id,
        "name": name,
        "username": me.username or "",
        "first_name": me.first_name or "",
        "last_name": me.last_name or "",
        "phone": me.phone or "",
    }


@click.group("tg")
def tg_group():
    """Telegram operations — connect, fetch, sync, listen."""
    pass


@tg_group.command("auth")
def tg_auth():
    """Interactive first-time authentication with Telegram."""
    success = asyncio.run(authenticate())
    if success:
        console.print("[green]✓[/green] Authentication successful. Run 'tg refresh' to sync.")
    else:
        console.print("[red]Authentication failed.[/red]")
        raise SystemExit(1)


@tg_group.command("chats")
@click.option("--type", "chat_type", help="Filter by type: user, group, supergroup, channel")
@structured_output_options
def tg_chats(chat_type: str | None, as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """List joined Telegram chats."""

    async def _run():
        async with connect() as client:
            return await list_chats(client, chat_type)

    chats = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if chats is None:
        return

    # Apply field filtering
    field_list = _parse_fields(fields, CHATS_DEFAULT_FIELDS)
    filtered_chats = [{k: c.get(k) for k in field_list if k in c} for c in chats]

    # Pre-computed aggregate: total count
    payload = {
        "count": len(chats),
        "total": len(chats),
        "chats": filtered_chats,
    }

    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        # Add contextual help hints for TOON/JSON/YAML output
        if as_toon or as_json or as_yaml:
            for hint in get_help_hints("chats"):
                click.echo(f"help: {hint}")
        return

    if not chats:
        console.print("[yellow]0 chats found.[/yellow]")
        return

    table = Table(title="Telegram Chats")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Type", style="cyan")
    table.add_column("Unread", justify="right")

    for c in chats:
        table.add_row(str(c["id"]), c["name"], c["type"], str(c["unread"]))

    console.print(table)
    console.print(f"\nTotal: {len(chats)} chats")

    # Contextual disclosure
    for hint in get_help_hints("chats"):
        console.print(f"[dim]help: {hint}[/dim]")


@tg_group.command("history")
@click.argument("chat")
@click.option("-n", "--limit", default=1000, help="Max messages to fetch")
@structured_output_options
def tg_history(chat: str, limit: int, as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Fetch historical messages from CHAT (name, username, or numeric ID)."""

    async def _run():
        with MessageDB() as db:
            async with connect() as client:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    task = progress.add_task(f"Fetching messages from {chat}...", total=None)

                    def on_progress(count: int):
                        progress.update(task, description=f"Stored {count} messages...")

                    count = await fetch_history(
                        client, _parse_chat(chat), limit=limit, db=db, on_progress=on_progress
                    )
                return count

    count = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if count is None:
        return
    payload = {"stored": count, "chat": chat}
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    console.print(f"\n[green]\u2713[/green] Stored {count} messages from {chat}")


@tg_group.command("sync")
@click.argument("chat")
@click.option("-n", "--limit", default=5000, help="Max messages per sync")
@structured_output_options
def tg_sync(chat: str, limit: int, as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Incremental sync — fetch only new messages from CHAT."""

    async def _run():
        with MessageDB() as db:
            # Resolve chat_id to get last_msg_id
            chat_id = resolve_chat_id_or_print(db, chat, allow_missing=True)
            matches = db.find_chats(chat)
            if len(matches) > 1:
                resolve_chat_id_or_print(db, chat)
                return None
            last_id = db.get_last_msg_id(chat_id) if chat_id else 0
        if last_id:
            console.print(f"Syncing from msg_id > {last_id}...")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task_id = progress.add_task(f"Syncing {chat}...", total=None)

            def on_progress(count: int):
                progress.update(task_id, description=f"Stored {count} new messages...")

            return await sync_chat_dialog(chat, limit=limit, on_progress=on_progress)

    count = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if count is None:
        return
    payload = {"synced": count, "chat": chat}
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    console.print(f"\n[green]\u2713[/green] Synced {count} new messages from {chat}")


@tg_group.command("sync-all")
@click.option("-n", "--limit", default=5000, help="Max messages per chat")
@click.option(
    "--delay",
    default=1.0,
    show_default=True,
    help="Seconds between chat syncs (anti-ban). Set 0 to disable.",
)
@click.option(
    "--max-chats",
    default=None,
    type=int,
    help="Max number of chats to sync per run (default: all)",
)
@structured_output_options
def tg_sync_all(limit: int, delay: float, max_chats: int | None, as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Sync all currently available Telegram dialogs with a single connection."""

    async def _run():
        on_chat_done = None
        if not as_json and not as_yaml and not as_toon:
            console.print("Syncing all available chats...")

            def _on_chat_done(name: str, new_count: int, total: int):
                if new_count > 0:
                    console.print(f"  [green]✓[/green] {name}: +{new_count} (total: {total})")
                else:
                    console.print(f"  [dim]✓ {name}: no new messages[/dim]")

            on_chat_done = _on_chat_done

        return await sync_all_dialogs(
            limit=limit, on_chat_done=on_chat_done, delay=delay, max_chats=max_chats
        )

    results = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if results is None:
        return
    total_new = sum(results.values())
    payload = {"new_messages": total_new, "chats": len(results), "results": results}
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    console.print(f"\n[green]✓[/green] Synced {total_new} new messages across {len(results)} chats")


@tg_group.command("refresh")
@click.option("-n", "--limit", default=5000, help="Max messages per chat")
@click.option(
    "--delay",
    default=1.0,
    show_default=True,
    help="Seconds between chat syncs (anti-ban). Set 0 to disable.",
)
@click.option(
    "--max-chats",
    default=None,
    type=int,
    help="Max number of chats to sync per run (default: all)",
)
@structured_output_options
def tg_refresh(limit: int, delay: float, max_chats: int | None, as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Refresh the local cache from all current Telegram dialogs."""

    async def _run():
        on_chat_done = None
        if not as_json and not as_yaml and not as_toon:
            console.print("Refreshing local cache...")

            def _on_chat_done(name: str, new_count: int, total: int):
                if new_count > 0:
                    console.print(f"  [green]✓[/green] {name}: +{new_count} (total: {total})")
                else:
                    console.print(f"  [dim]✓ {name}: no new messages[/dim]")

            on_chat_done = _on_chat_done

        return await sync_all_dialogs(
            limit=limit, on_chat_done=on_chat_done, delay=delay, max_chats=max_chats
        )

    results = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if results is None:
        return
    total_new = sum(results.values())
    updated = [
        name
        for name, count in sorted(results.items(), key=lambda item: (-item[1], item[0]))
        if count > 0
    ]
    payload = {
        "new_messages": total_new,
        "chats": len(results),
        "updated_chats": updated,
        "results": results,
    }
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return

    console.print(f"\n[green]✓[/green] Refreshed {len(results)} chats, {total_new} new messages.")
    if updated:
        console.print(f"[dim]Most recently updated: {', '.join(updated[:5])}[/dim]")


@tg_group.command("listen")
@click.argument("chats", nargs=-1)
@click.option("--persist", is_flag=True, help="Reconnect automatically if the connection drops")
@click.option(
    "--retry-seconds",
    default=5,
    show_default=True,
    help="Reconnect delay when using --persist",
)
def tg_listen(chats: tuple[str, ...], persist: bool, retry_seconds: int):
    """Real-time listener for new messages. Optionally specify CHATS to filter."""
    parsed: list[str | int] | None = None
    if chats:
        parsed = []
        for c in chats:
            try:
                parsed.append(int(c))
            except ValueError:
                parsed.append(c)

    async def _run_once():
        async with connect() as client:
            return await listen(client, chats=parsed)

    while True:
        try:
            result = asyncio.run(_run_once())
        except click.ClickException:
            raise
        except Exception as exc:
            if not persist:
                raise
            console.print(
                f"[yellow]Listener disconnected: {exc}. Retrying in {retry_seconds}s...[/yellow]"
            )
            time.sleep(retry_seconds)
            continue

        if not persist or result == "stopped":
            break

        console.print(
            f"[yellow]Listener disconnected. Reconnecting in {retry_seconds}s...[/yellow]"
        )
        time.sleep(retry_seconds)


@tg_group.command("info")
@click.argument("chat")
@structured_output_options
def tg_info(chat: str, as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Show detailed info about CHAT."""

    async def _run():
        async with connect() as client:
            return await get_chat_info(client, _parse_chat(chat))

    info = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if info is None:
        return
    if not info:
        console.print(f"[red]Could not find chat: {chat}[/red]")
        return

    # Apply field filtering if specified
    if fields:
        field_list = _parse_fields(fields, list(info.keys()))
        filtered_info = {k: info.get(k) for k in field_list if k in info}
        payload = filtered_info
    else:
        payload = info

    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        for hint in get_help_hints("info"):
            click.echo(f"help: {hint}")
        return

    table = Table(title="Chat Info", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")

    for k, v in info.items():
        table.add_row(k, v)

    console.print(table)
    for hint in get_help_hints("info"):
        console.print(f"[dim]help: {hint}[/dim]")


@tg_group.command("whoami")
@structured_output_options
def tg_whoami(as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Show current logged-in user info."""

    async def _run():
        async with connect() as client:
            me = await client.get_me()
            return me

    me = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if me is None:
        return

    info = _telegram_user_payload(me)

    # Apply field filtering
    if fields:
        field_list = _parse_fields(fields, USER_DEFAULT_FIELDS)
        filtered_info = {k: info.get(k) for k in field_list if k in info}
        payload = {"user": filtered_info}
    else:
        payload = {"user": info}

    if emit_structured(success_payload(payload), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        for hint in get_help_hints("whoami"):
            click.echo(f"help: {hint}")
        return

    name = " ".join(p for p in [me.first_name, me.last_name] if p)
    table = Table(title=f"👤 {name}")
    table.add_column("Field", style="bold cyan")
    table.add_column("Value", style="green")
    table.add_row("ID", str(me.id))
    table.add_row("Name", name)
    if me.username:
        table.add_row("Username", f"@{me.username}")
    if me.phone:
        table.add_row("Phone", f"+{me.phone}")

    console.print(table)
    for hint in get_help_hints("whoami"):
        console.print(f"[dim]help: {hint}[/dim]")


@tg_group.command("status")
@structured_output_options
def tg_status(as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Show Telegram authentication status."""

    async def _run():
        async with connect() as client:
            me = await client.get_me()
            return {
                "authenticated": True,
                "id": me.id,
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
                "username": me.username or "",
                "phone": me.phone or "",
            }

    info = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if info is None:
        return

    user = {key: value for key, value in info.items() if key != "authenticated"}
    if emit_structured(
        success_payload({"authenticated": True, "user": user}),
        as_json=as_json,
        as_yaml=as_yaml,
        as_toon=as_toon,
    ):
        for hint in get_help_hints("status"):
            click.echo(f"help: {hint}")
        return

    name = " ".join(part for part in [info["first_name"], info["last_name"]] if part).strip()
    console.print(f"[green]✓[/green] Authenticated as [bold]{name or info['id']}[/bold]")
    if info["username"]:
        console.print(f"[dim]@{info['username']}[/dim]")
    for hint in get_help_hints("status"):
        console.print(f"[dim]help: {hint}[/dim]")


@tg_group.command("send")
@click.argument("chat")
@click.argument("message")
@click.option("-r", "--reply", type=int, default=None, help="Message ID to reply to")
@click.option("--no-preview", is_flag=True, help="Disable link preview")
@structured_output_options
def tg_send(
    chat: str,
    message: str,
    reply: int | None,
    no_preview: bool,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool,
    fields: str | None,
):
    """Send a MESSAGE to CHAT (name, username, or numeric ID)."""

    async def _run():
        async with connect() as client:
            msg = await client.send_message(
                _parse_chat(chat),
                message,
                reply_to=reply,
                link_preview=not no_preview,
            )
            return msg

    msg = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if msg is None:
        return
    payload = {"sent": True, "msg_id": msg.id, "chat": chat}
    if reply is not None:
        payload["reply_to"] = reply
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    console.print(f"[green]\u2713[/green] Message sent (id: {msg.id})")


@tg_group.command("edit")
@click.argument("chat")
@click.argument("msg_id", type=int)
@click.argument("new_text")
@click.option("--no-preview", is_flag=True, help="Disable link preview")
@structured_output_options
def tg_edit(chat: str, msg_id: int, new_text: str, no_preview: bool, as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Edit a previously sent message. CHAT MSG_ID NEW_TEXT."""

    async def _run():
        async with connect() as client:
            return await client.edit_message(
                _parse_chat(chat),
                msg_id,
                new_text,
                link_preview=not no_preview,
            )

    result = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if result is None:
        return
    # Note: _run returns None on auth error, but edit doesn't return a value
    # The payload is emitted only on success
    payload = {"edited": True, "msg_id": msg_id, "chat": chat}
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    console.print(f"[green]\u2713[/green] Message {msg_id} edited")


@tg_group.command("delete")
@click.argument("chat")
@click.argument("msg_ids", nargs=-1, type=int, required=True)
@structured_output_options
def tg_delete(chat: str, msg_ids: tuple[int, ...], as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Delete one or more messages. CHAT MSG_ID [MSG_ID ...]."""

    async def _run():
        async with connect() as client:
            await client.delete_messages(_parse_chat(chat), list(msg_ids))

    result = asyncio.run(_run_with_auth(_run(), as_json=as_json, as_yaml=as_yaml, as_toon=as_toon))
    if result is None:
        return
    # Note: _run returns None on auth error, but delete doesn't return a value
    # The payload is emitted only on success
    payload = {"deleted": True, "msg_ids": list(msg_ids), "chat": chat}
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    console.print(f"[green]\u2713[/green] Deleted {len(msg_ids)} message(s)")