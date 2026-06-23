"""gate_impl_approval — human gate: impl-approved or followup-approved label."""

from __future__ import annotations

from typing import Literal

from langgraph.types import Command, interrupt

from graph.state import TicketState


async def gate_impl_approval(state: TicketState) -> Command[Literal["ship", "followups", "done"]]:
    result = interrupt("waiting_impl_approval")
    label  = (result or {}).get("label", "impl-approved") if isinstance(result, dict) else "impl-approved"
    identity  = state.get("identity") or {}
    is_spike  = identity.get("is_spike", False)

    if label == "followup-approved" and is_spike:
        return Command(goto="followups")
    if is_spike:
        return Command(goto="done")
    return Command(goto="ship")
