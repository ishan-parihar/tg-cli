"""Per-session cooldowns for heavy operations.

Stores timestamps of the last successful ``refresh`` / ``sync-all`` run so we
can refuse re-runs more frequently than the configured cooldown.  State lives in
a small JSON file in the data dir; no schema migration needed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import get_data_dir

_COOLDOWNS_FILE = "cooldowns.json"
# Minimum seconds between full dialog sweeps.  README recommends ≤1–2/day; 2h
# is a comfortable floor that catches "refresh every 15 min" mistakes without
# blocking legitimate hourly polling.
DEFAULT_REFRESH_COOLDOWN_SEC = 2 * 3600


def _cooldowns_path() -> Path:
    return get_data_dir() / _COOLDOWNS_FILE


def _read() -> dict[str, float]:
    p = _cooldowns_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except Exception:
        return {}


def _write(d: dict[str, float]) -> None:
    p = _cooldowns_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, p)


def last_run(op: str) -> float:
    """Unix timestamp of the last successful run of ``op``, or 0."""
    return _read().get(op, 0.0)


def mark_run(op: str) -> None:
    """Stamp ``op`` as successfully run now."""
    d = _read()
    d[op] = time.time()
    _write(d)


def refresh_allowed(cooldown_sec: float = DEFAULT_REFRESH_COOLDOWN_SEC) -> tuple[bool, float]:
    """Return (allowed, seconds_remaining). ``allowed=True`` when the cooldown elapsed."""
    last = last_run("refresh")
    if last == 0.0:
        return True, 0.0
    remaining = cooldown_sec - (time.time() - last)
    return remaining <= 0, max(0.0, remaining)