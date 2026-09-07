"""Persistent client-daemon: live updates + queue drain, sole session owner.

The daemon is the ONLY process that opens the Telegram session file (Telethon
sessions cannot be shared between processes).  Agents keep calling the same
CLI: reads hit local SQLite directly, writes route through the JSONL queue
which the daemon drains on its own connection.

Run: ``tg daemon run`` (foreground, systemd) or ``tg daemon start`` (detached).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import events
from telethon.errors import FloodWaitError

from . import queue as queue_mod
from . import throttle
from .client import (
    _get_sender_name,
    connect,
    get_rate_guard,
    guarded_delete,
    guarded_edit,
    guarded_send,
    sync_all,
)
from .config import get_data_dir
from .db import MessageDB
from .ratelimit import TelegramRateLimitedError

log = logging.getLogger(__name__)

HEARTBEAT_FILE = "daemon.json"
HEARTBEAT_FRESH_SEC = 120
_STARTUP_LIMIT = 500


def heartbeat_path():
    return get_data_dir() / HEARTBEAT_FILE


def write_heartbeat(started_at: float) -> None:
    p = heartbeat_path()
    tmp = p.with_suffix(".json.tmp")
    import json

    tmp.write_text(
        json.dumps({"pid": os.getpid(), "started_at": started_at, "updated_at": time.time()})
    )
    os.replace(tmp, p)


def read_heartbeat() -> dict | None:
    import json

    p = heartbeat_path()
    if not p.is_file():
        return None
    try:
        hb = json.loads(p.read_text() or "{}")
    except (OSError, ValueError):
        return None
    return hb if isinstance(hb, dict) and hb.get("pid") else None


def is_daemon_alive(fresh_sec: float = HEARTBEAT_FRESH_SEC) -> bool:
    """True when a daemon heartbeat is fresh and its pid still exists."""
    hb = read_heartbeat()
    if not hb:
        return False
    if time.time() - hb.get("updated_at", 0) > fresh_sec:
        return False
    try:
        os.kill(int(hb["pid"]), 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # permission/platform quirk — trust the fresh timestamp
    return True


def _store_event(db: MessageDB, chat_id, chat_name, msg, sender_name) -> None:
    ts = msg.date
    if ts and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    db.insert_message(
        chat_id=chat_id,
        chat_name=chat_name,
        msg_id=msg.id,
        sender_id=msg.sender_id,
        sender_name=sender_name,
        content=msg.text or msg.message or "[media]",
        timestamp=ts or datetime.now(timezone.utc),
    )


def _event_sender_name(msg) -> str | None:
    cached = getattr(msg, "_sender", None) or getattr(msg, "sender", None)
    return _get_sender_name(cached)


async def _execute_job(client, db: MessageDB, job: dict) -> dict:
    """Run one queue job on the daemon's connection.  Returns a result dict."""
    kind, p = job["kind"], job.get("payload") or {}
    if kind == queue_mod.SEND:
        msg = await guarded_send(
            client,
            p["chat"],
            p["message"],
            reply_to=p.get("reply_to"),
            link_preview=p.get("link_preview", True),
        )
        return {"msg_id": msg.id}
    if kind == queue_mod.EDIT:
        await guarded_edit(client, p["chat"], p["msg_id"], p["new_text"])
        return {"msg_id": p["msg_id"]}
    if kind == queue_mod.DELETE:
        await guarded_delete(client, p["chat"], p["msg_ids"])
        return {"deleted": len(p["msg_ids"])}
    if kind == queue_mod.REFRESH:
        allowed, remaining = throttle.refresh_allowed()
        if not allowed:
            raise TelegramRateLimitedError("daemon", "refresh", remaining)
        results = await sync_all(
            client,
            db,
            limit_per_chat=int(p.get("limit", _STARTUP_LIMIT)),
            delay=float(p.get("delay", 1.0)),
        )
        throttle.mark_run("refresh")
        return {"new_messages": sum(results.values()), "chats": len(results)}
    raise ValueError(f"unknown job kind: {kind}")


async def drain_pending(client, db: MessageDB) -> dict[str, int]:
    """Execute all due jobs.  Never raises — failures are recorded on the job."""
    stats = {"done": 0, "failed": 0, "deferred": 0}
    for job in queue_mod.pending():
        queue_mod.mark(job["id"], "running", attempts=job.get("attempts", 0) + 1)
        try:
            result = await _execute_job(client, db, job)
        except TelegramRateLimitedError as e:
            queue_mod.mark(job["id"], "pending", not_before=time.time() + e.retry_after,
                           error=str(e))
            stats["deferred"] += 1
        except FloodWaitError as e:
            get_rate_guard().record_flood("daemon", "history", e.seconds)
            queue_mod.mark(job["id"], "pending",
                           not_before=time.time() + e.seconds + 5, error=str(e))
            stats["deferred"] += 1
        except Exception as e:
            queue_mod.mark(job["id"], "failed", error=f"{type(e).__name__}: {e}")
            stats["failed"] += 1
            log.warning("job %s failed: %s", job["id"], e)
        else:
            queue_mod.mark(job["id"], "done", result=result)
            stats["done"] += 1
    return stats


async def run_daemon(
    chats: list | None = None,
    interval: float = 5.0,
    sync_on_start: bool = True,
    startup_limit: int = _STARTUP_LIMIT,
) -> str:
    """Connect once, catch up, then serve updates + queue until cancelled."""
    started_at = time.time()
    queue_mod.replay_orphans()
    with MessageDB() as db:
        async with connect() as client:
            if sync_on_start:
                try:
                    await sync_all(client, db, limit_per_chat=startup_limit, delay=1.0)
                    throttle.mark_run("refresh")
                except Exception as e:
                    log.warning("startup sync failed: %s", e)

            @client.on(events.NewMessage(chats=chats))
            async def on_new(event):
                msg = event.message
                chat = event.chat
                chat_id = getattr(chat, "id", None) or event.chat_id
                chat_name = (
                    getattr(chat, "title", None) or getattr(chat, "first_name", None) or "Unknown"
                )
                _store_event(db, chat_id, chat_name, msg, _event_sender_name(msg))

            @client.on(events.MessageEdited(chats=chats))
            async def on_edit(event):
                msg = event.message
                chat = event.chat
                chat_id = getattr(chat, "id", None) or event.chat_id
                content = msg.text or msg.message or "[media]"
                if not db.update_message(chat_id, msg.id, content):
                    chat_name = (
                        getattr(chat, "title", None)
                        or getattr(chat, "first_name", None)
                        or "Unknown"
                    )
                    _store_event(db, chat_id, chat_name, msg, _event_sender_name(msg))

            @client.on(events.MessageDeleted)
            async def on_delete(event):
                for mid in event.deleted_ids or []:
                    try:
                        db.delete_message(event.chat_id, mid)
                    except Exception as e:
                        log.debug("delete handler failed: %s", e)

            write_heartbeat(started_at)
            try:
                while True:
                    try:
                        await drain_pending(client, db)
                    except Exception as e:
                        log.warning("drain failed: %s", e)
                    write_heartbeat(started_at)
                    await asyncio.sleep(interval)
            except (asyncio.CancelledError, KeyboardInterrupt):
                return "stopped"
    return "stopped"


def start_detached(interval: float = 5.0) -> dict:
    """Spawn ``tg daemon run`` in the background.  Returns {started, pid/log}."""
    if is_daemon_alive():
        return {"started": False, "reason": "already running"}
    binary = shutil.which("tg") or sys.argv[0]
    log_path = get_data_dir() / "daemon.log"
    logf = open(log_path, "ab")
    proc = subprocess.Popen(
        [binary, "daemon", "run", "--interval", str(interval)],
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return {"started": True, "pid": proc.pid, "log": str(log_path)}


def stop() -> dict:
    """SIGTERM the daemon from its heartbeat pid."""
    hb = read_heartbeat()
    if not hb or not is_daemon_alive():
        return {"stopped": False, "reason": "not running"}
    try:
        os.kill(int(hb["pid"]), signal.SIGTERM)
    except OSError as e:
        return {"stopped": False, "reason": str(e)}
    return {"stopped": True, "pid": hb["pid"]}


# --- systemd user unit management (Linux) ---------------------------------

_UNIT_NAME = "tg-cli-daemon.service"
_LINGERING_MIN_UID = 1000


def _systemd_available() -> bool:
    """True when systemctl is on PATH and the user can run user units."""
    if sys.platform != "linux":
        return False
    if shutil.which("systemctl") is None:
        return False
    # systemd-run user instance needs either root OR an existing user manager.
    try:
        r = subprocess.run(
            ["systemctl", "--user", "status"], capture_output=True, timeout=2
        )
        # Exit 0 means user manager is running; 1/3 with "Failed to connect"
        # means no manager (no lingering, or no systemd user instance).
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _user_unit_dir() -> Path:
    """Return the systemd user unit directory for the current user."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if base:
        return Path(base).expanduser() / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def _unit_path() -> Path:
    return _user_unit_dir() / _UNIT_NAME


_UNIT_TEMPLATE = """\
[Unit]
Description=tg-cli persistent Telegram client (live updates + queue delivery)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={binary} daemon run --interval {interval}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def install_systemd(interval: float = 5.0, enable_linger: bool = True) -> dict:
    """Install the systemd user unit, enable it, start it.  Returns a payload.

    Side effects:
    - writes the unit file (idempotent — refreshed on each call)
    - enables linger if the user can (so the daemon survives logout)
    - runs ``systemctl --user daemon-reload``
    - enables + starts the unit
    """
    if not _systemd_available():
        return {"installed": False, "reason": "systemd user manager not available"}
    binary = shutil.which("tg") or sys.argv[0]
    unit_path = _unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        _UNIT_TEMPLATE.format(binary=shlex_quote(binary), interval=interval)
    )
    os.chmod(unit_path, 0o644)
    # Enable linger so the user manager survives logout (so the daemon runs).
    if enable_linger and os.geteuid() >= _LINGERING_MIN_UID:
        subprocess.run(
            ["loginctl", "enable-linger", str(os.geteuid())],
            capture_output=True,
            check=False,
        )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", _UNIT_NAME], check=False)
    start = subprocess.run(
        ["systemctl", "--user", "restart", _UNIT_NAME], check=False
    )
    return {
        "installed": True,
        "unit": str(unit_path),
        "started": start.returncode == 0,
        "linger_enabled": enable_linger,
        "binary": binary,
    }


def shlex_quote(s: str) -> str:
    """Quote a binary path for systemd's ExecStart without requiring shlex."""
    import shlex

    return shlex.quote(s)


def uninstall_systemd() -> dict:
    """Stop, disable, and remove the systemd user unit."""
    if not _systemd_available():
        return {"uninstalled": False, "reason": "systemd user manager not available"}
    subprocess.run(["systemctl", "--user", "disable", "--now", _UNIT_NAME], check=False)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    unit_path = _unit_path()
    removed = False
    if unit_path.exists():
        unit_path.unlink()
        removed = True
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return {"uninstalled": removed, "unit": str(unit_path)}
