"""Telegram subcommands — send, edit, delete, and more."""

import asyncio
import random
import time

import click
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .. import daemon as daemon_mod
from .. import queue as queue_mod
from .. import throttle
from ..client import (
    authenticate,
    connect,
    fetch_history,
    get_chat_info,
    guarded_delete,
    guarded_edit,
    guarded_send,
    list_chats,
    listen,
)
from ..console import console
from ..db import MessageDB
from ..ratelimit import TelegramRateLimitedError
from ._chat import _parse_chat, resolve_chat_id_or_print
from ._output import (
    emit_error,
    emit_structured,
    get_help_hints,
    structured_output_options,
    success_payload,
)
from ._sync import sync_all_dialogs, sync_chat_dialog


def _soft(
    code: str,
    message: str,
    details: dict | None,
    *,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool,
) -> None:
    """Emit a soft failure: structured payload for agents, dim line for humans.

    Always exits 0 — agents must be able to read the error, not catch a crash.
    Callers distinguish None (soft failure) from data by checking the return.
    """
    if emit_error(
        code, message, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon, details=details
    ):
        return
    console.print(f"[yellow]⚠ {message}[/yellow]")


def _enqueue_write(
    kind: str,
    payload: dict,
    *,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool,
    not_before: float = 0.0,
) -> None:
    """Enqueue a write job and report ``{queued: true, job_id}`` (exit 0)."""
    try:
        job = queue_mod.enqueue(kind, payload, not_before=not_before)
    except queue_mod.QueueFullError as exc:
        _soft("queue_full", str(exc), None,
              as_json=as_json, as_yaml=as_yaml, as_toon=as_toon)
        return
    out: dict = {"queued": True, "job_id": job["id"], "kind": kind}
    if not_before:
        out["not_before_in_seconds"] = max(0, round(not_before - time.time()))
    if emit_structured(out, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    console.print(
        f"[green]✓[/green] Queued {kind} (job {job['id']}) — the daemon will deliver it."
    )


async def _run_with_auth(coro, *, as_json: bool, as_yaml: bool, as_toon: bool):
    """Run an async operation that requires auth, handling auth errors softly."""
    try:
        return await coro
    except RuntimeError as exc:
        if "Not authenticated" in str(exc):
            # Soft failure (exit 0): None means "no data", and the payload
            # on stdout carries the auth_required code for agents.
            _soft("auth_required", str(exc), None,
                  as_json=as_json, as_yaml=as_yaml, as_toon=as_toon)
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
@structured_output_options
def tg_auth(as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Interactive first-time authentication with Telegram."""
    success = asyncio.run(authenticate())
    if success:
        payload = {"authenticated": True}
        if emit_structured(success_payload(payload),
                            as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
            return
        console.print("[green]✓[/green] Authentication successful. Run 'tg refresh' to sync.")
    else:
        # Soft failure so agent loops can read the error instead of crashing.
        _soft("auth_failed", "Authentication failed.", None,
              as_json=as_json, as_yaml=as_yaml, as_toon=as_toon)


@tg_group.command("chats")
@click.option("--type", "chat_type", help="Filter by type: user, group, supergroup, channel")
@structured_output_options
def tg_chats(
    chat_type: str | None, as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None
):
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

    # Structured output: the chat list itself (SCHEMA.md: query commands return lists).
    payload = filtered_chats

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
def tg_history(
    chat: str, limit: int, as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None
):
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
def tg_sync_all(
    limit: int,
    delay: float,
    max_chats: int | None,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool,
    fields: str | None,
):
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
@click.option(
    "--force",
    is_flag=True,
    help="Bypass the per-session cooldown (default 2h). Off by default to discourage hammering.",
)
@click.option(
    "--queue",
    "queue_if_limited",
    is_flag=True,
    help="Enqueue the refresh when the cooldown is active instead of failing.",
)
@structured_output_options
def tg_refresh(
    limit: int,
    delay: float,
    max_chats: int | None,
    force: bool,
    queue_if_limited: bool,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool,
    fields: str | None,
):
    """Refresh the local cache from all current Telegram dialogs."""
    flags = dict(as_json=as_json, as_yaml=as_yaml, as_toon=as_toon)
    if not force:
        allowed, remaining = throttle.refresh_allowed()
        if not allowed:
            if queue_if_limited or daemon_mod.is_daemon_alive():
                _enqueue_write(queue_mod.REFRESH, {"limit": limit}, **flags)
                return
            hours = remaining / 3600
            msg = (
                f"refresh ran recently; next one allowed in {hours:.1f}h. "
                "Use --force to override or --queue to enqueue."
            )
            _soft("refresh_cooldown", msg,
                  {"retry_after_seconds": int(remaining)}, **flags)
            return

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
    throttle.mark_run("refresh")
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

    backoff = retry_seconds
    while True:
        try:
            result = asyncio.run(_run_once())
        except click.ClickException:
            raise
        except Exception as exc:
            if not persist:
                raise
            console.print(
                f"[yellow]Listener disconnected: {exc}. Retrying in {backoff}s...[/yellow]"
            )
            time.sleep(backoff)
            # Exponential backoff with ±30% jitter, capped at 5 minutes.
            backoff = min(backoff * 2, 300)
            backoff *= random.uniform(0.7, 1.3)
            continue

        if not persist or result == "stopped":
            break

        # Reset backoff on a clean exit before reconnecting.
        backoff = retry_seconds
        console.print(
            f"[yellow]Listener disconnected. Reconnecting in {backoff}s...[/yellow]"
        )
        time.sleep(backoff)


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
        # _run_with_auth soft-fails on auth error, so None here means
        # get_chat_info reported the chat as not found.
        console.print(f"[red]Could not find chat: {chat}[/red]")
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
@click.option(
    "--queue",
    "queue_if_limited",
    is_flag=True,
    help="Enqueue when rate-limited instead of failing.",
)
@structured_output_options
def tg_send(
    chat: str,
    message: str,
    reply: int | None,
    no_preview: bool,
    queue_if_limited: bool,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool,
    fields: str | None,
):
    """Send a MESSAGE to CHAT (name, username, or numeric ID)."""
    flags = dict(as_json=as_json, as_yaml=as_yaml, as_toon=as_toon)
    job_payload = {
        "chat": chat,
        "message": message,
        "reply_to": reply,
        "link_preview": not no_preview,
    }
    # The daemon owns the session file — never open a second connection.
    if daemon_mod.is_daemon_alive():
        _enqueue_write(queue_mod.SEND, job_payload, **flags)
        return

    async def _run():
        async with connect() as client:
            return await guarded_send(
                client,
                _parse_chat(chat),
                message,
                reply_to=reply,
                link_preview=not no_preview,
            )

    try:
        msg = asyncio.run(_run_with_auth(_run(), **flags))
    except TelegramRateLimitedError as exc:
        if queue_if_limited:
            _enqueue_write(queue_mod.SEND, job_payload,
                           not_before=time.time() + exc.retry_after, **flags)
            return
        _soft("rate_limited", str(exc),
              {"retry_after_seconds": round(exc.retry_after)}, **flags)
        return
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
@click.option(
    "--queue",
    "queue_if_limited",
    is_flag=True,
    help="Enqueue when rate-limited instead of failing.",
)
@structured_output_options
def tg_edit(
    chat: str,
    msg_id: int,
    new_text: str,
    no_preview: bool,
    queue_if_limited: bool,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool,
    fields: str | None,
):
    """Edit a previously sent message. CHAT MSG_ID NEW_TEXT."""
    flags = dict(as_json=as_json, as_yaml=as_yaml, as_toon=as_toon)
    job_payload = {"chat": chat, "msg_id": msg_id, "new_text": new_text}
    if daemon_mod.is_daemon_alive():
        _enqueue_write(queue_mod.EDIT, job_payload, **flags)
        return

    async def _run():
        async with connect() as client:
            return await guarded_edit(
                client,
                _parse_chat(chat),
                msg_id,
                new_text,
                link_preview=not no_preview,
            )

    try:
        result = asyncio.run(_run_with_auth(_run(), **flags))
    except TelegramRateLimitedError as exc:
        if queue_if_limited:
            _enqueue_write(queue_mod.EDIT, job_payload,
                           not_before=time.time() + exc.retry_after, **flags)
            return
        _soft("rate_limited", str(exc),
              {"retry_after_seconds": round(exc.retry_after)}, **flags)
        return
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
@click.option(
    "--queue",
    "queue_if_limited",
    is_flag=True,
    help="Enqueue when rate-limited instead of failing.",
)
@structured_output_options
def tg_delete(
    chat: str,
    msg_ids: tuple[int, ...],
    queue_if_limited: bool,
    as_json: bool,
    as_yaml: bool,
    as_toon: bool,
    fields: str | None,
):
    """Delete one or more messages. CHAT MSG_ID [MSG_ID ...]."""
    flags = dict(as_json=as_json, as_yaml=as_yaml, as_toon=as_toon)
    job_payload = {"chat": chat, "msg_ids": list(msg_ids)}
    if daemon_mod.is_daemon_alive():
        _enqueue_write(queue_mod.DELETE, job_payload, **flags)
        return

    async def _run():
        async with connect() as client:
            await guarded_delete(client, _parse_chat(chat), list(msg_ids))

    try:
        result = asyncio.run(_run_with_auth(_run(), **flags))
    except TelegramRateLimitedError as exc:
        if queue_if_limited:
            _enqueue_write(queue_mod.DELETE, job_payload,
                           not_before=time.time() + exc.retry_after, **flags)
            return
        _soft("rate_limited", str(exc),
              {"retry_after_seconds": round(exc.retry_after)}, **flags)
        return
    if result is None:
        return
    # Note: _run returns None on auth error, but delete doesn't return a value
    # The payload is emitted only on success
    payload = {"deleted": True, "msg_ids": list(msg_ids), "chat": chat}
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    console.print(f"[green]\u2713[/green] Deleted {len(msg_ids)} message(s)")


@tg_group.group("queue")
def queue_group():
    """Durable write queue — enqueue while limited, the daemon delivers."""
    pass


@queue_group.command("status")
@structured_output_options
def queue_status(as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Show pending/running/done/failed job counts."""
    counts = queue_mod.status_counts()
    if emit_structured(counts, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    table = Table(title="Queue", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for k, v in counts.items():
        table.add_row(k, str(v))
    console.print(table)


@queue_group.command("clear")
@structured_output_options
def queue_clear(as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Drop finished (done/failed) jobs. Pending jobs are kept."""
    cleared = queue_mod.clear_done()
    payload = {"cleared": cleared}
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    console.print(f"[green]✓[/green] Cleared {cleared} finished job(s)")


@queue_group.command("drain")
@structured_output_options
def queue_drain(as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Run all due jobs now with your own connection (cron fallback).

    Refuses when the daemon is alive — it already drains the queue and a
    second session owner would fight it for the session file.
    """
    flags = dict(as_json=as_json, as_yaml=as_yaml, as_toon=as_toon)
    if daemon_mod.is_daemon_alive():
        _soft("daemon_running",
              "Daemon is alive — it drains the queue itself; 'queue drain' refused.",
              None, **flags)
        return

    async def _run():
        with MessageDB() as db:
            async with connect() as client:
                return await daemon_mod.drain_pending(client, db)

    stats = asyncio.run(_run_with_auth(_run(), **flags))
    if stats is None:
        return
    if emit_structured(stats, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    console.print(f"[green]✓[/green] Drained: {stats}")


@tg_group.group("daemon")
def daemon_group():
    """Persistent client — live updates + queue delivery, sole session owner."""
    pass


@daemon_group.command("status")
@structured_output_options
def daemon_status(as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Show whether the daemon heartbeat is fresh."""
    hb = daemon_mod.read_heartbeat()
    alive = daemon_mod.is_daemon_alive()
    payload = {"alive": alive, "heartbeat": hb}
    if emit_structured(payload, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    if alive:
        console.print(f"[green]✓[/green] Daemon alive (pid {hb['pid']})")
    else:
        console.print("[yellow]○ Daemon not running. Start with 'tg daemon start'.[/yellow]")


@daemon_group.command("run")
@click.option("--interval", default=5.0, show_default=True, help="Seconds between queue drains.")
@click.option("--no-sync", is_flag=True, help="Skip the startup catch-up sync.")
@click.option("--limit", default=500, show_default=True,
              help="Max messages per chat on startup sync.")
def daemon_run(interval: float, no_sync: bool, limit: int):
    """Run the daemon in the foreground (systemd, docker, tmux)."""
    console.print("[dim]Daemon starting — Ctrl+C to stop.[/dim]")
    asyncio.run(daemon_mod.run_daemon(
        interval=interval, sync_on_start=not no_sync, startup_limit=limit,
    ))


@daemon_group.command("start")
@click.option("--interval", default=5.0, show_default=True, help="Seconds between queue drains.")
@structured_output_options
def daemon_start(interval: float, as_json: bool, as_yaml: bool, as_toon: bool,
                 fields: str | None):
    """Spawn the daemon detached (logs to <data-dir>/daemon.log)."""
    result = daemon_mod.start_detached(interval=interval)
    if emit_structured(result, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    if result.get("started"):
        console.print(f"[green]✓[/green] Daemon started (pid {result['pid']})")
    else:
        console.print(f"[yellow]○ {result.get('reason', 'not started')}[/yellow]")


@daemon_group.command("stop")
@structured_output_options
def daemon_stop(as_json: bool, as_yaml: bool, as_toon: bool, fields: str | None):
    """Stop the detached daemon."""
    result = daemon_mod.stop()
    if emit_structured(result, as_json=as_json, as_yaml=as_yaml, as_toon=as_toon):
        return
    if result.get("stopped"):
        console.print("[green]✓[/green] Daemon stopped")
    else:
        console.print(f"[yellow]○ {result.get('reason', 'not running')}[/yellow]")
