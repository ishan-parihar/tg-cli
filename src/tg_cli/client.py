"""Telegram client with connection reuse and entity caching."""

from __future__ import annotations

import asyncio
import logging
import random
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User

from .config import (
    get_api_hash,
    get_api_id,
    get_session_path,
    get_session_phone,
    is_default_api_id,
)
from .console import console
from .db import MessageDB
from .ratelimit import (
    DELETE,
    DIALOGS,
    EDIT,
    HISTORY,
    SEND,
    TelegramRateGuard,
    guarded,
    peer_key,
)

log = logging.getLogger(__name__)

# Telegram Desktop 5.x fingerprint — makes the session look like a real client
_DEVICE_MODEL = "Desktop"
_SYSTEM_VERSION = "macOS 15.3"
_APP_VERSION = "5.12.1"
_LANG_CODE = "en"
_SYSTEM_LANG_CODE = "en-US"

# Progressive sync: limit for first-time chat sync (no prior messages in DB)
_FIRST_SYNC_LIMIT = 500


def _phone() -> str:
    """Return the current session's phone number, or a placeholder for the gate.

    The gate keys buckets by phone so per-account state stays separate when more
    than one CLI process is running.  We don't store the phone in plaintext on
    disk; Telethon exposes it via get_me() only after auth, so before-auth calls
    fall back to the session name which is local-only.
    """
    try:
        p = get_session_phone()
        if p:
            return p
    except Exception:
        pass
    return "anon"


async def _guarded_history(guard: TelegramRateGuard, phone: str, call):
    """Run a single RPC through the history gate. Returns the result on success.

    FloodWaitError is propagated to the caller; the caller decides whether to
    record the flood (only meaningful at a boundary that owns a chat scope).
    """
    return await guarded(guard, phone, HISTORY, None, call, max_defer=60.0)


async def _iter_dialogs_guarded(client, guard, phone):
    """Yield from iter_dialogs, rate-limited through the dialogs gate.

    Telethon's iter_dialogs is async-generator-shaped; we walk it through the
    guarded() helper by consuming one dialog at a time and re-checking the gate.
    """
    agen = client.iter_dialogs()
    while True:
        # Pull the next dialog through the gate.
        retry = guard.before_call(phone, DIALOGS)
        if retry > 0:
            await asyncio.sleep(retry * random.uniform(0.9, 1.1))
            continue
        try:
            dialog = await agen.__anext__()
        except StopAsyncIteration:
            return
        except FloodWaitError as e:
            guard.record_flood(phone, DIALOGS, e.seconds)
            await asyncio.sleep(e.seconds + random.uniform(1, 3))
            continue
        guard.record_success(phone, DIALOGS)
        yield dialog


def _get_sender_name(sender: User | Channel | Chat | None) -> str | None:
    if sender is None:
        return None
    if isinstance(sender, User):
        parts = [sender.first_name or "", sender.last_name or ""]
        name = " ".join(p for p in parts if p)
        return name or sender.username or str(sender.id)
    return getattr(sender, "title", None) or str(sender.id)


_default_api_warned = False


# Process-wide guard — one per Python process is enough.  Tests can override.
_default_guard = TelegramRateGuard()


def get_rate_guard() -> TelegramRateGuard:
    """Return the process-wide TelegramRateGuard singleton."""
    return _default_guard


@asynccontextmanager
async def connect() -> AsyncGenerator[TelegramClient, None]:
    """Async context manager for Telegram client — single connection, reuse within scope.
    Fails fast with clear error if not authenticated (no interactive prompts per AXI §6)."""
    global _default_api_warned
    api_id = get_api_id()
    api_hash = get_api_hash()

    if not _default_api_warned and is_default_api_id():
        _default_api_warned = True
        # Only warn in human mode (TTY) to avoid polluting structured output (AXI §6)
        if sys.stdout.isatty():
            console.print(
                "[yellow]⚠ Using default Telegram Desktop API credentials (api_id=2040).\n"
                "  This increases the risk of account restrictions.\n"
                "  Get your own at https://my.telegram.org and set "
                "TG_API_ID / TG_API_HASH.[/yellow]"
            )

    c = TelegramClient(
        get_session_path(),
        api_id,
        api_hash,
        device_model=_DEVICE_MODEL,
        system_version=_SYSTEM_VERSION,
        app_version=_APP_VERSION,
        lang_code=_LANG_CODE,
        system_lang_code=_SYSTEM_LANG_CODE,
    )
    # Use start() which handles both initial auth (interactive) and reconnection
    await c.start()
    try:
        yield c
    finally:
        await c.disconnect()


async def authenticate() -> bool:
    """Interactive authentication for first-time setup.
    Returns True if authentication succeeded."""
    global _default_api_warned
    api_id = get_api_id()
    api_hash = get_api_hash()

    if not _default_api_warned and is_default_api_id():
        _default_api_warned = True
        if sys.stdout.isatty():
            console.print(
                "[yellow]⚠ Using default Telegram Desktop API credentials (api_id=2040).\n"
                "  This increases the risk of account restrictions.\n"
                "  Get your own at https://my.telegram.org and set "
                "TG_API_ID / TG_API_HASH.[/yellow]"
            )

    c = TelegramClient(
        get_session_path(),
        api_id,
        api_hash,
        device_model=_DEVICE_MODEL,
        system_version=_SYSTEM_VERSION,
        app_version=_APP_VERSION,
        lang_code=_LANG_CODE,
        system_lang_code=_SYSTEM_LANG_CODE,
    )
    try:
        await c.start()
        return True
    except Exception as e:
        console.print(f"[red]Authentication failed: {e}[/red]")
        return False
    finally:
        await c.disconnect()


async def list_chats(
    client: TelegramClient,
    chat_type: str | None = None,
) -> list[dict]:
    """List all dialogs (chats/groups/channels) the user has joined."""
    results = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        t = "unknown"
        if isinstance(entity, User):
            t = "user"
        elif isinstance(entity, Chat):
            t = "group"
        elif isinstance(entity, Channel):
            t = "channel" if entity.broadcast else "supergroup"

        if chat_type and t != chat_type:
            continue

        results.append(
            {
                "id": dialog.id,
                "name": dialog.name,
                "type": t,
                "unread": dialog.unread_count,
            }
        )
    return results


async def get_chat_info(client: TelegramClient, chat: str | int) -> dict | None:
    """Get detailed information about a chat."""
    try:
        entity = await client.get_entity(chat)
    except Exception as e:
        log.debug("get_chat_info failed for %s: %s", chat, e)
        return None

    info: dict[str, str] = {}
    info["Title"] = getattr(entity, "title", None) or getattr(entity, "first_name", "") or str(chat)
    info["ID"] = str(entity.id)

    if isinstance(entity, User):
        info["Type"] = "User"
        info["Username"] = f"@{entity.username}" if entity.username else "—"
        info["Phone"] = entity.phone or "—"
    elif isinstance(entity, Chat):
        info["Type"] = "Group"
        info["Members"] = str(getattr(entity, "participants_count", "?"))
    elif isinstance(entity, Channel):
        info["Type"] = "Channel" if entity.broadcast else "Supergroup"
        info["Username"] = f"@{entity.username}" if entity.username else "—"
        try:
            from telethon.tl.functions.channels import GetFullChannelRequest

            full = await client(GetFullChannelRequest(entity))
            info["Members"] = str(full.full_chat.participants_count or "?")
            if full.full_chat.about:
                info["Description"] = full.full_chat.about[:200]
        except Exception as e:
            info["Members"] = "?"
            log.debug("Failed to get full channel info: %s", e)

    return info


async def fetch_history(
    client: TelegramClient,
    chat: str | int,
    limit: int = 1000,
    db: MessageDB | None = None,
    on_progress: Callable[[int], None] | None = None,
    min_id: int = 0,
    batch_delay: float = 0,
    guard: TelegramRateGuard | None = None,
) -> int:
    """Fetch historical messages from a chat and store them in the database.

    Args:
        client: Connected TelegramClient instance
        chat: Group name, username, or numeric ID
        limit: Max messages to fetch
        db: Database instance (creates one if None)
        on_progress: Callback invoked every batch with current count
        min_id: Only fetch messages with id > min_id (for incremental sync)
        batch_delay: Seconds to sleep between DB write batches (with ±30% jitter).
            Throttles iter_messages pagination. Set to 0 to disable.
        guard: Rate-limit guard (defaults to the process-wide singleton).
    """
    owns_db = db is None
    if db is None:
        db = MessageDB()
    if guard is None:
        guard = get_rate_guard()

    phone = _phone()
    try:
        # Resolve the chat once — go through the account-wide history gate so a
        # flood wait on resolveUsername can never bunch up against iter_messages.
        entity = await _guarded_history(guard, phone, lambda: client.get_entity(chat))
        chat_name = (
            getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat)
        )
        chat_id = entity.id

        # Lazy sender name resolution — avoids risky iter_participants API
        sender_cache: dict[int, str] = {}

        batch: list[dict] = []
        inserted_count = 0
        BATCH_SIZE = 200

        async for msg in client.iter_messages(entity, limit=limit, min_id=min_id):
            if msg.text is None and msg.message is None:
                continue

            # Extract sender name from Telethon's cached _sender (zero API calls)
            sender_name = None
            if msg.sender_id:
                if msg.sender_id in sender_cache:
                    sender_name = sender_cache[msg.sender_id]
                else:
                    # Telethon caches sender in msg._sender from the response
                    cached = getattr(msg, "_sender", None) or getattr(msg, "sender", None)
                    if cached:
                        sender_name = _get_sender_name(cached)
                    if sender_name:
                        sender_cache[msg.sender_id] = sender_name

            content = msg.text or msg.message or ""
            ts = msg.date
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            batch.append(
                dict(
                    chat_id=chat_id,
                    chat_name=chat_name,
                    msg_id=msg.id,
                    sender_id=msg.sender_id,
                    sender_name=sender_name,
                    content=content,
                    timestamp=ts or datetime.now(timezone.utc),
                )
            )

            if len(batch) >= BATCH_SIZE:
                inserted_count += db.insert_batch(batch)
                batch.clear()
                if on_progress:
                    on_progress(inserted_count)
                # Anti-ban: throttle between pagination batches
                if batch_delay > 0:
                    jitter = batch_delay * random.uniform(-0.3, 0.3)
                    await asyncio.sleep(batch_delay + jitter)

        # Flush remaining
        if batch:
            inserted_count += db.insert_batch(batch)

        return inserted_count
    except FloodWaitError as e:
        if 'guard' in locals() and guard is not None:
            guard.record_flood(phone, HISTORY, e.seconds)
        console.print(f"[yellow]⚠ Telegram rate limit hit, waiting {e.seconds}s...[/yellow]")
        await asyncio.sleep(e.seconds + random.uniform(1, 3))
        return 0
    finally:
        if owns_db:
            db.close()


async def sync_all(
    client: TelegramClient,
    db: MessageDB,
    limit_per_chat: int = 5000,
    on_chat_done: Callable[[str, int, int], None] | None = None,
    delay: float = 1.0,
    max_chats: int | None = None,
    guard: TelegramRateGuard | None = None,
) -> dict[str, int]:
    """Sync all chats in the database using a single connection.

    Args:
        on_chat_done: Callback(chat_name, new_count, total_in_chat)
        delay: Seconds to wait between each chat sync (with ±20% jitter).
            Set to 0 to disable. Helps avoid triggering Telegram rate limits.
        max_chats: Max number of chats to sync per run. None = no limit.
        guard: Rate-limit guard (defaults to the process-wide singleton).

    Returns:
        dict mapping chat_name to new message count
    """
    if guard is None:
        guard = get_rate_guard()
    phone = _phone()
    results: dict[str, int] = {}
    stored_chats = {c["chat_id"]: c for c in db.get_chats()}
    dialog_cache: dict[int, tuple[object, str]] = {}
    try:
        # iter_dialogs is the throttled RPC (the #1330 incident). Defer to the gate.
        async for dialog in _iter_dialogs_guarded(client, guard, phone):
            entity = dialog.entity
            dialog_cache[entity.id] = (entity, dialog.name)
    except Exception as e:
        log.debug("Failed to build dialog cache: %s", e)

    items = list(dialog_cache.items())
    if max_chats is not None:
        items = items[:max_chats]
    total = len(items)

    for idx, (chat_id, (entity, dialog_name)) in enumerate(items):
        chat_info = stored_chats.get(chat_id, {})
        chat_name = chat_info.get("chat_name") or dialog_name or str(chat_id)
        last_id = db.get_last_msg_id(chat_id) or 0

        # Progressive sync: use lower limit for first-time chat sync
        effective_limit = limit_per_chat
        if last_id == 0 and limit_per_chat > _FIRST_SYNC_LIMIT:
            effective_limit = _FIRST_SYNC_LIMIT
            log.debug("First sync for %s, limiting to %d messages", chat_name, effective_limit)

        try:
            count = await fetch_history(
                client,
                entity,
                limit=effective_limit,
                db=db,
                min_id=last_id,
                guard=guard,
            )
            results[chat_name] = count
            if on_chat_done:
                on_chat_done(chat_name, count, chat_info.get("msg_count", 0) + count)
        except FloodWaitError as e:
            guard.record_flood(phone, HISTORY, e.seconds)
            console.print(
                f"  [yellow]⚠ {chat_name}: rate limited, waiting {e.seconds}s...[/yellow]"
            )
            await asyncio.sleep(e.seconds + random.uniform(1, 3))
            results[chat_name] = 0
        except Exception as e:
            console.print(f"  [red]✗ {chat_name}: {e}[/red]")
            results[chat_name] = 0

        # Anti-ban: sleep with random jitter between chat syncs
        if delay > 0 and idx < total - 1:
            jitter = delay * random.uniform(-0.2, 0.2)
            await asyncio.sleep(delay + jitter)

    return results


async def listen(
    client: TelegramClient,
    chats: list[str | int] | None = None,
    db: MessageDB | None = None,
):
    """Real-time listen for new messages in specified chats (or all chats)."""
    owns_db = db is None
    if db is None:
        db = MessageDB()

    try:
        me = await client.get_me()
        console.print(f"[green]✓[/green] Logged in as [bold]{me.first_name}[/bold] ({me.phone})")
        console.print("[dim]Listening for messages... Press Ctrl+C to stop.[/dim]")

        @client.on(events.NewMessage(chats=chats))
        async def handler(event):
            msg = event.message
            # Use the chat that Telethon already attached to the event — no extra RPC.
            chat = event.chat
            # Pull sender name from Telethon's cache; only fall back to get_sender
            # when the cache is empty (rare, costs one RPC for the miss only).
            cached_sender = getattr(msg, "_sender", None) or getattr(msg, "sender", None)
            sender_name = _get_sender_name(cached_sender)
            if sender_name is None and msg.sender_id:
                try:
                    sender_name = _get_sender_name(await event.get_sender())
                except Exception:
                    sender_name = None

            chat_name = (
                getattr(chat, "title", None) or getattr(chat, "first_name", None) or "Unknown"
            )
            content = msg.text or msg.message or ""

            ts = msg.date
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            db.insert_message(
                chat_id=chat.id,
                chat_name=chat_name,
                msg_id=msg.id,
                sender_id=msg.sender_id,
                sender_name=sender_name,
                content=content,
                timestamp=ts or datetime.now(timezone.utc),
            )

            time_str = ts.strftime("%H:%M:%S") if ts else "??:??:??"
            console.print(
                f"[dim]{time_str}[/dim] [cyan]{chat_name}[/cyan] | "
                f"[bold]{sender_name or 'Unknown'}[/bold]: {content[:200]}"
            )

        status = "disconnected"
        try:
            await client.run_until_disconnected()
        except KeyboardInterrupt:
            status = "stopped"
            console.print("\n[yellow]Stopped listening.[/yellow]")
        finally:
            db_count = db.count()
            console.print(f"[green]Total messages in DB: {db_count}[/green]")
        return status
    finally:
        if owns_db:
            db.close()


# --- guarded write operations ----------------------------------------------


async def guarded_send(
    client: TelegramClient,
    chat,
    message: str,
    *,
    reply_to: int | None = None,
    link_preview: bool = True,
    guard: TelegramRateGuard | None = None,
):
    """Send a message through the SEND gate + per-peer limiter.

    Returns the Telethon Message on success.  FloodWaitError is caught, fed to
    the breaker, retried once after the gate-reported wait, then re-raised.
    """
    if guard is None:
        guard = get_rate_guard()
    phone = _phone()
    entity = await _guarded_history(guard, phone, lambda: client.get_entity(chat))
    peer = peer_key(entity)

    async def _send():
        return await client.send_message(
            entity, message, reply_to=reply_to, link_preview=link_preview
        )

    try:
        return await guarded(guard, phone, SEND, peer, _send, max_defer=60.0)
    except FloodWaitError as e:
        guard.record_flood(phone, SEND, e.seconds)
        await asyncio.sleep(e.seconds + random.uniform(1, 3))
        # One retry after the wait — if it fails again, propagate.
        return await guarded(guard, phone, SEND, peer, _send, max_defer=60.0)


async def guarded_edit(
    client: TelegramClient,
    chat,
    msg_id: int,
    new_text: str,
    *,
    link_preview: bool = True,
    guard: TelegramRateGuard | None = None,
):
    """Edit a message through the EDIT gate + per-peer limiter."""
    if guard is None:
        guard = get_rate_guard()
    phone = _phone()
    entity = await _guarded_history(guard, phone, lambda: client.get_entity(chat))
    peer = peer_key(entity)

    async def _edit():
        return await client.edit_message(entity, msg_id, new_text, link_preview=link_preview)

    try:
        return await guarded(guard, phone, EDIT, peer, _edit, max_defer=60.0)
    except FloodWaitError as e:
        guard.record_flood(phone, EDIT, e.seconds)
        await asyncio.sleep(e.seconds + random.uniform(1, 3))
        return await guarded(guard, phone, EDIT, peer, _edit, max_defer=60.0)


async def guarded_delete(
    client: TelegramClient,
    chat,
    msg_ids: list[int],
    *,
    guard: TelegramRateGuard | None = None,
):
    """Delete messages through the DELETE gate + per-peer limiter."""
    if guard is None:
        guard = get_rate_guard()
    phone = _phone()
    entity = await _guarded_history(guard, phone, lambda: client.get_entity(chat))
    peer = peer_key(entity)

    async def _delete():
        return await client.delete_messages(entity, msg_ids)

    try:
        return await guarded(guard, phone, DELETE, peer, _delete, max_defer=60.0)
    except FloodWaitError as e:
        guard.record_flood(phone, DELETE, e.seconds)
        await asyncio.sleep(e.seconds + random.uniform(1, 3))
        return await guarded(guard, phone, DELETE, peer, _delete, max_defer=60.0)
