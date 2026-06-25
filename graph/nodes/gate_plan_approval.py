"""gate_plan_approval — human gate: pauses until poller detects plan-approved label."""

from __future__ import annotations

from typing import Literal

from langgraph.types import Command, interrupt

from graph import events
from graph.state import TicketState


async def gate_plan_approval(state: TicketState) -> Command[Literal["implement"]]:
    identity = state.get("identity") or {}
    ticket = identity.get("ticket_number", 0)
    events.emit(ticket, "Plan Approval", "gate_waiting", {"label": "plan-approved"})
    interrupt("waiting_plan_approval")
    events.emit(ticket, "Plan Approval", "gate_resumed", {"label": "plan-approved"})
    return Command(goto="implement")
