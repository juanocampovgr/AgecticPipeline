"""gate_impl_approval — human gate: impl-approved label → ship."""

from __future__ import annotations

from typing import Literal

from langgraph.types import Command, interrupt

from graph import events
from graph.state import TicketState


async def gate_impl_approval(state: TicketState) -> Command[Literal["ship"]]:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)

    events.emit(ticket, "Impl Approval", "gate_waiting", {"label": "impl-approved"})
    interrupt("waiting_impl_approval")
    events.emit(ticket, "Impl Approval", "gate_resumed", {"label": "impl-approved"})

    return Command(goto="ship")
