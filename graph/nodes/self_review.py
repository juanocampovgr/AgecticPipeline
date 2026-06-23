"""node_self_review — headless self-review with retry cap from Store."""

from __future__ import annotations

from typing import Literal

import httpx
from langgraph.types import Command

from graph.nodes._base import _log, move_status, get_retry_caps
from graph.runner import run_stage
from graph.state import TicketState


async def node_self_review(
    state: TicketState, store=None
) -> Command[Literal["gate_impl_approval", "implement", "escalate_error"]]:
    identity      = state.get("identity") or {}
    impl          = state.get("impl")     or {}
    ticket        = identity.get("ticket_number", 0)
    repo          = identity.get("repo", "")
    worktree_path = impl.get("worktree_path", "")
    retry_count   = impl.get("self_review_retry_count", 0)

    caps        = get_retry_caps(store, repo)
    max_retries = caps["max_self_review_retries"]

    _log(f"  #{ticket}: node_self_review attempt={retry_count} max={max_retries}")

    context = {
        "command":    "/self-review-ticket",
        "tools":      "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "spawn_mode": "headless",
        "extra_args": "",
    }
    res = await run_stage(state, "Self Review", context)

    if res.outcome == "done" and res.self_review_passed:
        # Clean up worktree before human gate
        repo_local = identity.get("repo_local_path", "")
        if worktree_path and repo_local:
            from pipeline_poller import cleanup_worktree  # noqa: PLC0415
            await cleanup_worktree(
                worktree_path, repo_local, ticket,
                identity.get("jira_ticket_id", ""),
            )
        async with httpx.AsyncClient(timeout=30) as client:
            await move_status(client, identity["item_id"], "Ready to review Implementation")
        return Command(
            goto="gate_impl_approval",
            update={
                "impl":        {"self_review_passed": True, "worktree_path": ""},
                "run_history": [res.record.model_dump()],
                "control":     {"current_stage": "gate_impl_approval", "last_run": res.record.model_dump()},
            },
        )

    # Self-review failed or errored
    new_retry_count = retry_count + 1
    if new_retry_count > max_retries:
        return Command(
            goto="escalate_error",
            update={
                "errors":      [f"Self-review failed after {new_retry_count} attempt(s) — escalating"],
                "impl":        {"self_review_passed": False, "self_review_retry_count": new_retry_count},
                "run_history": [res.record.model_dump()],
            },
        )

    return Command(
        goto="implement",
        update={
            "impl":        {"self_review_passed": False, "self_review_retry_count": new_retry_count},
            "run_history": [res.record.model_dump()],
            "control":     {"current_stage": "implement"},
        },
    )
