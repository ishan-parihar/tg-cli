"""Tests for the rate-limit gate — pure stdlib, no network."""

from __future__ import annotations

import pytest

from tg_cli.ratelimit import (
    DIALOGS,
    HISTORY,
    SEND,
    RateLimitSpec,
    TelegramRateGuard,
    TelegramRateLimitedError,
    guarded,
    peer_key,
)


def test_peer_key_classifies_entities():
    class Channel:
        def __init__(self, id, broadcast=True):
            self.id = id
            self.broadcast = broadcast

    class Chat:
        def __init__(self, id):
            self.id = id

    class User:
        def __init__(self, id):
            self.id = id

    assert peer_key(Channel(123, broadcast=True)) == "channel:123"
    assert peer_key(Channel(456, broadcast=False)) == "supergroup:456"
    assert peer_key(Chat(789)) == "chat:789"
    assert peer_key(User(11)) == "user:11"
    assert peer_key(12345) == "id:12345"
    assert peer_key("@durov") == "username:durov"
    assert peer_key(None) == "unknown"


def test_account_bucket_refuses_after_max_calls():
    g = TelegramRateGuard(
        category_limits={DIALOGS: RateLimitSpec(max_calls=1, window_sec=60.0)},
    )
    assert g.before_call("+15550001", DIALOGS) == 0.0
    assert g.before_call("+15550001", DIALOGS) > 0.0


def test_peer_refusal_does_not_consume_account_slot():
    g = TelegramRateGuard(
        category_limits={SEND: RateLimitSpec(max_calls=5, window_sec=60.0)},
        peer_limits={"send:user": RateLimitSpec(max_calls=1, window_sec=60.0)},
    )
    # First peer-send OK.
    assert g.before_call("+1", SEND, peer="user:42") == 0.0
    # Second send to same peer refused — but account slot was refunded.
    retry_peer = g.before_call("+1", SEND, peer="user:42")
    assert retry_peer > 0.0
    # Third send to a DIFFERENT peer must still be allowed (account slot intact).
    assert g.before_call("+1", SEND, peer="user:99") == 0.0


def test_breaker_opens_after_threshold_floods():
    g = TelegramRateGuard(breaker_threshold=2, breaker_cooldown=60.0)
    g.record_flood("+1", HISTORY, 1.0)
    g.record_flood("+1", HISTORY, 1.0)
    assert g.is_suspended("+1", HISTORY) is True
    # Suspended calls don't consume the bucket — they just defer.
    assert g.before_call("+1", HISTORY) > 0.0


def test_record_success_resets_flood_count():
    g = TelegramRateGuard(breaker_threshold=3, breaker_cooldown=60.0)
    g.record_flood("+1", HISTORY, 1.0)
    g.record_success("+1", HISTORY)
    g.record_flood("+1", HISTORY, 1.0)
    assert g.is_suspended("+1", HISTORY) is False


@pytest.mark.asyncio
async def test_guarded_defers_until_clear_then_calls():
    g = TelegramRateGuard(
        category_limits={HISTORY: RateLimitSpec(max_calls=1, window_sec=0.05)},
    )
    calls = []

    async def factory():
        calls.append("called")
        return "ok"

    # First call goes through.
    assert await guarded(g, "+1", HISTORY, None, factory, max_defer=1.0) == "ok"
    # Second call must defer past the window then run.
    assert await guarded(g, "+1", HISTORY, None, factory, max_defer=1.0) == "ok"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_guarded_raises_when_budget_exceeded():
    g = TelegramRateGuard(
        category_limits={HISTORY: RateLimitSpec(max_calls=1, window_sec=60.0)},
    )

    async def factory():
        return "x"

    await guarded(g, "+1", HISTORY, None, factory, max_defer=0.0)
    with pytest.raises(TelegramRateLimitedError):
        await guarded(g, "+1", HISTORY, None, factory, max_defer=0.05)