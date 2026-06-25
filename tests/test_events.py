"""Unit tests for graph/events.py — JSONL event bus.

Covers:
  - emit() creates the events directory and writes a parseable JSON line
  - emit() appends rather than overwrites
  - emit() with a missing/zero ticket is a no-op
  - Concurrent writers from threads don't corrupt the file
  - emit() never raises even when given odd payloads

Run with: python -m pytest tests/test_events.py -v
"""

import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def events_dir(tmp_path, monkeypatch):
    d = tmp_path / "events"
    monkeypatch.setenv("PIPELINE_DIR", str(tmp_path))
    monkeypatch.setenv("PIPELINE_EVENTS_DIR", str(d))
    # Reload the module so the env vars take effect.
    import importlib
    import graph.events as events_module
    importlib.reload(events_module)
    yield d, events_module
    importlib.reload(events_module)


def test_emit_creates_file_and_writes_jsonl(events_dir):
    d, events = events_dir
    events.emit(42, "AI Planning", "stage_started", {"log_path": "/tmp/x.log"})

    path = d / "42.jsonl"
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["stage"] == "AI Planning"
    assert record["kind"] == "stage_started"
    assert record["payload"] == {"log_path": "/tmp/x.log"}
    assert isinstance(record["ts"], (int, float))


def test_emit_appends(events_dir):
    d, events = events_dir
    events.emit(7, "AI Planning", "stage_started", {})
    events.emit(7, "AI Planning", "stage_completed", {"outcome": "done"})
    events.emit(7, None, "heartbeat", None)

    lines = (d / "7.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3
    kinds = [json.loads(l)["kind"] for l in lines]
    assert kinds == ["stage_started", "stage_completed", "heartbeat"]


def test_emit_no_ticket_is_noop(events_dir):
    d, events = events_dir
    events.emit(0, "stage", "stage_started", {})
    assert not d.exists() or not any(d.iterdir())


def test_emit_concurrent_writes_no_corruption(events_dir):
    d, events = events_dir
    N_THREADS = 8
    N_PER_THREAD = 25

    def writer(idx: int) -> None:
        for i in range(N_PER_THREAD):
            events.emit(99, "AI Implementation", "stage_progress",
                        {"worker": idx, "i": i})

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw_lines = (d / "99.jsonl").read_text().strip().splitlines()
    assert len(raw_lines) == N_THREADS * N_PER_THREAD
    for line in raw_lines:
        record = json.loads(line)
        assert record["kind"] == "stage_progress"
        assert "worker" in record["payload"]
        assert "i" in record["payload"]


def test_emit_never_raises_on_bad_payload(events_dir):
    d, events = events_dir
    # default=str in json.dumps handles non-serialisable objects
    events.emit(3, "x", "stage_started", {"path": Path("/tmp/abc")})
    lines = (d / "3.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert "/tmp/abc" in lines[0]
