"""LangGraph node implementations — thin wrappers around spawn/GitHub utilities.

Spawn utilities are late-imported from pipeline_poller to avoid circular imports
(pipeline_poller imports this module via graph/workflow.py).
"""

import time
from typing import Any

import httpx
from langgraph.types import interrupt

from graph.state import TicketState
from github_api import (
    move_status as _move_status,
    remove_label,
    post_issue_comment,
    fetch_ci_status,
    fetch_pr_review_threads,
    reply_to_review_thread,
    create_issue,
)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _cfg() -> dict:
    """Return project config dict from env (lazy, so no import-time crash)."""
    import os
    return {
        "project_node_id": os.environ["PROJECT_NODE_ID"],
        "status_field_id":  os.environ["STATUS_FIELD_ID"],
    }


def _status_map() -> dict[str, str]:
    """Import STATUS dict from pipeline_poller (late import)."""
    from pipeline_poller import STATUS  # noqa: PLC0415
    return STATUS


# ── Entry / routing ───────────────────────────────────────────────────────────

async def node_route_entry(state: TicketState) -> dict:
    """Move ticket off 'Ready To Pick Up' and set is_spike."""
    from pipeline_poller import _is_spike_state  # noqa: PLC0415
    import httpx as _httpx

    is_spike = state.get("is_spike", False)
    target = "AI Implementation" if is_spike else "AI Planning"
    _log(f"  #{state['ticket_number']}: route_entry → '{target}' (spike={is_spike})")

    async with _httpx.AsyncClient(timeout=30) as client:
        await _move_status(
            client, state["item_id"], target,
            **_cfg(), status_map=_status_map(),
        )
    return {"is_spike": is_spike}


# ── Plan stage ────────────────────────────────────────────────────────────────

async def node_spawn_plan(state: TicketState) -> dict:
    from pipeline_poller import spawn_headless, AI_STAGES  # noqa: PLC0415
    cfg = AI_STAGES["AI Planning"]
    ticket = state["ticket_number"]
    _log(f"  #{ticket}: spawning plan")
    run_id = await spawn_headless("AI Planning", cfg["command"], cfg["tools"], ticket)
    return {"last_run_id": run_id, "last_fired_at": time.time()}


async def node_wait_plan_marker(state: TicketState) -> dict:
    result = interrupt("waiting_plan_marker")
    return {"_plan_marker_outcome": result.get("outcome", "done")}


async def node_move_to_plan_review(state: TicketState) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        await _move_status(
            client, state["item_id"], "Ready to Review then Plan",
            **_cfg(), status_map=_status_map(),
        )
    return {}


async def node_wait_plan_approval(state: TicketState) -> dict:
    interrupt("waiting_plan_approval")
    return {}


# ── Implement stage (shared by spike and non-spike) ───────────────────────────

async def node_spawn_implement(state: TicketState) -> dict:
    from pipeline_poller import (  # noqa: PLC0415
        spawn_headless, spawn_terminal, setup_worktree,
        kill_existing_claude_for_ticket,
        AI_STAGES, REPO_PATH_MAP, ANDROID_REPO_PATH, IOS_REPO_PATH, BACKEND_REPO_PATH,
    )

    ticket = state["ticket_number"]
    is_spike = state.get("is_spike", False)

    if is_spike:
        cfg = {"command": "/spike-tickets", "tools": AI_STAGES["AI Implementation"]["tools"]}
        _log(f"  #{ticket}: spawning spike impl (headless)")
        run_id = await spawn_headless(
            "AI Implementation", cfg["command"], cfg["tools"], ticket,
        )
        return {"last_run_id": run_id, "last_fired_at": time.time()}

    # Non-spike: terminal spawn with worktree
    cfg = AI_STAGES["AI Implementation"]
    repo_local = REPO_PATH_MAP.get(state["repo"], "")

    if not repo_local:
        labels_lower = [l.lower() for l in state.get("labels", [])] if state.get("labels") else []
        if "android" in labels_lower:
            repo_local = ANDROID_REPO_PATH
        elif "ios" in labels_lower:
            repo_local = IOS_REPO_PATH
        elif "backend" in labels_lower:
            repo_local = BACKEND_REPO_PATH

    if not repo_local:
        async with httpx.AsyncClient(timeout=30) as client:
            from pipeline_poller import _infer_repo_from_plan  # noqa: PLC0415
            repo_local = await _infer_repo_from_plan(client, state["repo_full"], ticket)

    if not repo_local:
        err = (
            f"Cannot determine local repo for '{state['repo']}'. "
            "Set ANDROID/IOS/BACKEND_REPO_PATH or add '**Affected**: Android/iOS/Backend' to the plan."
        )
        _log(f"  #{ticket}: ERROR: {err}")
        return {
            "errors": (state.get("errors") or []) + [err],
            "_impl_marker_outcome": "error",
        }

    kill_existing_claude_for_ticket(ticket)
    try:
        worktree_path = setup_worktree(repo_local, ticket)
    except Exception as e:
        err = f"Worktree setup failed: {e}"
        _log(f"  #{ticket}: ERROR: {err}")
        return {
            "errors": (state.get("errors") or []) + [err],
            "_impl_marker_outcome": "error",
        }

    run_id = spawn_terminal(
        "AI Implementation", cfg["command"], cfg["tools"], ticket, worktree_path,
    )
    return {
        "last_run_id": run_id,
        "last_fired_at": time.time(),
        "worktree_path": worktree_path,
        "repo_local_path": repo_local,
    }


async def node_wait_impl_marker(state: TicketState) -> dict:
    result = interrupt("waiting_impl_marker")
    return {"_impl_marker_outcome": result.get("outcome", "done")}


# ── Self-review stage ─────────────────────────────────────────────────────────

async def node_spawn_self_review(state: TicketState) -> dict:
    from pipeline_poller import spawn_headless  # noqa: PLC0415
    ticket = state["ticket_number"]
    repo = state.get("repo", "")
    # Android gets grindr_code_review; others get /code-review
    review_skill = "/grindr-code-review" if "android" in repo.lower() else "/code-review"
    command = f"/self-review-ticket --ticket {ticket} --review-skill '{review_skill}'"
    _log(f"  #{ticket}: spawning self-review")
    run_id = await spawn_headless(
        "Self Review", "/self-review-ticket",
        "Bash,Read,Grep,Glob,Edit,Write,Agent", ticket,
    )
    return {"last_run_id": run_id, "last_fired_at": time.time()}


async def node_wait_self_review_marker(state: TicketState) -> dict:
    result = interrupt("waiting_self_review_marker")
    outcome = result.get("outcome", "done")
    passed = outcome == "done"
    retry_count = state.get("self_review_retry_count", 0)
    if not passed:
        retry_count += 1
    return {
        "_self_review_outcome": outcome,
        "self_review_passed": passed,
        "self_review_retry_count": retry_count,
    }


async def node_move_to_impl_review(state: TicketState) -> dict:
    """Clean up worktree (if non-spike), then move to 'Ready to review Implementation'."""
    ticket = state["ticket_number"]
    if not state.get("is_spike") and state.get("worktree_path") and state.get("repo_local_path"):
        from pipeline_poller import cleanup_worktree  # noqa: PLC0415
        _log(f"  #{ticket}: cleaning worktree before impl review")
        cleanup_worktree(state["worktree_path"], state["repo_local_path"], ticket)

    async with httpx.AsyncClient(timeout=30) as client:
        await _move_status(
            client, state["item_id"], "Ready to review Implementation",
            **_cfg(), status_map=_status_map(),
        )
    return {"worktree_path": "", "repo_local_path": ""}


async def node_wait_impl_approval(state: TicketState) -> dict:
    result = interrupt("waiting_impl_approval")
    return {"impl_approval_type": result.get("label", "impl-approved")}


# ── Ship stage ────────────────────────────────────────────────────────────────

async def node_spawn_ship(state: TicketState) -> dict:
    from pipeline_poller import spawn_headless, AI_STAGES  # noqa: PLC0415
    ticket = state["ticket_number"]
    cfg = AI_STAGES["Ready To Ship - AI"]
    _log(f"  #{ticket}: spawning ship")
    run_id = await spawn_headless("Ready To Ship - AI", cfg["command"], cfg["tools"], ticket)
    return {"last_run_id": run_id, "last_fired_at": time.time()}


async def node_wait_ship_marker(state: TicketState) -> dict:
    result = interrupt("waiting_ship_marker")
    return {"_ship_marker_outcome": result.get("outcome", "done")}


async def node_move_to_in_pr(state: TicketState) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        await _move_status(
            client, state["item_id"], "In PR",
            **_cfg(), status_map=_status_map(),
        )
    return {}


# ── PR monitoring ─────────────────────────────────────────────────────────────

async def node_monitor_pr(state: TicketState) -> dict:
    """Interrupt here until poller detects a PR outcome (CI fail, comments, done)."""
    result = interrupt("waiting_pr_outcome")
    return {"pr_outcome": result.get("outcome", "done")}


# ── CI auto-fix ───────────────────────────────────────────────────────────────

async def node_spawn_fix_ci(state: TicketState) -> dict:
    from pipeline_poller import spawn_headless  # noqa: PLC0415
    ticket = state["ticket_number"]
    pr_number = state.get("pr_number", 0)
    _log(f"  #{ticket}: spawning CI fix for PR #{pr_number}")
    async with httpx.AsyncClient(timeout=30) as client:
        await _move_status(
            client, state["item_id"], "AI-PR Assistance",
            **_cfg(), status_map=_status_map(),
        )
    run_id = await spawn_headless(
        "AI-PR Assistance",
        f"/fix-ci-failure --pr {pr_number}",
        "Bash,Read,Grep,Glob,Edit,Write,Agent",
        ticket,
    )
    return {
        "last_run_id": run_id,
        "last_fired_at": time.time(),
        "ci_fix_count": state.get("ci_fix_count", 0) + 1,
    }


async def node_wait_fix_ci_marker(state: TicketState) -> dict:
    result = interrupt("waiting_fix_ci_marker")
    return {"_fix_ci_outcome": result.get("outcome", "done")}


# ── PR review comment responder ───────────────────────────────────────────────

async def node_spawn_respond_to_review(state: TicketState) -> dict:
    from pipeline_poller import spawn_headless  # noqa: PLC0415
    ticket = state["ticket_number"]
    pr_number = state.get("pr_number", 0)
    _log(f"  #{ticket}: spawning respond-to-review for PR #{pr_number}")
    async with httpx.AsyncClient(timeout=30) as client:
        await _move_status(
            client, state["item_id"], "AI-PR Assistance",
            **_cfg(), status_map=_status_map(),
        )
    run_id = await spawn_headless(
        "AI-PR Assistance",
        f"/respond-to-review --pr {pr_number}",
        "Bash,Read,Grep,Glob,Edit,Write,Agent",
        ticket,
    )
    return {
        "last_run_id": run_id,
        "last_fired_at": time.time(),
        "review_comment_round": state.get("review_comment_round", 0) + 1,
    }


async def node_wait_respond_marker(state: TicketState) -> dict:
    result = interrupt("waiting_respond_marker")
    return {"_respond_outcome": result.get("outcome", "done")}


# ── Spike follow-up ticket creation ──────────────────────────────────────────

async def node_spawn_followups(state: TicketState) -> dict:
    from pipeline_poller import spawn_headless  # noqa: PLC0415
    ticket = state["ticket_number"]
    _log(f"  #{ticket}: spawning spike follow-up creation")
    async with httpx.AsyncClient(timeout=30) as client:
        await _move_status(
            client, state["item_id"], "Ready To Ship - AI",
            **_cfg(), status_map=_status_map(),
        )
    run_id = await spawn_headless(
        "Ready To Ship - AI",
        "/spike-tickets --create-followups",
        "Bash,Read,Grep,Glob,Agent",
        ticket,
    )
    return {"last_run_id": run_id, "last_fired_at": time.time()}


async def node_wait_followups_marker(state: TicketState) -> dict:
    result = interrupt("waiting_followups_marker")
    return {"_followups_outcome": result.get("outcome", "done")}


# ── Terminal nodes ────────────────────────────────────────────────────────────

async def node_done(state: TicketState) -> dict:
    ticket = state["ticket_number"]
    _log(f"  #{ticket}: graph done → moving to Done")
    async with httpx.AsyncClient(timeout=30) as client:
        await _move_status(
            client, state["item_id"], "Done",
            **_cfg(), status_map=_status_map(),
        )
    return {}


async def node_needs_human(state: TicketState) -> dict:
    ticket = state["ticket_number"]
    errors = state.get("errors") or []
    error_summary = "; ".join(errors) if errors else "Pipeline could not proceed automatically."
    _log(f"  #{ticket}: needs_human → posting comment and moving to Needs Human")
    async with httpx.AsyncClient(timeout=30) as client:
        body = (
            f"⚠️ **Pipeline paused — human review needed**\n\n"
            f"{error_summary}\n\n"
            f"<!-- pipeline-needs-human -->"
        )
        await post_issue_comment(client, state["issue_node_id"], body)
        await _move_status(
            client, state["item_id"], "Needs Human",
            **_cfg(), status_map=_status_map(),
        )
    return {}


# ── Conditional edge routing functions ───────────────────────────────────────

def route_entry(state: TicketState) -> str:
    return "spike" if state.get("is_spike") else "normal"


def route_plan_marker(state: TicketState) -> str:
    return state.get("_plan_marker_outcome", "done")


def route_impl_marker(state: TicketState) -> str:
    outcome = state.get("_impl_marker_outcome", "done")
    if outcome == "error":
        return "error"
    if state.get("is_spike"):
        return "spike_done"
    return "self_review"


def route_self_review(state: TicketState) -> str:
    outcome = state.get("_self_review_outcome", "done")
    if outcome == "done":
        return "proceed"
    retry_count = state.get("self_review_retry_count", 0)
    if retry_count < 2:
        return "retry"
    return "proceed"


def route_impl_approval(state: TicketState) -> str:
    label = state.get("impl_approval_type", "impl-approved")
    is_spike = state.get("is_spike", False)
    if label == "followup-approved" and is_spike:
        return "followups"
    if is_spike:
        return "spike_done"
    return "ship"


def route_ship_marker(state: TicketState) -> str:
    return state.get("_ship_marker_outcome", "done")


def route_monitor_pr(state: TicketState) -> str:
    return state.get("pr_outcome", "done")


def route_fix_ci(state: TicketState) -> str:
    outcome = state.get("_fix_ci_outcome", "done")
    if outcome == "needs_human":
        return "needs_human"
    if state.get("ci_fix_count", 0) >= 3:
        return "needs_human"
    return "monitor_pr"


def route_respond(state: TicketState) -> str:
    outcome = state.get("_respond_outcome", "done")
    if outcome == "needs_human":
        return "needs_human"
    if state.get("review_comment_round", 0) >= 2:
        return "needs_human"
    return "monitor_pr"
