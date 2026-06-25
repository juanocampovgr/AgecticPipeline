"""node_ship — ship skill: push branch, create PR, move to In PR."""

from __future__ import annotations

from typing import Literal

import httpx
from langgraph.types import Command

from graph.nodes._base import _log, move_status, resolve_worktree
from graph.runner import run_stage
from graph.state import TicketState


async def node_ship(
    state: TicketState, store=None
) -> Command[Literal["monitor_pr", "ship", "escalate_error"]]:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    _log(f"  #{ticket}: node_ship")

    # Re-setup worktree from the existing branch (was cleaned after self-review)
    branch_id = identity.get("jira_ticket_id", "")
    base      = f"origin/juanocampovgr/{branch_id}" if branch_id else f"origin/juanocampovgr/{ticket}"
    try:
        worktree_path, repo_local = await resolve_worktree(state, store, base=base)
    except RuntimeError as exc:
        return Command(goto="escalate_error", update={"errors": [str(exc)]})

    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "Ready To Ship - AI")

    context = {
        "command":       "/ship-tickets",
        "tools":         "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "extra_args":    "",
        "worktree_path": worktree_path,
    }
    res = await run_stage(state, "Ready To Ship - AI", context)

    if res.outcome == "retry":
        return Command(goto="ship", update={"run_history": [res.record.model_dump()]})

    if res.outcome in ("error", "timeout", "crash"):
        return Command(
            goto="escalate_error",
            update={
                "errors":      [res.error or f"Ship failed: {res.outcome}"],
                "run_history": [res.record.model_dump()],
            },
        )

    # Clean worktree before In PR
    from pipeline_poller import cleanup_worktree  # noqa: PLC0415
    await cleanup_worktree(worktree_path, repo_local, ticket, identity.get("jira_ticket_id", ""))

    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "In PR")

    return Command(
        goto="monitor_pr",
        update={
            "ship":        {"pr_number": res.pr_number, "pr_url": res.pr_url},
            "identity":    {"repo_local_path": repo_local},
            "run_history": [res.record.model_dump()],
            "control":     {"current_stage": "monitor_pr", "last_run": res.record.model_dump()},
        },
    )
