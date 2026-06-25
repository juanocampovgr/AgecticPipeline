"""node_recover — graph entry point that handles recovery from prior errors.

When the poller restarts a ticket that previously errored (status moved from
Error back to Ready To Pick Up), it carries forward the prior LangGraph state
and sets `identity.is_recovery = True` plus `identity.recovery_target` (the
node to resume at). This node reads those signals and routes to the failed
stage instead of starting fresh, preserving run_history, branches, and other
accumulated state.

For normal (non-recovery) runs, this node is a transparent pass-through to
`route_entry` — a single log line, no board moves, no extra latency.
"""

from __future__ import annotations

from typing import Literal

import httpx
from langgraph.types import Command

from graph import events
from graph.nodes._base import _log, move_status
from graph.state import TicketState


# Stage name (as written into RunRecord.stage) → graph node name.
# Keys match the stage strings passed to `run_stage()` by every node.
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

# Board status to display while a stage is (re)running.
_NODE_TO_BOARD_STATUS: dict[str, str] = {
    "plan":        "AI Planning",
    "implement":   "AI Implementation",
    "quality":     "AI Implementation",
    "self_review": "AI Implementation",
    "ship":        "Ready To Ship - AI",
    "fix_ci":      "AI-PR Assistance",
    "respond":     "AI-PR Assistance",
    "followups":   "Ready To Ship - AI",
}

_RECOVER_TARGETS = Literal[
    "route_entry",
    "plan", "implement", "quality", "self_review",
    "ship", "fix_ci", "respond", "followups",
]


async def node_recover(state: TicketState) -> Command[_RECOVER_TARGETS]:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)

    # Fast path for fresh runs: 99% of invocations hit this branch.
    if not identity.get("is_recovery"):
        return Command(goto="route_entry")

    target = identity.get("recovery_target") or _scan_history_for_target(state)
    if not target:
        _log(f"  #{ticket}: recover — no failed stage in history, falling through to route_entry")
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

    events.emit(ticket, "Recovery", "stage_completed", {"resumed_at": target})

    return Command(
        goto=target,
        update={
            "identity": {"is_recovery": False, "recovery_target": ""},
            "errors":   [f"--- recovery: resumed at '{target}' from prior failure ---"],
            "control":  {"current_stage": target},
        },
    )


def _scan_history_for_target(state: TicketState) -> str | None:
    """Fallback: derive resume node from run_history when poller didn't set it."""
    history = state.get("run_history") or []
    for entry in reversed(history):
        outcome = entry.get("outcome")
        if outcome and outcome != "done":
            return _STAGE_TO_NODE.get(entry.get("stage", ""))
    return None
