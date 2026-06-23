"""gate_plan_approval — human gate: pauses until poller detects plan-approved label."""

from __future__ import annotations

from typing import Literal

from langgraph.types import Command, interrupt

from graph.state import TicketState


async def gate_plan_approval(state: TicketState) -> Command[Literal["implement"]]:
    interrupt("waiting_plan_approval")
    return Command(goto="implement")
