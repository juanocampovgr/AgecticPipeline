"""Terminal nodes: done, needs_human, escalate_error."""

from __future__ import annotations

import httpx

from graph.nodes._base import _log, move_status, post_escalation_comment
from graph.state import TicketState


async def node_done(state: TicketState) -> dict:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    _log(f"  #{ticket}: graph done → moving to Done")
    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "Done")
    return {}


async def node_needs_human(state: TicketState) -> dict:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    errors   = state.get("errors") or []
    _log(f"  #{ticket}: needs_human → posting comment + moving to Error")
    async with httpx.AsyncClient(timeout=30) as client:
        await post_escalation_comment(client, identity["issue_node_id"], errors, is_needs_human=True)
        await move_status(client, identity["item_id"], "Error")
    return {}


async def node_escalate_error(state: TicketState) -> dict:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    errors   = state.get("errors") or []
    _log(f"  #{ticket}: escalate_error → posting comment + moving to Error")
    async with httpx.AsyncClient(timeout=30) as client:
        await post_escalation_comment(client, identity["issue_node_id"], errors, is_needs_human=False)
        await move_status(client, identity["item_id"], "Error")
    return {}
