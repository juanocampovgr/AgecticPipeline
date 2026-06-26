"""node_recover — graph entry point for ALL pipeline restarts.

Two scenarios route here:
  1. Error recovery (dead-END): operator moved a ticket from Error back to
     Ready To Pick Up.  `identity.is_recovery = True` and (optionally)
     `identity.recovery_target` indicate the failed stage.
  2. Poller restart: the poller crashed or was restarted while a stage was
     executing.  The poller clears the checkpoint, sets `is_recovery = True`,
     and re-enters here so all recovery logic is centralised.

For normal (non-recovery) runs, this node is a transparent pass-through to
`route_entry`.

Recovery target is resolved in priority order:
  1. `identity.recovery_target` if set (explicit, e.g. from dead-END path).
  2. Result files on disk — find the first incomplete or missing stage.
  3. `run_history` scan (belt-and-suspenders fallback).
  4. Fall through to `route_entry` if no target can be determined.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import httpx
from langgraph.types import Command

from graph import events
from graph.nodes._base import _log, move_status, resolve_worktree
from graph.state import TicketState


# Nodes that need a clean worktree to operate correctly.
_WORKTREE_NODES = frozenset({"implement", "quality", "self_review", "ship"})

# Stage name (as written into RunRecord.stage) → graph node name.
_STAGE_TO_NODE: dict[str, str] = {
    "AI Planning":        "plan",
    "AI Implementation":  "implement",
    "AI Quality Check":   "quality",
    "Self Review":        "self_review",
    "Ready To Ship - AI": "ship",
    "Fix CI":             "fix_ci",
    "Respond To Review":  "respond",
    "Spike Followups":    "followups",
}

# Reverse mapping — node name → stage name (for result-file lookup).
_NODE_TO_STAGE: dict[str, str] = {v: k for k, v in _STAGE_TO_NODE.items()}

# Board status to display while a stage is (re)running.
_NODE_TO_BOARD_STATUS: dict[str, str] = {
    "plan":        "AI Planning",
    "implement":   "AI Implementation",
    "quality":     "AI Implementation",
    "self_review": "AI Implementation",
    "ship":        "Ready To Ship - AI",
    "monitor_pr":  "In PR",
    "fix_ci":      "AI-PR Assistance",
    "respond":     "AI-PR Assistance",
    "followups":   "Ready To Ship - AI",
}

# Ordered pipeline stages for disk-based target inference.
# Gate nodes (gate_plan_approval, gate_impl_approval) are intentionally omitted;
# routing directly to their successor skips the stale interrupt.
_PIPELINE_ORDER: list[tuple[str, str]] = [
    ("AI Planning",        "plan"),
    ("AI Implementation",  "implement"),
    ("AI Quality Check",   "quality"),
    ("Self Review",        "self_review"),
    ("Ready To Ship - AI", "ship"),
]

# Post-ship stages that can run in any order / repeat.
_POST_SHIP_STAGES: list[tuple[str, str]] = [
    ("Fix CI",            "fix_ci"),
    ("Respond To Review", "respond"),
    ("Spike Followups",   "followups"),
]

_RECOVER_TARGETS = Literal[
    "route_entry",
    "plan", "implement", "quality", "self_review",
    "ship", "monitor_pr", "fix_ci", "respond", "followups",
]


# ── Disk-based inference helpers ──────────────────────────────────────────────

def _best_result_outcome(ticket_dir: Path, stage_name: str) -> str | None:
    """Return the outcome of the highest-attempt result file for a stage, or None."""
    slug = stage_name.lower().replace(" ", "_")
    best: str | None = None
    for attempt in range(10):
        path = ticket_dir / f"{slug}_{attempt}.json"
        if not path.exists():
            break
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if "outcome" in raw:
                best = raw["outcome"]
        except Exception:
            pass
    return best


def _infer_target_from_results(ticket: int, is_spike: bool = False) -> str | None:
    """Scan result files on disk to determine the next stage to execute.

    Returns a node name, or None to fall through to run_history / route_entry.

    Algorithm:
      - Walk main pipeline stages in order; return the first that is not "done".
      - If all main stages are done, check post-ship stages for failures.
      - If ship is done and no post-ship failures, return "monitor_pr" so the
        graph re-enters the CI-polling gate rather than replaying the whole pipeline.
    """
    try:
        from graph.runner import RESULTS_DIR  # noqa: PLC0415
    except ImportError:
        return None

    ticket_dir = RESULTS_DIR / str(ticket)
    if not ticket_dir.exists():
        return None

    stages = [s for s in _PIPELINE_ORDER if not (is_spike and s[1] == "plan")]

    # First non-done (missing, failed, crashed, timed-out) stage is the resume point.
    for stage_name, node_name in stages:
        outcome = _best_result_outcome(ticket_dir, stage_name)
        if outcome != "done":
            return node_name

    # All main stages done — look for a post-ship failure.
    for stage_name, node_name in _POST_SHIP_STAGES:
        outcome = _best_result_outcome(ticket_dir, stage_name)
        if outcome is not None and outcome != "done":
            return node_name

    # Ship is done and no post-ship stage has a failure.
    # We're most likely parked in monitor_pr waiting for CI — resume there.
    if _best_result_outcome(ticket_dir, "Ready To Ship - AI") == "done":
        return "monitor_pr"

    return None


def _cleanup_orphaned_result(ticket: int, stage_name: str) -> None:
    """Delete result files for a stage that have no valid 'outcome' key.

    These are partial writes left by a crashed subprocess.  Removing them lets
    run_stage() do a fresh launch instead of stalling for the full timeout.
    """
    try:
        from graph.runner import RESULTS_DIR  # noqa: PLC0415
        ticket_dir = RESULTS_DIR / str(ticket)
        if not ticket_dir.exists():
            return
        slug = stage_name.lower().replace(" ", "_")
        for attempt in range(10):
            path = ticket_dir / f"{slug}_{attempt}.json"
            if not path.exists():
                break
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if "outcome" not in raw:
                    path.unlink()
                    _log(f"  #{ticket}: recover — removed orphaned result file {path.name}")
            except Exception:
                path.unlink()
                _log(f"  #{ticket}: recover — removed unparseable result file {path.name}")
    except Exception as e:
        _log(f"  #{ticket}: recover — cleanup_orphaned_result failed: {e}")


# ── Main node ─────────────────────────────────────────────────────────────────

async def node_recover(state: TicketState, store=None) -> Command[_RECOVER_TARGETS]:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    is_spike = identity.get("is_spike", False)

    # Fast path for fresh runs: 99% of invocations hit this branch.
    if not identity.get("is_recovery"):
        return Command(goto="route_entry")

    # Resolve target: explicit → disk inference → history scan → fall through.
    target = (
        identity.get("recovery_target")
        or _infer_target_from_results(ticket, is_spike)
        or _scan_history_for_target(state)
    )

    if not target:
        _log(f"  #{ticket}: recover — no resume target found, falling through to route_entry")
        return Command(
            goto="route_entry",
            update={"identity": {"is_recovery": False, "recovery_target": ""}},
        )

    if target not in _NODE_TO_BOARD_STATUS:
        _log(f"  #{ticket}: recover — unknown target '{target}', falling through to route_entry")
        return Command(
            goto="route_entry",
            update={"identity": {"is_recovery": False, "recovery_target": ""}},
        )

    _log(f"  #{ticket}: recover → resuming at '{target}'")
    recent_errors = (state.get("errors") or [])[-3:]
    events.emit(ticket, "Recovery", "stage_started",
                {"target": target, "errors": recent_errors})

    board_status = _NODE_TO_BOARD_STATUS[target]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await move_status(client, identity["item_id"], board_status)
    except Exception as e:
        _log(f"  #{ticket}: recover — board move failed: {e} (continuing)")

    # Clean up any incomplete result files for the target stage so run_stage()
    # does a fresh launch rather than stalling on a partial write from a crash.
    stage_name = _NODE_TO_STAGE.get(target, "")
    if stage_name:
        _cleanup_orphaned_result(ticket, stage_name)

    # For nodes that use a worktree, rebuild it from a clean state so a crashed
    # subprocess cannot poison the retry with stale/contaminated files.
    # monitor_pr is a gate node — it never writes files, no worktree needed.
    worktree_update: dict = {}
    if target in _WORKTREE_NODES:
        try:
            worktree_path, repo_local = await resolve_worktree(state, store)
            _log(f"  #{ticket}: recover — rebuilt worktree at {worktree_path}")
            worktree_update = {
                "impl":     {"worktree_path": worktree_path},
                "identity": {"repo_local_path": repo_local},
            }
        except Exception as e:
            _log(f"  #{ticket}: recover — worktree rebuild failed: {e} (continuing without rebuild)")

    events.emit(ticket, "Recovery", "stage_completed", {"resumed_at": target})

    return Command(
        goto=target,
        update={
            "identity": {"is_recovery": False, "recovery_target": ""},
            "errors":   [f"--- recovery: resumed at '{target}' ---"],
            "control":  {"current_stage": target},
            **worktree_update,
        },
    )


def _scan_history_for_target(state: TicketState) -> str | None:
    """Fallback: derive resume node from run_history when disk inference returns None."""
    history = state.get("run_history") or []
    for entry in reversed(history):
        outcome = entry.get("outcome")
        if outcome and outcome != "done":
            return _STAGE_TO_NODE.get(entry.get("stage", ""))
    return None
