"""node_followups — create follow-up tickets from spike research."""

from __future__ import annotations

from typing import Literal

import httpx
from langgraph.types import Command

from graph.nodes._base import _log, move_status
from graph.runner import run_stage
from graph.state import TicketState


async def node_followups(state: TicketState) -> Command[Literal["done", "escalate_error"]]:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    _log(f"  #{ticket}: node_followups")

    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "Ready To Ship - AI")

    context = {
        "command":    "/spike-tickets --create-followups",
        "tools":      "Bash,Read,Grep,Glob,Agent",
        "spawn_mode": "headless",
        "extra_args": "",
    }
    res = await run_stage(state, "Spike Followups", context)

    if res.outcome in ("error", "timeout", "crash"):
        return Command(
            goto="escalate_error",
            update={
                "errors":      [res.error or f"Followups failed: {res.outcome}"],
                "run_history": [res.record.model_dump()],
            },
        )

    return Command(
        goto="done",
        update={
            "followup_tickets": res.followup_ticket_numbers,
            "run_history":      [res.record.model_dump()],
            "control":          {"current_stage": "done", "last_run": res.record.model_dump()},
        },
    )
