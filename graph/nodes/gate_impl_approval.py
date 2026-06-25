"""gate_impl_approval — human gate: impl-approved or followup-approved label."""

from __future__ import annotations

from typing import Literal

from langgraph.types import Command, interrupt

from graph import events
from graph.state import TicketState


async def gate_impl_approval(state: TicketState) -> Command[Literal["ship", "followups", "done"]]:
    identity  = state.get("identity") or {}
    ticket    = identity.get("ticket_number", 0)
    is_spike  = identity.get("is_spike", False)

    expected = "followup-approved | impl-approved" if is_spike else "impl-approved"
    events.emit(ticket, "Impl Approval", "gate_waiting", {"label": expected})
    result = interrupt("waiting_impl_approval")
    label  = (result or {}).get("label", "impl-approved") if isinstance(result, dict) else "impl-approved"
    events.emit(ticket, "Impl Approval", "gate_resumed", {"label": label})

    if label == "followup-approved" and is_spike:
        return Command(goto="followups")
    if is_spike:
        return Command(goto="done")
    return Command(goto="ship")
