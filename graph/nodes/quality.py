"""node_quality — run quality-check skill in the worktree."""

from __future__ import annotations

from typing import Literal

from langgraph.types import Command

from graph.nodes._base import _log, stage_model
from graph.runner import run_stage
from graph.state import TicketState


async def node_quality(
    state: TicketState,
) -> Command[Literal["self_review", "escalate_error", "needs_human"]]:
    identity      = state.get("identity") or {}
    impl          = state.get("impl")     or {}
    ticket        = identity.get("ticket_number", 0)
    worktree_path = impl.get("worktree_path", "")

    if not worktree_path:
        return Command(
            goto="escalate_error",
            update={"errors": ["No worktree path in state for quality check"]},
        )

    # Choose verification mode: label "quality-mode:local" → local gradlew,
    # otherwise default to runner (GitHub Actions — canonical CI env).
    labels_lower = [la.lower() for la in identity.get("labels", [])]
    use_local    = "quality-mode:local" in labels_lower
    quality_mode = "local" if use_local else "runner"
    mode_arg     = f"--mode {quality_mode}"

    _log(f"  #{ticket}: node_quality worktree={worktree_path} mode={quality_mode}")

    context = {
        "command":       "/quality-check",
        "tools":         "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "extra_args":    mode_arg,
        "worktree_path": worktree_path,
        "model":         stage_model("quality"),
    }
    res = await run_stage(state, "AI Quality Check", context)

    checks_data = [c.model_dump() for c in res.checks]
    run_entry   = [res.record.model_dump()]

    if res.outcome == "needs_human":
        return Command(
            goto="needs_human",
            update={
                "errors":          [res.error or "Quality check requires human intervention"],
                "quality":         {"checks": checks_data, "quality_mode": quality_mode},
                "quality_history": [{"checks": checks_data, "outcome": res.outcome}],
                "run_history":     run_entry,
            },
        )

    if res.outcome in ("error", "timeout", "crash"):
        return Command(
            goto="escalate_error",
            update={
                "errors":          [res.error or f"Quality check failed: {res.outcome}"],
                "quality":         {"checks": checks_data, "quality_mode": quality_mode},
                "quality_history": [{"checks": checks_data, "outcome": res.outcome}],
                "run_history":     run_entry,
            },
        )

    return Command(
        goto="self_review",
        update={
            "quality":         {"checks": checks_data, "affected_modules": res.modules, "quality_mode": quality_mode},
            "quality_history": [{"checks": checks_data, "modules": res.modules, "outcome": res.outcome}],
            "run_history":     run_entry,
            "control":         {"current_stage": "self_review", "last_run": res.record.model_dump()},
        },
    )
