"""node_fix_ci — fix CI failures on the PR branch."""

from __future__ import annotations

from typing import Literal

import httpx
from langgraph.types import Command

from graph.nodes._base import _log, move_status, resolve_worktree, get_retry_caps, stage_model
from graph.runner import run_stage
from graph.state import TicketState


async def node_fix_ci(
    state: TicketState, store=None
) -> Command[Literal["monitor_pr", "needs_human"]]:
    identity     = state.get("identity") or {}
    ship         = state.get("ship")     or {}
    ticket       = identity.get("ticket_number", 0)
    repo         = identity.get("repo", "")
    pr_number    = ship.get("pr_number", 0)
    ci_fix_count = ship.get("ci_fix_count", 0)

    caps = get_retry_caps(store, repo)
    if ci_fix_count >= caps["max_ci_fix_attempts"]:
        return Command(
            goto="needs_human",
            update={"errors": [f"CI still failing after {ci_fix_count} fix attempt(s)"]},
        )

    _log(f"  #{ticket}: node_fix_ci attempt={ci_fix_count} pr={pr_number}")

    branch_id = identity.get("jira_ticket_id", "")
    base      = f"origin/juanocampovgr/{branch_id}" if branch_id else f"origin/juanocampovgr/{ticket}"
    try:
        worktree_path, repo_local = await resolve_worktree(state, store, base=base)
    except RuntimeError as exc:
        return Command(goto="needs_human", update={"errors": [str(exc)]})

    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "AI-PR Assistance")

    context = {
        "command":       f"/fix-ci-failure --pr {pr_number}",
        "tools":         "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "extra_args":    "",
        "worktree_path": worktree_path,
        "model":         stage_model("fix_ci"),
    }
    res = await run_stage(state, "Fix CI", context)

    from pipeline_poller import cleanup_worktree  # noqa: PLC0415
    await cleanup_worktree(worktree_path, repo_local, ticket, identity.get("jira_ticket_id", ""))

    new_count = ci_fix_count + 1
    if res.outcome in ("needs_human", "timeout", "crash"):
        return Command(
            goto="needs_human",
            update={
                "errors":      [res.error or f"CI fix failed: {res.outcome}"],
                "ship":        {"ci_fix_count": new_count},
                "run_history": [res.record.model_dump()],
            },
        )

    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "In PR")

    return Command(
        goto="monitor_pr",
        update={
            "ship":        {"ci_fix_count": new_count},
            "run_history": [res.record.model_dump()],
            "control":     {"current_stage": "monitor_pr", "last_run": res.record.model_dump()},
        },
    )
