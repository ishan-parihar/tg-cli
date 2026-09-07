"""Proactive rate-limit gate, per-peer limiter, and reactive circuit breaker.

Three layers, calibrated conservative against Telegram's observed abuse signals:
1. Sliding-window account gate keyed by (phone, category) — stops the call BEFORE
   Telegram opens a FLOOD_WAIT.  Defaults: dialogs 1/min, history 30/min, send 20/min.
2. Per-peer send limiter — Telegram rate-limits sending per peer too (~1/s to a
   private chat, ~20/min into the same group/channel).  Peer refusal does NOT
   consume the account-wide slot, so a burst aimed at one peer cannot burn budget.
3. Reactive circuit breaker — once a (phone, category) pair trips a threshold of
   flood waits, suspend it for a cooldown so we don't re-trigger the same ban.

All state is in-process, single-event-loop, plain deques.  No DB, no deps.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# --- operation categories (one bucket per (phone, category)) -----------------

DIALOGS = "dialogs"
HISTORY = "history"
SEND = "send"
EDIT = "edit"
DELETE = "delete"
ADMIN = "admin"


@dataclass(frozen=True)
class RateLimitSpec:
    """Sliding-window cap: at most ``max_calls`` invocations per ``window_sec``."""

    max_calls: int
    window_sec: float


# Conservative defaults.  Override via env TG_RATE_<CATEGORY>_<MAX>_<WINDOW>
# or by constructing TelegramRateGuard(...) explicitly.
DEFAULT_SPECS: dict[str, RateLimitSpec] = {
    DIALOGS: RateLimitSpec(max_calls=1, window_sec=60.0),  # the #1330/iter_dialogs incident
    HISTORY: RateLimitSpec(max_calls=30, window_sec=60.0),
    SEND: RateLimitSpec(max_calls=20, window_sec=60.0),
    EDIT: RateLimitSpec(max_calls=15, window_sec=60.0),
    DELETE: RateLimitSpec(max_calls=15, window_sec=60.0),
    ADMIN: RateLimitSpec(max_calls=5, window_sec=60.0),
}

# Per-peer send pacing — only "send" gets a peer bucket by default.
# Roughly: 1 msg / 5s to a private chat, ~20/min into the same group/channel.
DEFAULT_PEER_SEND_SPEC: dict[str, RateLimitSpec] = {
    "send:user": RateLimitSpec(max_calls=1, window_sec=5.0),
    "send:channel": RateLimitSpec(max_calls=20, window_sec=60.0),
    "send:chat": RateLimitSpec(max_calls=20, window_sec=60.0),
    "send:supergroup": RateLimitSpec(max_calls=20, window_sec=60.0),
}


# --- per-peer key derivation (purely local; never a network call) ------------


def peer_key(entity: object | None) -> str:
    """Derive a stable peer key from any Telethon entity / input peer / id / string.

    Returns one of: "user:<id>", "channel:<id>", "chat:<id>", "supergroup:<id>",
    "username:<handle>", "id:<str>", or "unknown".

    No network calls — only attribute access on the object you pass in.
    """
    if entity is None:
        return "unknown"
    # Telethon entities expose .__class__.__name__; reuse it to avoid importing types.
    cls = entity.__class__.__name__ if hasattr(entity, "__class__") else ""
    # Channel objects: distinguish broadcast channels vs supergroups via .broadcast
    if cls in ("Channel", "ChannelFull"):
        kind = "channel" if getattr(entity, "broadcast", False) else "supergroup"
        return f"{kind}:{entity.id}"
    if cls == "Chat" or cls == "ChatFull":
        return f"chat:{entity.id}"
    if cls == "User" or cls == "UserFull":
        return f"user:{entity.id}"
    # Input peer variants: InputPeerUser/Channel/Chat/PeerUser/...
    if cls.startswith("InputPeer") or cls.startswith("Peer"):
        kind = cls.replace("InputPeer", "").replace("Peer", "").lower()
        if hasattr(entity, "user_id"):
            return f"user:{entity.user_id}"
        if hasattr(entity, "channel_id"):
            return f"channel:{entity.channel_id}"
        if hasattr(entity, "chat_id"):
            return f"chat:{entity.chat_id}"
        return f"{kind or 'peer'}:{getattr(entity, 'id', '?')}"
    # Plain ints / strings (checked BEFORE generic .id fallback)
    if isinstance(entity, int):
        return f"id:{entity}"
    if isinstance(entity, str):
        s = entity.lstrip("@").strip()
        return f"username:{s}" if s else "unknown"
    if hasattr(entity, "id"):
        return f"id:{entity.id}"
    return "unknown"


# --- account-wide sliding window --------------------------------------------


@dataclass
class _Bucket:
    spec: RateLimitSpec
    times: deque[float] = field(default_factory=deque)


class TelegramRateGuard:
    """Proactive rate-limit gate + per-peer limiter + reactive breaker.

    One instance per process is enough — gates are stateless w.r.t. the Telegram
    client.  Pass it into ``connect()`` via the ``rate_guard`` keyword so every
    RPC site can defer to ``guard.before_call(phone, op, peer=None)``.
    """

    def __init__(
        self,
        category_limits: dict[str, RateLimitSpec] | None = None,
        peer_limits: dict[str, RateLimitSpec] | None = None,
        peer_max_buckets: int = 4096,
        breaker_threshold: int = 3,
        breaker_cooldown: float = 300.0,
    ):
        self._specs = dict(DEFAULT_SPECS)
        if category_limits:
            self._specs.update(category_limits)
        self._peer_specs = dict(DEFAULT_PEER_SEND_SPEC)
        if peer_limits:
            self._peer_specs.update(peer_limits)
        self._peer_max_buckets = peer_max_buckets

        # account buckets: (phone, category) -> _Bucket
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        # peer buckets: (phone, category, peer_key) -> _Bucket (LRU on size)
        self._peer_buckets: dict[tuple[str, str, str], _Bucket] = {}
        # breaker: (phone, category) -> suspended-until timestamp
        self._suspended: dict[tuple[str, str], float] = {}
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        # flood counts: (phone, category) -> consecutive count (resets on success)
        self._flood_counts: dict[tuple[str, str], int] = {}

    # ---- public API ---------------------------------------------------------

    def before_call(
        self,
        phone: str,
        category: str,
        peer: str | None = None,
    ) -> float:
        """Check rate limits BEFORE making a Telegram call.

        Returns ``retry_after`` (seconds) the caller should sleep before retrying.
        Returns 0.0 when the call is allowed to proceed immediately.

        Order of checks (cheapest first):
        1. breaker — if suspended, refuse without consuming a slot.
        2. account bucket.
        3. peer bucket (only if ``peer`` provided).
        """
        suspended_until = self._suspended.get((phone, category), 0.0)
        now = time.monotonic()
        if suspended_until > now:
            return suspended_until - now

        retry = self._try_acquire(self._buckets, (phone, category), self._specs.get(category))
        if retry > 0:
            return retry

        if peer:
            peer_spec_key = self._peer_spec_key(category, peer)
            if peer_spec_key:
                spec = self._peer_specs.get(peer_spec_key)
                if spec is not None:
                    retry = self._try_acquire(
                        self._peer_buckets,
                        (phone, category, peer),
                        spec,
                    )
                    if retry > 0:
                        # Refund the account slot — peer refusal must NOT consume it.
                        bucket = self._buckets.get((phone, category))
                        if bucket and bucket.times:
                            bucket.times.pop()
                        return retry
        return 0.0

    def record_flood(self, phone: str, category: str, retry_after: float) -> None:
        """Record that Telegram returned FLOOD_WAIT on this (phone, category)."""
        self._flood_counts[(phone, category)] = self._flood_counts.get((phone, category), 0) + 1
        count = self._flood_counts[(phone, category)]
        if count >= self._breaker_threshold:
            cooldown = max(retry_after, self._breaker_cooldown)
            self._suspended[(phone, category)] = time.monotonic() + cooldown
            log.warning(
                "rate-limit breaker OPEN phone=%s category=%s count=%d cooldown=%.0fs",
                _mask(phone),
                category,
                count,
                cooldown,
            )

    def record_success(self, phone: str, category: str) -> None:
        """Clear flood counter for (phone, category) after a successful call."""
        if self._flood_counts.get((phone, category)):
            self._flood_counts[(phone, category)] = 0

    def is_suspended(self, phone: str, category: str) -> bool:
        return self._suspended.get((phone, category), 0.0) > time.monotonic()

    def stats(self) -> dict[str, int]:
        return {
            "account_buckets": len(self._buckets),
            "peer_buckets": len(self._peer_buckets),
            "suspended_pairs": sum(
                1 for t in self._suspended.values() if t > time.monotonic()
            ),
        }

    # ---- internals ----------------------------------------------------------

    def _try_acquire(
        self,
        store: dict[tuple, _Bucket],
        key: tuple,
        spec: RateLimitSpec | None,
    ) -> float:
        if spec is None:
            return 0.0
        # max_calls=0 means "always refuse" — short-circuit before the bucket math.
        if spec.max_calls <= 0:
            return spec.window_sec
        now = time.monotonic()
        bucket = store.get(key)
        if bucket is None:
            # LRU cap for peer buckets only — account buckets are bounded by category count.
            if store is self._peer_buckets and len(store) >= self._peer_max_buckets:
                store.pop(next(iter(store)))
            bucket = _Bucket(spec=spec)
            store[key] = bucket
        # Drop entries outside the window.
        cutoff = now - spec.window_sec
        while bucket.times and bucket.times[0] < cutoff:
            bucket.times.popleft()
        if len(bucket.times) >= spec.max_calls:
            return max(0.0, bucket.times[0] + spec.window_sec - now)
        bucket.times.append(now)
        return 0.0

    def _peer_spec_key(self, category: str, peer: str) -> str | None:
        # peer keys look like "user:123", "channel:-100..", "chat:.."
        # We only attach peer limits to "send" by default.
        if category not in (SEND, EDIT):
            return None
        head = peer.split(":", 1)[0]
        if head in ("user", "channel", "chat", "supergroup"):
            return f"{category}:{head}"
        return None


# --- error types raised by the gate -----------------------------------------


class TelegramRateLimitedError(Exception):
    """Account-wide or peer rate limit — caller should defer or skip."""

    def __init__(self, phone: str, category: str, retry_after: float, peer: str | None = None):
        self.phone = phone
        self.category = category
        self.retry_after = retry_after
        self.peer = peer
        scope = f"peer={peer}" if peer else "account"
        super().__init__(
            f"Rate-limited ({scope}, category={category}, retry in {retry_after:.1f}s)"
        )


# --- helpers used at call sites --------------------------------------------


def _mask(phone: str) -> str:
    """Hide the middle of a phone number for log output."""
    if len(phone) <= 4:
        return "***"
    return phone[:3] + "***" + phone[-2:]


# --- bounded async helper that defers until the gate says go ----------------


async def guarded(
    guard: TelegramRateGuard,
    phone: str,
    category: str,
    peer: str | None,
    call: Callable[[], asyncio.Future],
    *,
    max_defer: float = 30.0,
) -> object:
    """Run ``call()`` after the gate clears, sleeping up to ``max_defer`` seconds.

    Raises TelegramRateLimitedError if the gate doesn't open within the budget.
    Calls do NOT auto-record flood/success — the caller wires that around the
    Telethon ``FloodWaitError`` it actually receives.
    """
    waited = 0.0
    while True:
        retry = guard.before_call(phone, category, peer)
        if retry <= 0:
            return await call()
        if waited + retry > max_defer:
            raise TelegramRateLimitedError(phone, category, retry, peer)
        sleep_for = min(retry, max_defer - waited)
        # Tiny floor jitter so a herd of waiting callers doesn't wake in lockstep.
        await asyncio.sleep(sleep_for * random.uniform(0.9, 1.1))
        waited += sleep_for