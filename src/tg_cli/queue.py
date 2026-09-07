"""Durable JSONL job queue — the IPC between agents and the daemon.

Agents enqueue writes (send/edit/delete/refresh) and get an immediate
``{queued: true, job_id}`` payload.  The daemon (the only process that opens
the Telegram session) drains pending jobs through the guarded send paths.

Corrupt lines are skipped, never fatal.  ``running`` jobs orphaned by a killed
worker replay as ``pending`` on the next drain.

Concurrency: every read/write goes through a process-wide advisory lock
(``fcntl.flock`` on POSIX, ``msvcrt`` on Windows) so multiple CLI processes
can enqueue without trampling each other.  The lock also gates the drain, so
the daemon and an ad-hoc ``queue drain`` cannot interleave.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid

from .config import get_data_dir

log = logging.getLogger(__name__)

_QUEUE_FILE = "jobs.jsonl"
_TMP_FILE = "jobs.jsonl.lock.tmp"
MAX_PENDING = 100
DONE_RETENTION_DAYS = 7

# Job kinds the daemon knows how to execute.
SEND = "send"
EDIT = "edit"
DELETE = "delete"
REFRESH = "refresh"
KINDS = (SEND, EDIT, DELETE, REFRESH)


class QueueFullError(Exception):
    """Too many pending jobs — caller should back off, not enqueue."""


def _path():
    p = get_data_dir() / "queue"
    p.mkdir(parents=True, exist_ok=True)
    return p / _QUEUE_FILE


def _lock_path():
    return _path().with_name("jobs.jsonl.lock")


# Cross-platform advisory file lock.  Returns a context manager that holds
# the lock for the duration of the read-modify-write critical section.
if sys.platform == "win32":
    import msvcrt

    class _Lock:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            # Block until the lock is acquired (single-byte lock at offset 0).
            while True:
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
                    return self
                except OSError:
                    time.sleep(0.01)

        def __exit__(self, *exc):
            try:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
else:
    import fcntl

    class _Lock:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _critical_section():
    """Acquire the queue-wide advisory lock for one read-modify-write."""
    return _Lock(open(_lock_path(), "a+b", buffering=0))


def _read_all() -> list[dict]:
    """Read all jobs under the queue lock, skipping corrupt lines."""
    path = _path()
    if not path.is_file():
        return []
    jobs = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                job = json.loads(line)
            except json.JSONDecodeError:
                log.warning("queue: skipping corrupt line")
                continue
            if isinstance(job, dict) and job.get("id") and job.get("kind"):
                jobs.append(job)
    except OSError as e:
        log.warning("queue read failed: %s", e)
    return jobs


def _write_all(jobs: list[dict]) -> None:
    """Atomic write: temp file + fsync + rename + fsync directory."""
    path = _path()
    tmp = path.with_name(_TMP_FILE)
    body = "\n".join(json.dumps(j, ensure_ascii=False) for j in jobs)
    payload = body + "\n" if body else ""
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    # Durability: fsync the directory so the rename is durable.
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY)
        os.fsync(dfd)
        os.close(dfd)
    except OSError:
        pass


def enqueue(kind: str, payload: dict, *, not_before: float = 0.0) -> dict:
    """Append a job.  Returns the job dict.  Raises QueueFullError / ValueError."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind: {kind}")
    with _critical_section():
        jobs = _read_all()
        if sum(1 for j in jobs if j.get("status") == "pending") >= MAX_PENDING:
            raise QueueFullError(f"queue full ({MAX_PENDING} pending)")
        now = time.time()
        job = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "payload": payload,
            "status": "pending",
            "attempts": 0,
            "not_before": not_before,
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
        }
        jobs.append(job)
        _write_all(jobs)
    return job


def pending(now: float | None = None) -> list[dict]:
    """Jobs ready to run, oldest first."""
    t = time.time() if now is None else now
    return sorted(
        [j for j in _read_all() if j.get("status") == "pending" and j.get("not_before", 0) <= t],
        key=lambda j: j.get("created_at", 0),
    )


def status_counts() -> dict[str, int]:
    counts = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    total = 0
    oldest_pending: float | None = None
    for j in _read_all():
        total += 1
        s = j.get("status")
        if s in counts:
            counts[s] += 1
        if s == "pending":
            c = j.get("created_at", 0)
            oldest_pending = c if oldest_pending is None else min(oldest_pending, c)
    return {"total": total, **counts, "oldest_pending_at": oldest_pending}


def replay_orphans() -> int:
    """Reset ``running`` jobs to ``pending`` (worker died mid-drain)."""
    with _critical_section():
        jobs = _read_all()
        n = 0
        for j in jobs:
            if j.get("status") == "running":
                j["status"] = "pending"
                j["updated_at"] = time.time()
                n += 1
        if n:
            _write_all(jobs)
    return n


def mark(
    job_id: str,
    status: str,
    *,
    result: dict | None = None,
    error: str | None = None,
    not_before: float | None = None,
    attempts: int | None = None,
) -> bool:
    """Update a job's status.  Returns False when the job is gone."""
    with _critical_section():
        jobs = _read_all()
        for j in jobs:
            if j.get("id") == job_id:
                j["status"] = status
                j["updated_at"] = time.time()
                if result is not None:
                    j["result"] = result
                if error is not None:
                    j["error"] = error
                if not_before is not None:
                    j["not_before"] = not_before
                if attempts is not None:
                    j["attempts"] = attempts
                _write_all(jobs)
                return True
    return False


def prune_done(older_than_days: float = DONE_RETENTION_DAYS) -> int:
    """Drop done/failed jobs older than the retention window.  Returns count."""
    cutoff = time.time() - older_than_days * 86400
    with _critical_section():
        jobs = _read_all()
        keep = [
            j
            for j in jobs
            if not (
                j.get("status") in ("done", "failed") and j.get("updated_at", 0) < cutoff
            )
        ]
        dropped = len(jobs) - len(keep)
        if dropped:
            _write_all(keep)
    return dropped


def clear_done() -> int:
    """Drop all done/failed jobs regardless of age.  Returns count."""
    with _critical_section():
        jobs = _read_all()
        keep = [j for j in jobs if j.get("status") not in ("done", "failed")]
        dropped = len(jobs) - len(keep)
        if dropped:
            _write_all(keep)
    return dropped
