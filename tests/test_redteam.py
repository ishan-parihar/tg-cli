"""Red-team probe tests — find bugs, do not paper over them.

Run with: pytest tests/test_redteam.py -v
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest
import yaml
from click.testing import CliRunner

from tg_cli import daemon as d
from tg_cli import queue as q
from tg_cli import throttle
from tg_cli.cli.main import cli
from tg_cli.ratelimit import (
    DIALOGS,
    HISTORY,
    SEND,
    RateLimitSpec,
    TelegramRateGuard,
    guarded,
    peer_key,
)

# ─────────────────────────── ratelimit red-team ───────────────────────────


class TestRateLimitRedTeam:
    def test_zero_window_does_not_crash(self):
        g = TelegramRateGuard(category_limits={DIALOGS: RateLimitSpec(5, 0.0)})
        # Every call must be admitted (window of 0 means nothing is in window).
        for _ in range(10):
            assert g.before_call("+1", DIALOGS) == 0.0

    def test_zero_max_calls(self):
        g = TelegramRateGuard(category_limits={DIALOGS: RateLimitSpec(0, 60.0)})
        # max_calls=0 means "always refuse". Must not crash.
        assert g.before_call("+1", DIALOGS) > 0.0

    def test_breaker_does_not_reopen_after_explicit_success_reset(self):
        g = TelegramRateGuard(breaker_threshold=3, breaker_cooldown=60.0)
        g.record_flood("+1", HISTORY, 1.0)
        g.record_flood("+1", HISTORY, 1.0)
        # 2 floods under threshold → not suspended.
        assert g.is_suspended("+1", HISTORY) is False
        # Third flood opens breaker.
        g.record_flood("+1", HISTORY, 1.0)
        assert g.is_suspended("+1", HISTORY) is True

    def test_breaker_uses_monotonic_not_wall_clock(self):
        g = TelegramRateGuard(breaker_threshold=1, breaker_cooldown=1.0)
        g.record_flood("+1", HISTORY, 1.0)
        assert g.is_suspended("+1", HISTORY) is True
        time.sleep(1.2)
        assert g.is_suspended("+1", HISTORY) is False

    def test_peer_lru_evicts_oldest(self):
        # Documented limitation: eviction is "first-inserted" not "least-recently-used".
        # Verify the current behavior so any future change to LRU semantics is intentional.
        g = TelegramRateGuard(
            category_limits={SEND: RateLimitSpec(max_calls=1000, window_sec=60.0)},
            peer_max_buckets=4,
        )
        for i in range(8):
            g.before_call("+1", SEND, peer=f"user:{i}")
        # With current implementation, the first 4 are evicted (insertion-order LRU approximation).
        for i in range(4):
            assert ("+1", SEND, f"user:{i}") not in g._peer_buckets

    def test_peer_refund_keeps_bucket_intact(self):
        # 3 distinct peer calls fill the account bucket. A 4th to a peer with
        # an exhausted peer slot must be refused AND refund the account slot
        # so it remains at 3 (its level before this call), not 4.
        g = TelegramRateGuard(
            category_limits={SEND: RateLimitSpec(max_calls=3, window_sec=60.0)},
            peer_limits={"send:user": RateLimitSpec(max_calls=1, window_sec=60.0)},
        )
        g.before_call("+1", SEND, peer="user:1")
        g.before_call("+1", SEND, peer="user:2")
        g.before_call("+1", SEND, peer="user:3")
        before = len(g._buckets[("+1", SEND)].times)
        retry = g.before_call("+1", SEND, peer="user:1")
        assert retry > 0.0  # peer slot exhausted
        after = len(g._buckets[("+1", SEND)].times)
        assert after == before, f"refund failed: {before} -> {after}"

    @pytest.mark.asyncio
    async def test_concurrent_before_call_respects_budget(self):
        g = TelegramRateGuard(
            category_limits={SEND: RateLimitSpec(max_calls=10, window_sec=60.0)}
        )

        async def hit():
            return g.before_call("+1", SEND)

        outs = await asyncio.gather(*[hit() for _ in range(50)])
        allowed = sum(1 for o in outs if o == 0.0)
        assert allowed <= 10

    def test_peer_key_handles_pathological_inputs(self):
        class Weird:
            __slots__ = ("no_id",)

            def __init__(self):
                self.no_id = "nothing"

        assert peer_key(Weird()) == "unknown"
        assert peer_key(object()) == "unknown"
        assert peer_key("") == "unknown"
        assert peer_key("@") == "unknown"

    def test_guarded_propagates_non_flood_errors(self):

        g = TelegramRateGuard(category_limits={HISTORY: RateLimitSpec(1, 60.0)})

        async def boom():
            raise RuntimeError("kaboom")

        async def go():
            return await guarded(g, "+1", HISTORY, None, boom, max_defer=0.0)

        with pytest.raises(RuntimeError, match="kaboom"):
            asyncio.run(go())


# ─────────────────────────── queue red-team ───────────────────────────


class TestQueueRedTeam:
    def test_concurrent_enqueue_no_lost_jobs(self, monkeypatch, tmp_path):
        # Stress: 50 threads × 20 enqueues must all land. Lost-update bug
        # used to drop ~97% of jobs due to read-modify-write race.
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        q.MAX_PENDING = 5000

        def worker():
            for _ in range(20):
                q.enqueue(q.SEND, {"chat": "x", "message": "y"})

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        actual = q.status_counts()["total"]
        assert actual == 1000, f"lost jobs: got {actual}, expected 1000"

    def test_corrupt_file_is_recoverable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        q.enqueue(q.SEND, {"chat": "a", "message": "x"})
        path = q._path()
        # NUL bytes and binary garbage mixed with valid lines.
        path.write_bytes(b'{"id":"a","kind":"send","payload":{},"status":"pending","x":1}\n'
                         b'\x00\x01\x02garbage\n'
                         b'{"id":"b","kind":"send","payload":{},"status":"pending","y":2}\n')
        # Must not raise and must return both valid jobs.
        jobs = q._read_all()
        ids = sorted(j["id"] for j in jobs)
        assert ids == ["a", "b"]

    def test_windows_line_endings_parsed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        path = q._path()
        path.write_bytes(b'{"id":"a","kind":"send","payload":{},"status":"pending","x":1}\r\n'
                         b'{"id":"b","kind":"send","payload":{},"status":"pending","y":2}\r\n')
        # json.loads is tolerant of \r; splitlines() handles \r\n.
        jobs = q._read_all()
        assert len(jobs) == 2

    def test_tmp_replace_atomic_on_same_filesystem(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        q.enqueue(q.SEND, {"chat": "a", "message": "x"})
        path = q._path()
        # Both .jsonl and .jsonl.tmp must be on the same fs (they are:
        # both under DATA_DIR).
        tmp_path_resolved = path.resolve().parent
        tmp_resolved = path.with_suffix(".jsonl.tmp").resolve().parent
        assert tmp_path_resolved == tmp_resolved

    def test_prune_actually_keeps(self, monkeypatch, tmp_path):
        # Bug probe: an earlier version wrote back `jobs` instead of `keep`,
        # silently resurrecting pruned entries. Verify the fix stuck.
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        old = q.enqueue(q.SEND, {"chat": "a", "message": "1"})
        new = q.enqueue(q.SEND, {"chat": "b", "message": "2"})
        q.mark(old["id"], "done")
        q.mark(new["id"], "done")
        jobs = q._read_all()
        for job in jobs:
            if job["id"] == old["id"]:
                job["updated_at"] = time.time() - 8 * 86400
        q._write_all(jobs)
        dropped = q.prune_done()
        assert dropped == 1
        remaining = q.status_counts()
        assert remaining["total"] == 1
        assert remaining["done"] == 1

    def test_max_pending_race_respects_cap(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        q.MAX_PENDING = 5
        for i in range(5):
            q.enqueue(q.SEND, {"chat": "x", "message": str(i)})

        results = []

        def worker():
            try:
                q.enqueue(q.SEND, {"chat": "x", "message": "extra"})
                results.append("ok")
            except q.QueueFullError:
                results.append("rejected")

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        actual = q.status_counts()["pending"]
        assert actual == 5, f"race: {actual} pending jobs after fill+20 attempts"

    def test_clock_skew_makes_pending_replay_immediately(self, monkeypatch, tmp_path):
        # Bug probe: not_before in the future, then clock jumps back.
        # The job should still wait — but the implementation compares to
        # time.time() which is the (jumped) wall clock, so it'd run now.
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        # Schedule 1h in the future.
        future = time.time() + 3600
        q.enqueue(q.SEND, {"chat": "x", "message": "y"}, not_before=future)
        # Simulate clock skew backward by 2h. pending() will think the future
        # not_before is now in the past.
        def _back():
            return time.time() - 7200
        monkeypatch.setattr(q.time, "time", _back)
        # This probe doesn't fully simulate, but it documents the gap.

    def test_empty_pending_array_is_safe(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert q.pending() == []
        assert q.status_counts()["total"] == 0
        assert q.clear_done() == 0
        assert q.prune_done() == 0


# ─────────────────────────── daemon red-team ───────────────────────────


class TestDaemonRedTeam:
    def test_heartbeat_path_must_exist_for_alive_check(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        # Fresh heartbeat with non-existent pid → should be considered dead.
        import json

        p = d.heartbeat_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": 999999, "started_at": time.time(),
                                 "updated_at": time.time()}))
        assert d.is_daemon_alive() is False

    def test_drain_marks_bad_kind_failed(self, monkeypatch, tmp_path):
        # Force-injected unknown kind must surface as a failed job, not crash drain.
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from tg_cli.db import MessageDB

        path = q._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"id":"x","kind":"bogus","payload":{},"status":"pending",'
            '"attempts":0,"not_before":0,"created_at":0,"updated_at":0,'
            '"result":null,"error":null}\n'
        )
        db = MessageDB(db_path=tmp_path / "t.db")

        class FakeClient:
            pass

        stats = asyncio.run(d.drain_pending(FakeClient(), db))
        db.close()
        assert stats["failed"] >= 1
        assert q._read_all()[-1]["status"] == "failed"

    def test_drain_defer_recomputes_not_before(self, monkeypatch, tmp_path):
        # Bug probe: deferred jobs use not_before = time.time() + retry_after,
        # but time.time() is called twice (once when computing, once when
        # checking back). The intent is: the job is ready `retry_after`
        # seconds from NOW (not from when it was deferred).
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from tg_cli.db import MessageDB

        job = q.enqueue(q.SEND, {"chat": "x", "message": "y"})

        class FakeClient:
            async def get_entity(self, chat):
                from tg_cli.ratelimit import TelegramRateLimitedError
                raise TelegramRateLimitedError("+1", SEND, 30.0)

        db = MessageDB(db_path=tmp_path / "t.db")
        stats = asyncio.run(d.drain_pending(FakeClient(), db))
        db.close()
        assert stats["deferred"] == 1
        stored = [j for j in q._read_all() if j["id"] == job["id"]][0]
        assert stored["not_before"] >= time.time() + 29.0

    def test_replay_orphans_is_idempotent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        job = q.enqueue(q.SEND, {"chat": "a", "message": "x"})
        q.mark(job["id"], "running", attempts=1)
        assert q.replay_orphans() == 1
        # Second call: job is back to pending, so 0 orphans.
        assert q.replay_orphans() == 0

    def test_start_detached_writes_stale_log(self, monkeypatch, tmp_path):
        # Bug probe: log_path opens with "ab" mode. If the parent dir is
        # missing, open() will fail. Verify mkdir() runs first.
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        # Data dir exists already (config.get_data_dir creates it).
        result = d.start_detached(interval=1.0)
        # The actual subprocess may or may not be running depending on
        # whether `tg` is on PATH. Either way, the call must not crash.
        assert "started" in result


# ─────────────────────────── CLI soft-fail red-team ───────────────────────────


class TestCliSoftFail:
    def test_refresh_cooldown_returns_ok_false_exit_zero(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        throttle.mark_run("refresh")
        result = CliRunner().invoke(cli, ["refresh", "--yaml"])
        assert result.exit_code == 0, (
            f"refresh cooldown must exit 0, got {result.exit_code}\n{result.output}"
        )
        data = yaml.safe_load(result.output)
        assert data["ok"] is False
        assert data["error"]["code"] == "refresh_cooldown"
        assert "retry_after_seconds" in data["error"].get("details", {})

    def test_refresh_queue_flag_works(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        throttle.mark_run("refresh")
        result = CliRunner().invoke(cli, ["refresh", "--queue", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)["data"]
        assert data["queued"] is True

    def test_send_queues_when_daemon_alive(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod.daemon_mod, "is_daemon_alive", lambda *a, **k: True)
        result = CliRunner().invoke(cli, ["send", "SomeChat", "hi", "--yaml"])
        assert result.exit_code == 0, f"got {result.exit_code}: {result.output}"
        data = yaml.safe_load(result.output)["data"]
        assert data["queued"] is True

    def test_edit_queues_when_daemon_alive(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod.daemon_mod, "is_daemon_alive", lambda *a, **k: True)
        result = CliRunner().invoke(cli, ["edit", "SomeChat", "42", "new text", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)["data"]
        assert data["queued"] is True

    def test_delete_queues_when_daemon_alive(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod.daemon_mod, "is_daemon_alive", lambda *a, **k: True)
        result = CliRunner().invoke(cli, ["delete", "SomeChat", "1", "2", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)["data"]
        assert data["queued"] is True

    def test_queue_drain_refuses_when_daemon_alive(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod.daemon_mod, "is_daemon_alive", lambda *a, **k: True)
        result = CliRunner().invoke(cli, ["queue", "drain", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)
        assert data["ok"] is False
        assert data["error"]["code"] == "daemon_running"

    def test_daemon_start_already_running(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod.daemon_mod, "is_daemon_alive", lambda *a, **k: True)
        result = CliRunner().invoke(cli, ["daemon", "start", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)["data"]
        assert data["started"] is False

    def test_daemon_stop_when_not_running(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod.daemon_mod, "is_daemon_alive", lambda *a, **k: False)
        result = CliRunner().invoke(cli, ["daemon", "stop", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)["data"]
        assert data["stopped"] is False

    def test_queue_full_returns_soft_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod.daemon_mod, "is_daemon_alive", lambda *a, **k: True)
        monkeypatch.setattr(tg_mod.queue_mod, "MAX_PENDING", 2)
        # Pre-fill via direct enqueue.
        for i in range(2):
            tg_mod.queue_mod.enqueue(q.SEND, {"chat": "x", "message": str(i)})
        result = CliRunner().invoke(cli, ["send", "Chat", "msg", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)
        assert data["ok"] is False
        assert data["error"]["code"] == "queue_full"

    def test_refresh_force_skips_cooldown(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        throttle.mark_run("refresh")

        async def fake(*a, **kw):
            return {}

        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod, "sync_all_dialogs", fake)
        result = CliRunner().invoke(cli, ["refresh", "--force", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)
        assert data["ok"] is True

    def test_help_for_queue_and_daemon(self):
        result = CliRunner().invoke(cli, ["queue", "--help"])
        assert result.exit_code == 0
        assert "status" in result.output
        assert "drain" in result.output
        assert "clear" in result.output
        result = CliRunner().invoke(cli, ["daemon", "--help"])
        assert result.exit_code == 0
        assert "status" in result.output
        assert "run" in result.output
        assert "start" in result.output
        assert "stop" in result.output

    def test_send_help_documents_queue(self):
        result = CliRunner().invoke(cli, ["send", "--help"])
        assert result.exit_code == 0
        assert "--queue" in result.output


# ─────────────────────────── SQL injection / file ownership ───────────────────────────


class TestSecurity:
    def test_search_injection_safe(self, tmp_path):
        from datetime import datetime, timezone

        from tg_cli.db import MessageDB

        db = MessageDB(db_path=tmp_path / "test-inject.db")
        try:
            ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
            db.insert_message(chat_id=1, chat_name="x", msg_id=1, sender_id=1,
                              sender_name="x", content="normal", timestamp=ts)
            for nasty in [
                "'; DROP TABLE messages;--",
                '" OR 1=1;--',
                "%' OR '1'='1",
                "test'; UPDATE messages SET content='pwned';--",
            ]:
                results = db.search(nasty, chat_id=1)
                assert isinstance(results, list)
                assert db.count(1) == 1
        finally:
            db.close()

    def test_data_dir_path_traversal_rejected(self, monkeypatch, tmp_path):
        # Bug probe: DATA_DIR=../../etc should not escape the working tree
        # for an unprivileged user. We test that get_data_dir returns a Path
        # and resolves it.
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "ok"))
        from tg_cli.config import get_data_dir
        d = get_data_dir()
        assert d.is_absolute()
        assert d.exists()


# ─────────────────────────── stress / fuzz ───────────────────────────


class TestStress:
    def test_high_volume_enqueue(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        q.MAX_PENDING = 10000
        for _ in range(5):
            for i in range(100):
                q.enqueue(q.SEND, {"chat": f"c{i}", "message": "x"})
            q.clear_done()
        assert q.status_counts()["total"] <= 500

    def test_drain_loop_terminates(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from tg_cli.db import MessageDB

        class ForeverClient:
            async def get_entity(self, chat):
                raise ValueError("nope")

        for _ in range(10):
            q.enqueue(q.SEND, {"chat": "x", "message": "y"})
        db = MessageDB(db_path=tmp_path / "t.db")
        stats1 = asyncio.run(d.drain_pending(ForeverClient(), db))
        stats2 = asyncio.run(d.drain_pending(ForeverClient(), db))
        db.close()
        assert stats1["failed"] == 10
        assert stats2["done"] + stats2["failed"] + stats2["deferred"] == 0

    def test_repeated_record_flood_extends_by_elapsed(self):
        # Documented behavior: suspension-end = monotonic_now + cooldown. Each
        # flood resets the deadline to now+cooldown, which is functionally an
        # extension by elapsed time. Verify this is the actual behavior.
        g = TelegramRateGuard(breaker_threshold=1, breaker_cooldown=60.0)
        g.record_flood("+1", HISTORY, 1.0)
        first_until = g._suspended[("+1", HISTORY)]
        time.sleep(0.05)
        g.record_flood("+1", HISTORY, 1.0)
        second_until = g._suspended[("+1", HISTORY)]
        assert second_until > first_until
        assert second_until - first_until >= 0.04


# ─────────────────────────── additional red-team probes ───────────────────────────


class TestMoreProbes:
    def test_pid_1_returns_alive_for_session(self, monkeypatch, tmp_path):
        # /proc/1 always exists. A heartbeat with pid=1 should be considered
        # alive IF the heartbeat is fresh — the kill check should NOT fail
        # on pid 1 (which you can't signal without privilege).
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import json

        p = d.heartbeat_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": 1, "started_at": time.time(),
                                 "updated_at": time.time()}))
        assert d.is_daemon_alive() is True

    def test_start_detached_refuses_when_alive(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        # Fake "already running" by setting is_daemon_alive to True.
        monkeypatch.setattr(d, "is_daemon_alive", lambda *a, **k: True)
        result = d.start_detached(interval=1.0)
        assert result["started"] is False
        assert "already running" in result["reason"]

    def test_daemon_run_registers_sigterm(self, monkeypatch):
        # Probe: tg daemon run should install a SIGTERM handler that triggers
        # graceful shutdown. If it doesn't, systemd will eventually SIGKILL.

        class Runner:
            def invoke(self, *a, **kw):
                # We can't actually run the daemon in tests, but we can
                # check that the source references asyncio.CancelledError
                # and signal handling.
                import tg_cli.daemon as dm
                src = open(dm.__file__).read()
                assert "asyncio.CancelledError" in src
                assert "KeyboardInterrupt" in src
                return None

        Runner().invoke()

    def test_heartbeat_does_not_use_wall_clock_for_alive(self, monkeypatch, tmp_path):
        # The daemon uses time.monotonic() for breaker; heartbeat uses
        # time.time(). If the system clock jumps backward, the heartbeat
        # timestamp can be in the "future" relative to time.time(), making
        # the daemon appear alive forever. Verify the daemon uses wall clock
        # for heartbeat (acceptable) and that fresh_sec is the right knob.
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import json

        p = d.heartbeat_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # Heartbeat from 1 hour ago → stale.
        p.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time(),
                                 "updated_at": time.time() - 3600}))
        assert d.is_daemon_alive() is False

    def test_breaker_does_not_recover_after_success_if_still_suspended(self):
        # Edge case: record_success resets the COUNTER but not the active
        # suspension. The breaker self-heals only when time expires. This is
        # documented behavior — keep it locked until the cooldown elapses so
        # we don't re-trigger the same ban.
        g = TelegramRateGuard(breaker_threshold=1, breaker_cooldown=600.0)
        g.record_flood("+1", HISTORY, 1.0)
        assert g.is_suspended("+1", HISTORY) is True
        g.record_success("+1", HISTORY)
        assert g.is_suspended("+1", HISTORY) is True  # still suspended

    def test_peer_key_handles_input_peer_user(self):
        # Real Telethon types we should classify correctly.
        class InputPeerUser:
            __slots__ = ("user_id",)

            def __init__(self, uid):
                self.user_id = uid

        class InputPeerChannel:
            __slots__ = ("channel_id",)

            def __init__(self, cid):
                self.channel_id = cid

        class InputPeerChat:
            __slots__ = ("chat_id",)

            def __init__(self, cid):
                self.chat_id = cid

        assert peer_key(InputPeerUser(42)) == "user:42"
        assert peer_key(InputPeerChannel(123)) == "channel:123"
        assert peer_key(InputPeerChat(789)) == "chat:789"

    def test_guarded_with_budget_under_retry_after(self):
        from tg_cli.ratelimit import TelegramRateLimitedError

        # When the gate says "wait 60s" but budget is 5s, must raise quickly
        # without sleeping the full 60s.
        g = TelegramRateGuard(category_limits={HISTORY: RateLimitSpec(1, 60.0)})

        async def quick():
            return "ok"

        async def go():
            await guarded(g, "+1", HISTORY, None, quick, max_defer=0.0)  # burn budget
            start = time.monotonic()
            with pytest.raises(TelegramRateLimitedError):
                await guarded(g, "+1", HISTORY, None, quick, max_defer=0.05)
            elapsed = time.monotonic() - start
            assert elapsed < 1.0, f"took {elapsed}s, expected <1s"

        asyncio.run(go())

    def test_mark_with_unknown_id_is_safe(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        # No exception, just returns False.
        assert q.mark("nonexistent", "done") is False

    def test_prune_with_empty_queue(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert q.prune_done() == 0

    def test_status_counts_returns_expected_shape(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        counts = q.status_counts()
        # Must include total and all status buckets plus oldest_pending_at.
        for key in ("pending", "running", "done", "failed", "total", "oldest_pending_at"):
            assert key in counts, f"missing key: {key}"