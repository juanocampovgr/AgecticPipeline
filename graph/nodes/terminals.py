"""Terminal nodes: done, needs_human, escalate_error."""

from __future__ import annotations

import httpx

from graph import events
from graph.nodes._base import _log, move_status, post_escalation_comment, cleanup_worktree_if_needed
from graph.state import TicketState


async def node_done(state: TicketState) -> dict:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    _log(f"  #{ticket}: graph done → moving to Done")
    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "Done")
    events.emit(ticket, "Done", "ticket_done", {"outcome": "done"})
    return {}


async def node_needs_human(state: TicketState) -> dict:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    errors   = state.get("errors") or []
    _log(f"  #{ticket}: needs_human → cleaning worktree + posting comment + moving to Error")
    # Clean up the worktree so a subsequent retry starts from a known-good state
    # rather than from whatever the crashed subprocess left behind.
    await cleanup_worktree_if_needed(state)
    async with httpx.AsyncClient(timeout=30) as client:
        await post_escalation_comment(client, identity["issue_node_id"], errors, is_needs_human=True)
        await move_status(client, identity["item_id"], "Error")
    events.emit(ticket, "Done", "ticket_done", {
        "outcome": "needs_human",
        "error":   (errors[-1] if errors else "")[:300],
    })
    return {}


async def node_escalate_error(state: TicketState) -> dict:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    errors   = state.get("errors") or []
    _log(f"  #{ticket}: escalate_error → cleaning worktree + posting comment + moving to Error")
    # Clean up the worktree on the error path so a retry gets a clean slate.
    await cleanup_worktree_if_needed(state)
    async with httpx.AsyncClient(timeout=30) as client:
        await post_escalation_comment(client, identity["issue_node_id"], errors, is_needs_human=False)
        await move_status(client, identity["item_id"], "Error")
    events.emit(ticket, "Done", "ticket_done", {
        "outcome": "error",
        "error":   (errors[-1] if errors else "")[:300],
    })
    return {}
