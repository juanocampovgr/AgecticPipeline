"""node_route_entry — route spike/normal/recovery to the right starting node."""

from __future__ import annotations

from typing import Literal

import httpx
from langgraph.types import Command

from graph.nodes._base import _log, move_status
from graph.state import TicketState


async def node_route_entry(state: TicketState) -> Command[Literal["plan", "implement"]]:
    identity = state.get("identity") or {}
    is_spike  = identity.get("is_spike", False)
    entry     = identity.get("entry_point", "plan")
    ticket    = identity.get("ticket_number", 0)

    target_status = "AI Implementation" if (is_spike or entry == "implement") else "AI Planning"
    _log(f"  #{ticket}: route_entry → spike={is_spike} entry={entry} status='{target_status}'")

    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], target_status)

    goto = "implement" if (is_spike or entry == "implement") else "plan"
    return Command(goto=goto)
