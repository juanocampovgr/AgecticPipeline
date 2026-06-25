"""Per-ticket event log — JSONL bus between the pipeline and the dashboard TUI.

Every line in `~/.pipeline/events/{ticket}.jsonl` is a single JSON object:
    {"ts": 1700000000.0, "stage": "AI Implementation", "kind": "stage_started", "payload": {...}}

Writes are append-only with `O_APPEND`, which on POSIX systems is atomic for
writes under PIPE_BUF (4 KB) — enough for our event payloads. The dashboard
tails the file and is the sole reader.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

PIPELINE_DIR = Path(os.environ.get("PIPELINE_DIR", Path.home() / ".pipeline"))
EVENTS_DIR = Path(os.environ.get("PIPELINE_EVENTS_DIR", PIPELINE_DIR / "events"))

# Allowed event kinds — keep this list in sync with the dashboard renderer.
KINDS = frozenset({
    "ticket_grabbed",
    "stage_started",
    "stage_progress",
    "stage_completed",
    "stage_failed",
    "stage_retry",
    "gate_waiting",
    "gate_resumed",
    "status_changed",
    "heartbeat",
    "ticket_done",
})


def events_path(ticket: int) -> Path:
    return EVENTS_DIR / f"{ticket}.jsonl"


def emit(ticket: int, stage: str | None, kind: str, payload: dict[str, Any] | None = None) -> None:
    """Append one event line to the ticket's event log. Never raises."""
    if not ticket:
        return
    try:
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "stage": stage or "",
            "kind": kind,
            "payload": payload or {},
        }
        line = json.dumps(record, default=str) + "\n"
        # O_APPEND makes the write atomic against concurrent writers.
        fd = os.open(
            events_path(ticket),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o644,
        )
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        # Telemetry must never break the pipeline.
        pass
