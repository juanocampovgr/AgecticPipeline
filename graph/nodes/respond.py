"""node_respond — respond to PR review comments."""

from __future__ import annotations

from typing import Literal

import httpx
from langgraph.types import Command

from graph.nodes._base import _log, move_status, resolve_worktree, get_retry_caps
from graph.runner import run_stage
from graph.state import TicketState


async def node_respond(
    state: TicketState, store=None
) -> Command[Literal["monitor_pr", "needs_human"]]:
    identity  = state.get("identity") or {}
    ship      = state.get("ship")     or {}
    review    = state.get("review")   or {}
    ticket    = identity.get("ticket_number", 0)
    repo      = identity.get("repo", "")
    pr_number = ship.get("pr_number", 0)
    round_num = review.get("review_comment_round", 0)

    caps = get_retry_caps(store, repo)
    if round_num >= caps["max_review_response_rounds"]:
        return Command(
            goto="needs_human",
            update={"errors": [f"Review response cap reached after {round_num} round(s)"]},
        )

    _log(f"  #{ticket}: node_respond round={round_num} pr={pr_number}")

    branch_id = identity.get("jira_ticket_id", "")
    base      = f"origin/juanocampovgr/{branch_id}" if branch_id else f"origin/juanocampovgr/{ticket}"
    try:
        worktree_path, repo_local = await resolve_worktree(state, store, base=base)
    except RuntimeError as exc:
        return Command(goto="needs_human", update={"errors": [str(exc)]})

    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "AI-PR Assistance")

    context = {
        "command":       f"/respond-to-review --pr {pr_number}",
        "tools":         "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "extra_args":    "",
        "worktree_path": worktree_path,
    }
    res = await run_stage(state, "Respond To Review", context)

    from pipeline_poller import cleanup_worktree  # noqa: PLC0415
    await cleanup_worktree(worktree_path, repo_local, ticket, identity.get("jira_ticket_id", ""))

    new_round = round_num + 1
    if res.outcome in ("needs_human", "timeout", "crash"):
        return Command(
            goto="needs_human",
            update={
                "errors":      [res.error or f"Review response failed: {res.outcome}"],
                "review":      {"review_comment_round": new_round},
                "run_history": [res.record.model_dump()],
            },
        )

    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "In PR")

    return Command(
        goto="monitor_pr",
        update={
            "review":               {"review_comment_round": new_round},
            "responded_thread_ids": res.responded_thread_ids,
            "run_history":          [res.record.model_dump()],
            "control":              {"current_stage": "monitor_pr", "last_run": res.record.model_dump()},
        },
    )
