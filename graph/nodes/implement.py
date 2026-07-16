"""node_implement — implementation stage (worktree-based; non-spike only)."""

from __future__ import annotations

from typing import Literal

import httpx
from langgraph.types import Command

from graph.nodes._base import _log, move_status, resolve_worktree, stage_model
from graph.runner import run_stage
from graph.state import TicketState


async def node_implement(
    state: TicketState, store=None
) -> Command[Literal["quality", "implement", "escalate_error"]]:
    identity  = state.get("identity") or {}
    ticket    = identity.get("ticket_number", 0)
    _log(f"  #{ticket}: node_implement")

    # ── Worktree-based implementation (spikes are handled in the plan stage) ────
    try:
        worktree_path, repo_local = await resolve_worktree(state, store)
    except RuntimeError as exc:
        return Command(
            goto="escalate_error",
            update={"errors": [str(exc)], "run_history": []},
        )

    from pipeline_poller import kill_existing_claude_for_ticket  # noqa: PLC0415
    kill_existing_claude_for_ticket(ticket)

    async with httpx.AsyncClient(timeout=30) as client:
        await move_status(client, identity["item_id"], "AI Implementation")

    context = {
        "command":       "/code-tickets",
        "tools":         "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "extra_args":    "",
        "worktree_path": worktree_path,
        "model":         stage_model("implement"),
    }
    res = await run_stage(state, "AI Implementation", context)

    if res.outcome in ("error", "timeout", "crash"):
        return Command(
            goto="escalate_error",
            update={
                "errors":      [res.error or f"Implementation failed: {res.outcome}"],
                "impl":        {"worktree_path": worktree_path},
                "identity":    {"repo_local_path": repo_local},
                "run_history": [res.record.model_dump()],
            },
        )

    return Command(
        goto="quality",
        update={
            "impl":        {
                "branch":         res.branch,
                "files_changed":  res.files_changed,
                "impl_summary":   res.impl_summary,
                "worktree_path":  worktree_path,
            },
            "identity":    {"repo_local_path": repo_local},
            "commit_shas": res.commit_shas,
            "run_history": [res.record.model_dump()],
            "control":     {"current_stage": "quality", "last_run": res.record.model_dump()},
        },
    )
