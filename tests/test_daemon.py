"""Tests for daemon heartbeat + queue drain — fakes only, no network."""

from __future__ import annotations

import asyncio
import time

import pytest
import yaml
from click.testing import CliRunner

from tg_cli import daemon as d
from tg_cli import queue as q
from tg_cli.cli.main import cli


def test_heartbeat_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert d.is_daemon_alive() is False
    assert d.read_heartbeat() is None
    d.write_heartbeat(time.time())
    assert d.is_daemon_alive() is True
    hb = d.read_heartbeat()
    assert hb["pid"] > 0


def test_heartbeat_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import json

    # Stale timestamp → not alive.
    p = d.heartbeat_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pid": 1, "started_at": 0, "updated_at": time.time() - 3600}))
    assert d.is_daemon_alive() is False
    # Fresh timestamp but dead pid → not alive.
    p.write_text(json.dumps({"pid": 2**30, "started_at": 0, "updated_at": time.time()}))
    assert d.is_daemon_alive() is False


def test_drain_send_job(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    class FakeMsg:
        id = 4242

    class FakeEntity:
        id = 7

    class FakeClient:
        async def get_entity(self, chat):
            assert chat == "TestChat"
            return FakeEntity()

        async def send_message(self, entity, message, reply_to=None, link_preview=True):
            assert message == "hello daemon"
            return FakeMsg()

    from tg_cli.db import MessageDB

    job = q.enqueue(q.SEND, {"chat": "TestChat", "message": "hello daemon"})
    db = MessageDB(db_path=tmp_path / "t.db")
    stats = asyncio.run(d.drain_pending(FakeClient(), db))
    db.close()
    assert stats == {"done": 1, "failed": 0, "deferred": 0}
    assert q.status_counts()["done"] == 1
    stored = q._read_all()[0]
    assert stored["id"] == job["id"]
    assert stored["result"] == {"msg_id": 4242}


def test_drain_failed_job_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    class FakeClient:
        async def get_entity(self, chat):
            raise ValueError("no such chat")

    from tg_cli.db import MessageDB

    q.enqueue(q.SEND, {"chat": "Nope", "message": "x"})
    db = MessageDB(db_path=tmp_path / "t.db")
    stats = asyncio.run(d.drain_pending(FakeClient(), db))
    db.close()
    assert stats["failed"] == 1
    assert q.status_counts()["failed"] == 1
    assert "ValueError" in q._read_all()[0]["error"]


def test_drain_refresh_respects_cooldown(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from tg_cli import throttle

    throttle.mark_run("refresh")

    class FakeClient:
        pass

    from tg_cli.db import MessageDB

    job = q.enqueue(q.REFRESH, {"limit": 10})
    db = MessageDB(db_path=tmp_path / "t.db")
    stats = asyncio.run(d.drain_pending(FakeClient(), db))
    db.close()
    assert stats == {"done": 0, "failed": 0, "deferred": 1}
    again = [j for j in q._read_all() if j["id"] == job["id"]][0]
    assert again["status"] == "pending"
    assert again["not_before"] > time.time()


def test_refresh_cooldown_soft_not_hard(runner_cli, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from tg_cli import throttle

    throttle.mark_run("refresh")
    result = runner_cli.invoke(cli, ["refresh", "--yaml"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.output)
    assert data["ok"] is False
    assert data["error"]["code"] == "refresh_cooldown"


def test_refresh_cooldown_queue_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from tg_cli import throttle

    throttle.mark_run("refresh")
    runner = CliRunner()
    result = runner.invoke(cli, ["refresh", "--queue", "--yaml"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.output)["data"]
    assert data["queued"] is True
    assert q.status_counts()["pending"] == 1


def test_send_routes_to_queue_when_daemon_alive(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import tg_cli.cli.tg as tg_mod

    monkeypatch.setattr(tg_mod.daemon_mod, "is_daemon_alive", lambda *a, **k: True)
    runner = CliRunner()
    result = runner.invoke(cli, ["send", "SomeChat", "hi", "--yaml"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.output)["data"]
    assert data["queued"] is True
    assert data["kind"] == "send"


def test_queue_drain_refuses_when_daemon_alive(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import tg_cli.cli.tg as tg_mod

    monkeypatch.setattr(tg_mod.daemon_mod, "is_daemon_alive", lambda *a, **k: True)
    runner = CliRunner()
    result = runner.invoke(cli, ["queue", "drain", "--yaml"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.output)
    assert data["ok"] is False
    assert data["error"]["code"] == "daemon_running"


def test_queue_status_and_daemon_status(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    q.enqueue(q.SEND, {"chat": "a", "message": "x"})
    runner = CliRunner()
    result = runner.invoke(cli, ["queue", "status", "--yaml"])
    assert result.exit_code == 0
    assert yaml.safe_load(result.output)["data"]["pending"] == 1
    result = runner.invoke(cli, ["daemon", "status", "--yaml"])
    assert result.exit_code == 0
    assert yaml.safe_load(result.output)["data"]["alive"] is False


@pytest.fixture
def runner_cli():
    return CliRunner()
