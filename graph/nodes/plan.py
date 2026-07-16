"""node_plan — headless plan generation."""

from __future__ import annotations

from typing import Literal

import httpx
from langgraph.types import Command

from graph.nodes._base import _log, move_status, stage_model
from graph.runner import run_stage
from graph.state import TicketState


async def node_plan(state: TicketState, store=None) -> Command[Literal["gate_plan_approval", "escalate_error"]]:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    is_spike = identity.get("is_spike", False)
    _log(f"  #{ticket}: node_plan starting spike={is_spike}")

    # Spikes run the research skill; normal tickets run the planning skill. Both
    # produce a plan-shaped result (plan_content + plan_comment_url) under the
    # "AI Planning" stage and route to the same plan-approval gate.
    command = "/spike-tickets" if is_spike else "/plan-github-tickets"
    context = {
        "command":    command,
        "tools":      "Bash,Read,Grep,Glob,Agent",
        "extra_args": "",
        "model":      stage_model("plan"),
    }
    res = await run_stage(state, "AI Planning", context)

    if res.outcome in ("error", "timeout", "crash"):
        return Command(
            goto="escalate_error",
            update={
                "errors":      [res.error or f"Plan stage failed with outcome={res.outcome}"],
                "run_history": [res.record.model_dump()],
                "control":     {"current_stage": "escalate_error", "last_run": res.record.model_dump()},
            },
        )

    # Move to plan-review before human gate
    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "Ready to Review then Plan")

    return Command(
        goto="gate_plan_approval",
        update={
            "request":     {"approved_plan": res.plan_content, "approved_plan_comment_url": res.plan_comment_url},
            "run_history": [res.record.model_dump()],
            "control":     {"current_stage": "gate_plan_approval", "last_run": res.record.model_dump()},
        },
    )
