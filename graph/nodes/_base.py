"""Shared helpers used by all node modules."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx

from github_api import (
    move_status as _gql_move_status,
    post_issue_comment,
)
from graph.state import TicketState
from graph.store import get_repo_profile

if TYPE_CHECKING:
    from graph.schemas import StageResult

# ── Retry caps — defaults; Store can override per repo ────────────────────────

MAX_SELF_REVIEW_RETRIES    = 2
MAX_CI_FIX_ATTEMPTS        = 3
MAX_REVIEW_RESPONSE_ROUNDS = 2


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _cfg() -> dict:
    import os
    return {
        "project_node_id": os.environ["PROJECT_NODE_ID"],
        "status_field_id":  os.environ["STATUS_FIELD_ID"],
    }


def _status_map() -> dict[str, str]:
    from pipeline_poller import STATUS  # noqa: PLC0415
    return STATUS


async def move_status(client: httpx.AsyncClient, item_id: str, target: str) -> None:
    """Move the board ticket to target status."""
    await _gql_move_status(
        client, item_id, target,
        **_cfg(), status_map=_status_map(),
    )


def get_retry_caps(store, repo: str) -> dict:
    """Return retry caps, allowing Store override per repo."""
    profile = get_repo_profile(store, repo)
    return {
        "max_self_review_retries":    profile.get("max_self_review_retries",    MAX_SELF_REVIEW_RETRIES),
        "max_ci_fix_attempts":        profile.get("max_ci_fix_attempts",        MAX_CI_FIX_ATTEMPTS),
        "max_review_response_rounds": profile.get("max_review_response_rounds", MAX_REVIEW_RESPONSE_ROUNDS),
    }


async def resolve_repo_local(state: TicketState, store) -> str:
    """Resolve the local repo path from Store → env → label inference."""
    identity = state.get("identity") or {}
    repo = identity.get("repo", "")

    profile = get_repo_profile(store, repo)
    repo_local = profile.get("repo_local_path", "")
    if repo_local:
        return repo_local

    from pipeline_poller import (  # noqa: PLC0415
        REPO_PATH_MAP, ANDROID_REPO_PATH, IOS_REPO_PATH, BACKEND_REPO_PATH,
    )
    repo_local = REPO_PATH_MAP.get(repo, "")
    if repo_local:
        return repo_local

    labels_lower = [la.lower() for la in identity.get("labels", [])]
    if "android" in labels_lower:
        return ANDROID_REPO_PATH
    if "ios" in labels_lower:
        return IOS_REPO_PATH
    if "backend" in labels_lower:
        return BACKEND_REPO_PATH
    return ""


async def resolve_worktree(
    state: TicketState, store, *, base: str = "origin/master"
) -> tuple[str, str]:
    """Return (worktree_path, repo_local_path). Raises RuntimeError if repo unknown."""
    from pipeline_poller import (  # noqa: PLC0415
        setup_worktree, _get_repo_semaphore,
    )
    identity = state.get("identity") or {}
    ticket = identity.get("ticket_number", 0)
    repo = identity.get("repo", "")
    branch_id = identity.get("jira_ticket_id", "")

    repo_local = await resolve_repo_local(state, store)
    if not repo_local:
        raise RuntimeError(
            f"Cannot determine local repo for '{repo}'. "
            "Set ANDROID/IOS/BACKEND_REPO_PATH or configure in the repo_profiles Store."
        )

    async with _get_repo_semaphore(repo):
        worktree_path = await setup_worktree(repo_local, ticket, branch_id, base)
    return worktree_path, repo_local


async def cleanup_worktree_if_needed(state: TicketState) -> None:
    """Clean up worktree if present in state."""
    impl = state.get("impl") or {}
    worktree_path = impl.get("worktree_path", "")
    identity = state.get("identity") or {}
    ticket = identity.get("ticket_number", 0)
    branch_id = identity.get("jira_ticket_id", "")

    repo_local = impl.get("worktree_path", "")
    # Try to get actual repo_local from impl group
    # (stored separately if worktree was set up by node_implement)
    from pipeline_poller import PIPELINE_DIR  # noqa: PLC0415
    # repo_local_path is stored in identity group after resolve_worktree
    repo_local_path = identity.get("repo_local_path", "")

    if worktree_path and repo_local_path:
        from pipeline_poller import cleanup_worktree  # noqa: PLC0415
        _log(f"  #{ticket}: cleaning worktree {worktree_path}")
        await cleanup_worktree(worktree_path, repo_local_path, ticket, branch_id)


async def post_escalation_comment(client: httpx.AsyncClient, issue_node_id: str, errors: list[str], is_needs_human: bool) -> None:
    """Post a needs-human or auto-escalated comment on the issue."""
    error_summary = "; ".join(errors) if errors else "Pipeline could not proceed automatically."
    if is_needs_human:
        body = (
            f"⚠️ **Pipeline paused — human review needed**\n\n"
            f"{error_summary}\n\n"
            f"<!-- pipeline-needs-human -->"
        )
    else:
        body = (
            f"🚨 **Pipeline auto-escalated to Error**\n\n"
            f"{error_summary}\n\n"
            f"<!-- pipeline-error:auto-escalated -->"
        )
    await post_issue_comment(client, issue_node_id, body)
