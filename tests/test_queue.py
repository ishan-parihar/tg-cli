"""Tests for the JSONL job queue — storage only, no network."""

from __future__ import annotations

import json
import time

import pytest

from tg_cli import queue as q


def test_enqueue_and_pending_order(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    j1 = q.enqueue(q.SEND, {"chat": "a", "message": "hi"})
    j2 = q.enqueue(q.SEND, {"chat": "b", "message": "yo"})
    assert j1["id"] != j2["id"]
    assert j1["status"] == "pending"
    assert [j["id"] for j in q.pending()] == [j1["id"], j2["id"]]


def test_enqueue_rejects_unknown_kind(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        q.enqueue("nonsense", {})


def test_queue_full(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(q, "MAX_PENDING", 2)
    q.enqueue(q.SEND, {"chat": "a", "message": "1"})
    q.enqueue(q.SEND, {"chat": "b", "message": "2"})
    with pytest.raises(q.QueueFullError):
        q.enqueue(q.SEND, {"chat": "c", "message": "3"})


def test_not_before_defers_job(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    j = q.enqueue(q.SEND, {"chat": "a", "message": "later"}, not_before=time.time() + 3600)
    assert q.pending() == []
    assert q.status_counts()["pending"] == 1
    assert j["id"]


def test_replay_orphans(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    j = q.enqueue(q.SEND, {"chat": "a", "message": "x"})
    assert q.mark(j["id"], "running", attempts=1) is True
    assert q.pending() == []
    assert q.replay_orphans() == 1
    assert [x["id"] for x in q.pending()] == [j["id"]]


def test_corrupt_lines_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    j = q.enqueue(q.SEND, {"chat": "a", "message": "ok"})
    path = q._path()
    with open(path, "a") as f:
        f.write("this is not json\n")
    assert [x["id"] for x in q.pending()] == [j["id"]]
    assert q.status_counts()["total"] == 1


def test_mark_and_clear_done(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    j = q.enqueue(q.SEND, {"chat": "a", "message": "x"})
    assert q.mark("nope", "done") is False
    assert q.mark(j["id"], "done", result={"msg_id": 5}) is True
    assert q.pending() == []
    assert q.status_counts()["done"] == 1
    assert q.clear_done() == 1
    assert q.status_counts()["total"] == 0


def test_prune_done_respects_age(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    fresh = q.enqueue(q.SEND, {"chat": "a", "message": "1"})
    old = q.enqueue(q.SEND, {"chat": "b", "message": "2"})
    q.mark(fresh["id"], "done")
    q.mark(old["id"], "done")
    # Backdate the old job past retention.
    jobs = q._read_all()
    for job in jobs:
        if job["id"] == old["id"]:
            job["updated_at"] = time.time() - 8 * 86400
    q._write_all(jobs)
    assert q.prune_done() == 1
    assert q.status_counts()["total"] == 1


def test_status_counts_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    q.enqueue(q.REFRESH, {})
    counts = q.status_counts()
    assert counts["pending"] == 1
    assert counts["total"] == 1
    assert counts["oldest_pending_at"] is not None
    # raw_json line check — file is valid JSONL
    lines = q._path().read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "refresh"
