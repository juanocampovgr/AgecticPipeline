"""node_monitor_pr — external-state gate: CI + PR merged + comments-approved.

This is the one non-result-file interrupt in the pipeline.  CI status is
produced by GitHub Actions — there's no Claude skill to spawn and no result
file to await.  The poller polls fetch_ci_status() and resumes this node with
Command(resume={"outcome": "done"|"fix_ci"|"respond"|"needs_human"}).
"""

from __future__ import annotations

from typing import Literal

from langgraph.types import Command, interrupt

from graph.state import TicketState


async def node_monitor_pr(
    state: TicketState,
) -> Command[Literal["done", "fix_ci", "respond", "needs_human"]]:
    result  = interrupt("waiting_pr_outcome")
    outcome = (result or {}).get("outcome", "done") if isinstance(result, dict) else "done"
    valid   = {"done", "fix_ci", "respond", "needs_human"}
    goto    = outcome if outcome in valid else "needs_human"
    return Command(
        goto=goto,
        update={"ship": {"ci_status": outcome}},
    )
