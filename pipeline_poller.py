"""
Polling pipeline orchestrator for GitHub Project #2 (juanocampovgr).

Polls the board every N seconds. For each ticket, drives a LangGraph StateGraph
thread that encodes the full development lifecycle:

  Non-spike: AI Planning → [plan-approved] → AI Implementation → Self Review
             → [impl-approved] → Ready To Ship - AI → In PR → monitor CI/comments → Done

  Spike:     AI Implementation (/spike-tickets) → [impl-approved → Done |
             followup-approved → create follow-up tickets → Done]

Human approval gates still work by polling GitHub labels every cycle.
Graph state is checkpointed in SQLite (~/.pipeline/graph_checkpoints.db) so
tickets resume correctly after restarts or crashes.

Poller's three jobs (v2 refactor):
  1. Discover & grab  — fetch_board(); new Ready To Pick Up → create_task(_run_thread)
  2. Drive interrupt gates — human labels (plan/impl approval) + external-state gate (monitor_pr)
  3. Supervise — watchdog for tickets parked too long at gates
"""

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from github_api import (
    fetch_board,
    fetch_issue_body,
    move_status as _gql_move_status,
    remove_label as _gql_remove_label,
    post_issue_comment,
    fetch_ci_status,
)
from graph import events as graph_events
from graph.terminal import open_dashboard as _open_dashboard_terminal
from graph.runner import compute_result_path

# Stage name (RunRecord.stage) → graph node name. Mirrors graph/nodes/recover.py;
# kept in lockstep — if you add a new pipeline stage, add it in both places.
_STAGE_TO_NODE: dict[str, str] = {
    "AI Planning":        "plan",
    "AI Implementation":  "implement",
    "AI Quality Check":   "quality",
    "Self Review":        "self_review",
    "Ready To Ship - AI": "ship",
    "Fix CI":             "fix_ci",
    "Respond To Review":  "respond",
    "Spike Followups":    "followups",
}

# ── Config ───────────────────────────────────────────────────────────────────

def _gh_token() -> str:
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Could not get GitHub token from gh CLI — run `gh auth login` first")
    return result.stdout.strip()


GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN") or _gh_token()
os.environ.setdefault("GITHUB_TOKEN", GITHUB_TOKEN)  # ensure github_api._get_token() finds it without shelling out
PROJECT_OWNER    = os.environ["PROJECT_OWNER"]
PROJECT_NUMBER   = int(os.environ["PROJECT_NUMBER"])
PROJECT_NODE_ID  = os.environ["PROJECT_NODE_ID"]
STATUS_FIELD_ID  = os.environ["STATUS_FIELD_ID"]
CLAUDE_BIN       = os.environ.get("CLAUDE_BIN", "claude")
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))

ANDROID_REPO_PATH = os.environ.get("ANDROID_REPO_PATH", "")
IOS_REPO_PATH     = os.environ.get("IOS_REPO_PATH", "")
BACKEND_REPO_PATH = os.environ.get("BACKEND_REPO_PATH", "")
REPO_PATH_MAP: dict[str, str] = {
    "grindr-android-agent": ANDROID_REPO_PATH,
    "grindr-android":       ANDROID_REPO_PATH,
    "grindr-3.0-ios":       IOS_REPO_PATH,
    "backend":              BACKEND_REPO_PATH,
}

MIN_DESCRIPTION_CHARS = int(os.environ.get("MIN_DESCRIPTION_CHARS", "20"))
MAX_TICKET_FAILURES   = int(os.environ.get("MAX_TICKET_FAILURES", "3"))

PIPELINE_DIR  = Path(os.environ.get("PIPELINE_DIR", Path.home() / ".pipeline"))
LOG_DIR       = Path(os.environ.get("LOG_DIR", PIPELINE_DIR / "logs"))
WORKTREES_DIR = PIPELINE_DIR / "worktrees"
GRAPH_DB_PATH = PIPELINE_DIR / "graph_checkpoints.db"
RESULTS_DIR   = PIPELINE_DIR / "results"

PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


_JIRA_RE   = re.compile(r'\b([A-Z][A-Z0-9]{1,9}-\d+)\b')
_SLUG_JUNK = re.compile(r'[^a-z0-9]+')


def extract_jira_ticket_id(title: str, body: str) -> str | None:
    """Return the first Jira-style ticket ID (e.g. ANDROID-1234) found in
    the issue title or body. Title is checked first."""
    for text in (title, body):
        m = _JIRA_RE.search(text)
        if m:
            return m.group(1)
    return None


def _make_branch_slug(text: str, max_len: int = 45) -> str:
    """Convert text to a git-safe lowercase hyphen-slug."""
    text = _SLUG_JUNK.sub('-', text.lower()).strip('-')
    return text[:max_len].rstrip('-')


def make_branch_id(jira_id: str, title: str) -> str:
    """Return 'JIRA-123-friendly-description' from the Jira ID and issue title.

    The Jira ID is stripped from the title before slugifying so the prefix
    doesn't appear twice (e.g. 'ANDROID-1234 Fix crash' → 'ANDROID-1234-fix-crash').
    """
    desc = _JIRA_RE.sub('', title).strip()
    slug = _make_branch_slug(desc)
    return f"{jira_id}-{slug}" if slug else jira_id


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def _validate_config() -> None:
    errors: list[str] = []
    if not any([ANDROID_REPO_PATH, IOS_REPO_PATH, BACKEND_REPO_PATH]):
        errors.append(
            "No repo paths set — set at least one of ANDROID_REPO_PATH, IOS_REPO_PATH, "
            "BACKEND_REPO_PATH in the launchd plist"
        )
    claude_check = subprocess.run(["which", CLAUDE_BIN], capture_output=True, text=True)
    if claude_check.returncode != 0:
        errors.append(f"CLAUDE_BIN '{CLAUDE_BIN}' not found in PATH — check CLAUDE_BIN env var")
    if errors:
        for msg in errors:
            print(f"FATAL: {msg}", file=sys.stderr)
        sys.exit(1)


# ── Status option IDs (board v2) ──────────────────────────────────────────────

STATUS = {
    "Backlog":                        "f75ad846",
    "AI Planning":                    "61e4505c",
    "Ready to Review then Plan":      "47fc9ee4",
    "AI Implementation":              "df73e18b",
    "Ready to review Implementation": "98236657",
    "Ready To Ship - AI":             "c08c27e2",
    "Ready To Pick Up":               "2eb5346d",
    "In PR":                          "484abe4c",
    "Error":                          "f49d1062",
    "Done":                           "77ddf356",
    "AI-PR Assistance":               "e63c7fb8",
}

# ── AI stage configs ──────────────────────────────────────────────────────────

AI_STAGES = {
    "AI Planning": {
        "command":      "/plan-github-tickets",
        "tools":        "Bash,Read,Grep,Glob,Agent",
        "done_marker":  "<!-- ai-plan:done -->",
        "error_marker": "<!-- ai-plan:error -->",
        "next_status":  "Ready to Review then Plan",
        "spawn_mode":   "headless",
    },
    "AI Implementation": {
        "command":      "/code-tickets",
        "tools":        "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "done_marker":  "<!-- ai-impl:done -->",
        "error_marker": "<!-- ai-impl:error -->",
        "next_status":  "Ready to review Implementation",
        "spawn_mode":   "terminal",
    },
    "AI Quality Check": {
        "command":      "/quality-check",
        "tools":        "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "done_marker":  "<!-- ai-quality:done -->",
        "error_marker": "<!-- ai-quality:error -->",
        "spawn_mode":   "terminal",
    },
    "Ready To Ship - AI": {
        "command":      "/ship-tickets",
        "tools":        "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "done_marker":  "<!-- ai-ship:done -->",
        "error_marker": "<!-- ai-ship:error -->",
        "next_status":  "In PR",
        "spawn_mode":   "terminal",
    },
    "Self Review": {
        "command":      "/self-review-ticket",
        "tools":        "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "done_marker":  "<!-- ai-self-review:done -->",
        "error_marker": "<!-- ai-self-review:failed -->",
        "spawn_mode":   "headless",
    },
    "AI-PR Assistance (CI Fix)": {
        "command":      "/fix-ci-failure",
        "tools":        "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "done_marker":  "<!-- ai-ci-fix:done -->",
        "error_marker": "<!-- ai-ci-fix:needs-human -->",
        "spawn_mode":   "headless",
    },
    "AI-PR Assistance (Review)": {
        "command":      "/respond-to-review",
        "tools":        "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "done_marker":  "<!-- ai-review-response:done -->",
        "error_marker": "<!-- ai-review-response:needs-human -->",
        "spawn_mode":   "headless",
    },
    "Spike Follow-ups": {
        "command":      "/spike-tickets",
        "tools":        "Bash,Read,Grep,Glob,Agent",
        "done_marker":  "<!-- ai-followups:done -->",
        "error_marker": "<!-- ai-followups:error -->",
        "spawn_mode":   "headless",
    },
}

# ── Per-repo concurrency ──────────────────────────────────────────────────────

_MAX_PER_REPO = int(os.environ.get("MAX_CONCURRENT_PER_REPO", "2"))
_MAX_TOTAL    = int(os.environ.get("MAX_CONCURRENT_RUNS", "4"))

_repo_semaphores: dict[str, asyncio.Semaphore] = {}
_total_semaphore: asyncio.Semaphore | None = None
_ticket_locks: dict[int, asyncio.Lock] = {}


def _get_repo_semaphore(repo: str) -> asyncio.Semaphore:
    if repo not in _repo_semaphores:
        _repo_semaphores[repo] = asyncio.Semaphore(_MAX_PER_REPO)
    return _repo_semaphores[repo]


def _get_total_semaphore() -> asyncio.Semaphore:
    global _total_semaphore
    if _total_semaphore is None:
        _total_semaphore = asyncio.Semaphore(_MAX_TOTAL)
    return _total_semaphore


# ── Spike detection ───────────────────────────────────────────────────────────

def _is_spike(item: dict) -> bool:
    if "spike" in [l.lower() for l in item.get("labels", [])]:
        return True
    return "spike" in item.get("title", "").lower()


def _is_spike_state(state: dict) -> bool:
    """Version that accepts graph state dict (used by nodes.py)."""
    return state.get("is_spike", False)


# ── Active-thread tracking (restart recovery discriminator) ───────────────────

_active_threads: set[str] = set()


async def _run_thread(workflow, thread_id: str, input_) -> None:
    """Wrap workflow.ainvoke so the poll loop can tell which threads are live.

    Adds thread_id to _active_threads for the duration of the call so that
    reconcile_once can skip (not re-invoke) tickets whose node is already
    running in this process.  On poller restart the set is empty, so any
    thread with snapshot.next will be re-invoked and run_stage()'s
    idempotency guard handles whether to re-launch the Terminal or read an
    already-written result file.
    """
    _active_threads.add(thread_id)
    try:
        await workflow.ainvoke(input_, {"configurable": {"thread_id": thread_id}})
    finally:
        _active_threads.discard(thread_id)


# ── Failure reporting ─────────────────────────────────────────────────────────

def post_failure_comment(repo_full: str, ticket: int, stage: str, error: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    body = (
        f"⚠️ **Pipeline failure — {stage}**\n\n"
        f"**Error:** {error}\n"
        f"**Time:** {ts}\n\n"
        f"The ticket has been paused. To retry, move it back to **{stage}** on the board.\n\n"
        f"<!-- pipeline-error:{stage} -->"
    )
    result = subprocess.run(
        ["gh", "issue", "comment", str(ticket), "--repo", repo_full, "--body", body],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        _log(f"  WARNING: could not post failure comment on #{ticket}: {result.stderr.strip()}")


# ── Session guard ─────────────────────────────────────────────────────────────

def kill_existing_claude_for_ticket(ticket: int) -> None:
    import process_utils
    pids = process_utils.find_pipeline_claude_pids_for_ticket(ticket)
    if not pids:
        return
    _log(f"  #{ticket}: killing {len(pids)} existing claude session(s): {pids}")
    process_utils.kill_pids(pids, log=_log)


# ── Error escalation ──────────────────────────────────────────────────────────

async def _escalate_to_error(client: httpx.AsyncClient, item: dict, reason: str) -> None:
    ticket = item["issue_number"]
    # Idempotent: if ticket is already in Error, skip — prevents duplicate comment spam
    if item.get("status") == "Error":
        _log(f"  #{ticket}: already in Error — skipping duplicate escalation")
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    body = (
        f"🚨 **Pipeline auto-escalated to Error**\n\n"
        f"{reason}\n\n"
        f"**Time:** {ts}\n\n"
        f"<!-- pipeline-error:auto-escalated -->"
    )
    await post_issue_comment(client, item["issue_node_id"], body)
    await _gql_move_status(client, item["item_id"], "Error",
                           PROJECT_NODE_ID, STATUS_FIELD_ID, STATUS)
    _log(f"  #{ticket}: escalated to Error — {reason[:80]}")


def _setup_worktree_sync(repo_local_path: str, ticket: int, branch_id: str = "", base: str = "origin/master") -> str:
    branch = f"juanocampovgr/{branch_id or ticket}"
    worktree_path = str(WORKTREES_DIR / str(ticket))

    _log(f"  setup_worktree: repo={repo_local_path} branch={branch} base={base} worktree={worktree_path}")

    subprocess.run(
        ["git", "-C", repo_local_path, "worktree", "remove", "--force", worktree_path],
        capture_output=True, text=True, check=False,
    )
    if Path(worktree_path).exists():
        shutil.rmtree(worktree_path, ignore_errors=True)

    if base == "origin/master":
        fetch_refspec = "master:master"
    else:
        fetch_refspec = base.removeprefix("origin/")
    r = subprocess.run(
        ["git", "-C", repo_local_path, "fetch", "origin", fetch_refspec],
        capture_output=True, text=True, check=False,
    )
    _log(f"  setup_worktree: fetch rc={r.returncode}")

    # Check whether the target branch already exists on the remote. If it does,
    # fetch it and create the worktree from the remote tip so the local history
    # matches — a regular `git push` will then be a fast-forward, not rejected.
    remote_branch_exists = subprocess.run(
        ["git", "-C", repo_local_path, "ls-remote", "--exit-code", "--heads", "origin", branch],
        capture_output=True, text=True, check=False,
    ).returncode == 0

    if remote_branch_exists:
        rf = subprocess.run(
            ["git", "-C", repo_local_path, "fetch", "origin", f"{branch}:{branch}"],
            capture_output=True, text=True, check=False,
        )
        if rf.returncode == 0:
            _log(f"  setup_worktree: remote branch '{branch}' found — using it as worktree base")
            worktree_base = branch
        else:
            _log(f"  setup_worktree: remote branch fetch failed (rc={rf.returncode}), falling back to {base}")
            worktree_base = base
    else:
        worktree_base = base

    current = subprocess.run(
        ["git", "-C", repo_local_path, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    if current == branch:
        r = subprocess.run(
            ["git", "-C", repo_local_path, "checkout", "-f", "master"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"checkout -f master failed: {r.stderr.strip()}")

    r = subprocess.run(
        ["git", "-C", repo_local_path, "branch", "-D", branch],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0 and "not found" not in r.stderr and branch not in r.stderr:
        raise RuntimeError(f"branch -D {branch} failed: {r.stderr.strip()}")

    # When using an existing remote branch as the base, the branch name already
    # matches so we use `git worktree add` without `-b` (checkout, not create).
    if worktree_base == branch:
        result = subprocess.run(
            ["git", "-C", repo_local_path, "worktree", "add", worktree_path, branch],
            capture_output=True, text=True, check=False,
        )
    else:
        result = subprocess.run(
            ["git", "-C", repo_local_path, "worktree", "add", "-b", branch, worktree_path, worktree_base],
            capture_output=True, text=True, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    head_sha = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    remote_sha = subprocess.run(
        ["git", "-C", repo_local_path, "rev-parse", f"origin/{branch}"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    _log(
        f"  setup_worktree: SUCCESS at {worktree_path} "
        f"HEAD={head_sha[:12]} remote={remote_sha[:12] if remote_sha else 'none'}"
    )
    return worktree_path


def _cleanup_worktree_sync(worktree_path: str, repo_local_path: str, ticket: int, branch_id: str = "") -> None:
    branch = f"juanocampovgr/{branch_id or ticket}"
    subprocess.run(
        ["git", "-C", repo_local_path, "worktree", "remove", "--force", worktree_path],
        capture_output=True, text=True, check=False,
    )
    if Path(worktree_path).exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    subprocess.run(
        ["git", "-C", repo_local_path, "branch", "-D", branch],
        capture_output=True, text=True, check=False,
    )


async def setup_worktree(repo_local_path: str, ticket: int, branch_id: str = "", base: str = "origin/master") -> str:
    """Async wrapper — runs git operations in a thread so the event loop stays free."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _setup_worktree_sync, repo_local_path, ticket, branch_id, base)


async def cleanup_worktree(worktree_path: str, repo_local_path: str, ticket: int, branch_id: str = "") -> None:
    """Async wrapper — runs git operations in a thread so the event loop stays free."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _cleanup_worktree_sync, worktree_path, repo_local_path, ticket, branch_id)


# ── Repo inference ────────────────────────────────────────────────────────────

async def _infer_repo_from_plan(client: httpx.AsyncClient, repo_full: str, ticket: int) -> str:
    from github_api import gql_with_retry, ISSUE_COMMENTS_QUERY  # noqa: PLC0415
    try:
        owner, repo = repo_full.split("/")
        data = await gql_with_retry(client, ISSUE_COMMENTS_QUERY, {
            "owner": owner, "repo": repo, "number": ticket,
        })
        issue = data["repository"]["issue"]
        comments = issue["comments"]["nodes"]
        issue_body = issue.get("body") or ""

        for c in reversed(comments):
            body = c.get("body") or ""
            if "## Implementation Plan" not in body:
                continue
            body_lower = body.lower()
            if "affected**: android" in body_lower or "affected: android" in body_lower:
                return ANDROID_REPO_PATH
            if "affected**: ios" in body_lower or "affected: ios" in body_lower:
                return IOS_REPO_PATH
            if "affected**: backend" in body_lower or "affected: backend" in body_lower:
                return BACKEND_REPO_PATH

        body_lower = issue_body.lower()
        if "affected**: android" in body_lower or "affected: android" in body_lower:
            return ANDROID_REPO_PATH
        if "affected**: ios" in body_lower or "affected: ios" in body_lower:
            return IOS_REPO_PATH
        if "affected**: backend" in body_lower or "affected: backend" in body_lower:
            return BACKEND_REPO_PATH
        if ".kt" in issue_body or "android" in body_lower:
            return ANDROID_REPO_PATH
        if ".swift" in issue_body or "ios" in body_lower:
            return IOS_REPO_PATH
    except Exception as e:
        _log(f"    _infer_repo_from_plan ERROR: {e}")
    return ""


# ── LangGraph human-gate handler (v2) ────────────────────────────────────────

async def _handle_gate(
    client: httpx.AsyncClient,
    item: dict,
    graph_state_values: dict,
    interrupt_value: str,
    workflow,
    config: dict,
) -> bool:
    """Drive the three human / external-state interrupt gates.

    Returns True if the graph was resumed, False if no trigger was detected yet.

    Three gate types only — all waiting_*_marker branches have been removed;
    those signals now flow through result files read by the graph nodes themselves.
    """
    from langgraph.types import Command  # noqa: PLC0415

    ticket = item["issue_number"]
    repo_full = item["repo_full"]
    labels = item.get("labels", [])

    thread_id = config["configurable"]["thread_id"]

    async def resume(payload: dict) -> None:
        # Register in _active_threads so concurrent poll cycles don't spawn a
        # second _run_thread while this ainvoke is blocking in run_stage.
        _active_threads.add(thread_id)
        try:
            await workflow.ainvoke(Command(resume=payload), config)
        finally:
            _active_threads.discard(thread_id)
        _log(f"    #{ticket}: resumed gate '{interrupt_value}' with {payload}")

    # ── Gate 1: plan-approved label ───────────────────────────────────────────
    if interrupt_value == "waiting_plan_approval":
        if "plan-approved" in labels:
            _log(f"    #{ticket}: plan-approved label detected")
            await resume({"approved": True})
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "plan-approved")
            return True

    # ── Gate 2: impl-approved / followup-approved label ───────────────────────
    elif interrupt_value == "waiting_impl_approval":
        if "followup-approved" in labels:
            _log(f"    #{ticket}: followup-approved label detected")
            await resume({"label": "followup-approved"})
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "followup-approved")
            return True
        if "impl-approved" in labels:
            _log(f"    #{ticket}: impl-approved label detected")
            await resume({"label": "impl-approved"})
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "impl-approved")
            return True

    # ── Gate 3: PR outcome — CI status + labels (external-state gate) ─────────
    elif interrupt_value == "waiting_pr_outcome":
        # comments-approved label takes priority over CI checks
        if "comments-approved" in labels:
            _log(f"    #{ticket}: comments-approved label detected")
            await resume({"outcome": "respond"})
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "comments-approved")
            return True

        # Check nested state for pr_number (new nested schema: ship.pr_number)
        ship = graph_state_values.get("ship") or {}
        pr_number = ship.get("pr_number", 0) or graph_state_values.get("pr_number", 0)
        if pr_number:
            ci = await fetch_ci_status(client, repo_full, pr_number)
            if ci["status"] == "done":
                await resume({"outcome": "done"})
                return True
            if ci["status"] == "abandoned":
                _log(f"    #{ticket}: PR #{pr_number} closed without merge → needs_human")
                await resume({"outcome": "needs_human"})
                return True
            if ci["status"] == "fail":
                ship_state = graph_state_values.get("ship") or {}
                ci_fix_count = ship_state.get("ci_fix_count", 0) or graph_state_values.get("ci_fix_count", 0)
                try:
                    from graph.nodes._base import MAX_CI_FIX_ATTEMPTS  # noqa: PLC0415
                except ImportError:
                    MAX_CI_FIX_ATTEMPTS = 3
                if ci_fix_count >= MAX_CI_FIX_ATTEMPTS:
                    _log(f"    #{ticket}: CI still failing after {ci_fix_count} fix attempts → needs_human")
                    await resume({"outcome": "needs_human"})
                    return True
                _log(f"    #{ticket}: CI failure detected → fix_ci")
                await resume({"outcome": "fix_ci"})
                return True

    else:
        _log(f"    #{ticket}: unknown interrupt value '{interrupt_value}' — no action")

    return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_initial_state(
    item: dict,
    ticket: int,
    jira_ticket_id: str | None,
    *,
    entry_point: str = "plan",
    initial_request: str = "",
    initial_request_url: str = "",
) -> dict[str, Any]:
    """Build the initial LangGraph state dict for a new graph thread (nested schema)."""
    identity: dict[str, Any] = {
        "ticket_number":  ticket,
        "item_id":        item["item_id"],
        "issue_node_id":  item["issue_node_id"],
        "repo":           item["repo"],
        "repo_full":      item["repo_full"],
        "labels":         item.get("labels", []),
        "is_spike":       _is_spike(item),
        "jira_ticket_id": jira_ticket_id or "",
        "entry_point":    entry_point,
    }
    state: dict[str, Any] = {
        "identity": identity,
        "request": {
            "initial_request":     initial_request,
            "initial_request_url": initial_request_url,
        },
        "errors":               [],
        "commit_shas":          [],
        "quality_history":      [],
        "run_history":          [],
        "responded_thread_ids": [],
        "followup_tickets":     [],
    }
    return state


# ── Dashboard helper ──────────────────────────────────────────────────────────

def _open_dashboard(item: dict, status: str | None = None) -> None:
    """Open the per-ticket dashboard window and announce the grab over the event bus.

    Safe to call multiple times: the terminal opener no-ops if a live dashboard
    pid is already on file, and the event log replays from offset 0 on attach.
    """
    ticket = item["issue_number"]
    title  = item.get("title", "") or ""
    _open_dashboard_terminal(ticket, title)
    graph_events.emit(ticket, None, "ticket_grabbed", {
        "ticket":   ticket,
        "title":    title,
        "repo":     item.get("repo", ""),
        "repo_full": item.get("repo_full", ""),
        "jira_id":  "",  # filled in by individual nodes when known
        "is_spike": _is_spike(item),
        "status":   status or item.get("status", ""),
    })


# ── Recovery helpers (used by reconcile_once dead-END branch) ─────────────────

async def _clear_checkpoint(thread_id: str) -> None:
    """Delete LangGraph checkpoint rows for a thread so a fresh run can start.

    Uses aiosqlite (bundled with langgraph-checkpoint-sqlite) to avoid blocking
    the event loop. The new run will repopulate the checkpoint as it executes.
    """
    try:
        import aiosqlite  # noqa: PLC0415
        async with aiosqlite.connect(str(GRAPH_DB_PATH)) as db:
            await db.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
            await db.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
            await db.commit()
        _log(f"    checkpoint cleared for thread_id='{thread_id}'")
    except Exception as e:
        _log(f"    WARNING: could not clear checkpoint for '{thread_id}': {e}")


def _compute_recovery_target(prev_values: dict) -> tuple[str | None, dict | None]:
    """Return (graph_node_name, failed_run_record) for the last non-done stage.

    Walks run_history from newest to oldest, returns the first failed stage.
    None / None when there is no failure recorded (e.g. successful Done ticket
    moved back to Ready To Pick Up — operator-driven full rerun).
    """
    history = prev_values.get("run_history") or []
    for entry in reversed(history):
        outcome = entry.get("outcome")
        if outcome and outcome != "done":
            node = _STAGE_TO_NODE.get(entry.get("stage", ""))
            if node:
                return node, entry
    return None, None


def _clear_failed_result_file(ticket: int, stage: str, prev_values: dict) -> None:
    """Delete the cached result file for the failed stage so it actually re-runs.

    Earlier successful stages keep their result files → run_stage's idempotency
    guard returns them instantly on the rerun (no wasted work).
    """
    try:
        path = compute_result_path(prev_values, stage)
        if path.exists():
            path.unlink()
            _log(f"    #{ticket}: removed failed result file {path.name}")
    except Exception as e:
        _log(f"    WARNING: could not remove failed result file for #{ticket} '{stage}': {e}")


# ── Main reconciliation loop ──────────────────────────────────────────────────

async def reconcile_once(workflow) -> None:
    """Three jobs: discover & grab, drive interrupt gates, supervise stale gates."""
    _log("polling board...")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            board = await fetch_board(client, PROJECT_OWNER, PROJECT_NUMBER)
        except Exception as e:
            _log(f"  ERROR: fetch failed: {e}")
            return

        _log(f"  board returned {len(board)} item(s)")

        for item in board:
            graph_events.emit(item["issue_number"], None, "heartbeat", {
                "status": item.get("status", ""),
            })

        for item in board:
            ticket = item["issue_number"]
            status = item["status"]
            thread_id = str(ticket)
            _log(f"  #{ticket} [{item['repo']}] status='{status}'")

            config = {"configurable": {"thread_id": thread_id}}

            try:
                graph_snapshot = await workflow.aget_state(config)
            except Exception as e:
                _log(f"    #{ticket}: ERROR getting graph state: {e}")
                continue

            has_thread = bool(graph_snapshot and graph_snapshot.values)

            if not has_thread:
                # ── Job 1: Discover & grab ─────────────────────────────────
                if status == "Ready To Pick Up":
                    try:
                        body = await fetch_issue_body(client, item["repo_full"], ticket)
                    except Exception as e:
                        _log(f"    #{ticket}: body fetch failed — retry next poll")
                        continue
                    if len(body.strip()) < MIN_DESCRIPTION_CHARS:
                        await _escalate_to_error(client, item, "Ticket has no usable description.")
                        continue

                    jira_ticket_id = extract_jira_ticket_id(item.get("title", ""), body)
                    if jira_ticket_id:
                        jira_ticket_id = make_branch_id(jira_ticket_id, item.get("title", ""))
                        _log(f"    #{ticket}: branch id '{jira_ticket_id}'")

                    _log(f"    #{ticket}: 'Ready To Pick Up' → starting new graph thread")
                    initial = _build_initial_state(
                        item, ticket, jira_ticket_id,
                        initial_request=body,
                        initial_request_url=f"https://github.com/{item['repo_full']}/issues/{ticket}",
                    )
                    _open_dashboard(item, status="AI Planning")
                    graph_events.emit(ticket, None, "status_changed", {"status": "AI Planning"})
                    asyncio.create_task(_run_thread(workflow, thread_id, initial))

                elif status == "Ready to Review then Plan" and "plan-approved" in item.get("labels", []):
                    # Recovery: plan-approved label present but no graph thread (DB reset / Error).
                    # Start a fresh thread routed straight to implementation.
                    _log(f"    #{ticket}: thread-less + plan-approved → recovery start at implementation")
                    try:
                        body = await fetch_issue_body(client, item["repo_full"], ticket)
                    except Exception as e:
                        _log(f"    #{ticket}: body fetch failed — retry next poll")
                        continue

                    jira_ticket_id = extract_jira_ticket_id(item.get("title", ""), body)
                    if jira_ticket_id:
                        jira_ticket_id = make_branch_id(jira_ticket_id, item.get("title", ""))

                    initial = _build_initial_state(
                        item, ticket, jira_ticket_id,
                        entry_point="implement",
                        initial_request=body,
                        initial_request_url=f"https://github.com/{item['repo_full']}/issues/{ticket}",
                    )
                    _open_dashboard(item, status="AI Implementation")
                    graph_events.emit(ticket, None, "status_changed", {"status": "AI Implementation"})
                    asyncio.create_task(_run_thread(workflow, thread_id, initial))
                    await _gql_remove_label(client, item["issue_node_id"], item["repo_full"], "plan-approved")

                else:
                    _log(f"    #{ticket}: no graph thread and not 'Ready To Pick Up' — skipping")
                continue

            # ── Thread exists — check interrupt status ─────────────────────
            interrupt_value: str | None = None
            if graph_snapshot.tasks:
                for task in graph_snapshot.tasks:
                    if task.interrupts:
                        interrupt_value = task.interrupts[0].value
                        break

            if interrupt_value is not None:
                # ── Job 2: Drive interrupt gates ───────────────────────────
                _log(f"    #{ticket}: at interrupt '{interrupt_value}'")
                try:
                    await _handle_gate(
                        client, item, graph_snapshot.values or {},
                        interrupt_value, workflow, config,
                    )
                except Exception as e:
                    _log(f"    #{ticket}: ERROR in _handle_gate: {e}")
                continue

            # ── Not at interrupt — check if we need to restart ─────────────
            if graph_snapshot.next and thread_id not in _active_threads:
                # Restart recovery: thread has pending work but no live task in this
                # process (poller restarted). Pass None — LangGraph resumes from last
                # checkpoint; run_stage() idempotency handles re-launch vs read.
                _log(f"    #{ticket}: restart recovery — resuming from checkpoint")
                _open_dashboard(item, status=status)
                asyncio.create_task(_run_thread(workflow, thread_id, None))
            elif graph_snapshot.next:
                _log(f"    #{ticket}: node running in this process — skip")
            elif status == "Ready To Pick Up":
                # ── Dead-END recovery ──────────────────────────────────────
                # Graph reached END (escalate_error / needs_human / done) and the
                # operator moved the ticket back to "Ready To Pick Up". Build a
                # recovery state and start a fresh thread that enters at
                # `node_recover` — which will route to the failed stage.
                prev_values = graph_snapshot.values or {}
                target, failed_run = _compute_recovery_target(prev_values)
                if target is None:
                    _log(f"    #{ticket}: graph at END + Ready To Pick Up but no failed stage found — fresh restart")
                    await _clear_checkpoint(thread_id)
                    continue  # next poll will hit the standard "Ready To Pick Up" path
                _log(f"    #{ticket}: dead-END recovery → resume at '{target}'")
                _clear_failed_result_file(ticket, (failed_run or {}).get("stage", ""), prev_values)
                await _clear_checkpoint(thread_id)
                recovery_state = {
                    **prev_values,
                    "identity": {
                        **(prev_values.get("identity") or {}),
                        "is_recovery":     True,
                        "recovery_target": target,
                    },
                }
                _open_dashboard(item, status=status)
                graph_events.emit(ticket, None, "status_changed", {"status": f"recovering → {target}"})
                asyncio.create_task(_run_thread(workflow, thread_id, recovery_state))
            else:
                _log(f"    #{ticket}: graph done — skip")


# ── Main entry point ──────────────────────────────────────────────────────────

_PID_FILE = PIPELINE_DIR / "poller.pid"


def _acquire_pid_lock() -> bool:
    """Write our PID to the lock file. Return False if another instance is running."""
    if _PID_FILE.exists():
        try:
            other_pid = int(_PID_FILE.read_text().strip())
            # Check if that process is actually alive
            import subprocess as _sp
            result = _sp.run(["kill", "-0", str(other_pid)], capture_output=True)
            if result.returncode == 0:
                _log(f"ERROR: another poller is already running (pid {other_pid}) — exiting")
                return False
        except Exception:
            pass  # stale file — safe to overwrite
    _PID_FILE.write_text(str(os.getpid()))
    return True


def _release_pid_lock() -> None:
    try:
        if _PID_FILE.exists() and _PID_FILE.read_text().strip() == str(os.getpid()):
            _PID_FILE.unlink()
    except Exception:
        pass


async def main():
    _validate_config()
    if not _acquire_pid_lock():
        sys.exit(1)
    _log("=== pipeline-poller starting (LangGraph edition) ===")
    _log(f"  project:   #{PROJECT_NUMBER} owner={PROJECT_OWNER}")
    _log(f"  claude:    {CLAUDE_BIN}")
    _log(f"  android:   {ANDROID_REPO_PATH or '(not set)'}")
    _log(f"  ios:       {IOS_REPO_PATH or '(not set)'}")
    _log(f"  backend:   {BACKEND_REPO_PATH or '(not set)'}")
    _log(f"  interval:  {POLL_INTERVAL}s  max_per_repo={_MAX_PER_REPO}  max_total={_MAX_TOTAL}")
    _log(f"  graph_db:  {GRAPH_DB_PATH}")
    _log(f"  results:   {RESULTS_DIR}")

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: PLC0415
    from graph.workflow import build_workflow  # noqa: PLC0415

    async with AsyncSqliteSaver.from_conn_string(str(GRAPH_DB_PATH)) as checkpointer:
        workflow = build_workflow(checkpointer)
        _log("  LangGraph workflow compiled and ready")

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _on_signal():
            _log("shutdown signal — killing spawned claude children")
            import process_utils
            process_utils.kill_pids(
                [p for p, _ in process_utils.find_pipeline_claude_pids()],
                grace_seconds=2.0, log=_log,
            )
            _release_pid_lock()
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _on_signal)

        while not stop_event.is_set():
            try:
                await reconcile_once(workflow)
            except Exception as e:
                _log(f"ERROR: reconcile error: {e}")
            _log(f"sleeping {POLL_INTERVAL}s until next poll")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

        _log("=== pipeline-poller stopped ===")
        _release_pid_lock()


async def reset_thread(ticket: int, *, dry_run: bool = False) -> None:
    """Reset a stuck pipeline thread: wipe checkpoints, results, and move board back.

    Usage:
        python pipeline_poller.py reset-thread <N> [--dry-run]
    """
    import shutil as _shutil
    import sqlite3 as _sqlite3

    thread_id = str(ticket)
    print(f"=== reset-thread #{ticket} {'(DRY RUN)' if dry_run else ''} ===")

    # 1. Delete LangGraph checkpoints for this thread
    if GRAPH_DB_PATH.exists():
        if dry_run:
            print(f"  [DRY] would DELETE FROM checkpoints/writes WHERE thread_id='{thread_id}'")
        else:
            with _sqlite3.connect(str(GRAPH_DB_PATH)) as con:
                con.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
                con.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
                con.commit()
            print(f"  deleted checkpoints for thread_id='{thread_id}'")
    else:
        print(f"  graph_db not found at {GRAPH_DB_PATH} — skipping checkpoint deletion")

    # 2. Remove result files for this ticket
    results_dir = RESULTS_DIR / thread_id
    if results_dir.exists():
        if dry_run:
            print(f"  [DRY] would remove result directory: {results_dir}")
        else:
            _shutil.rmtree(results_dir, ignore_errors=True)
            print(f"  removed result directory: {results_dir}")
    else:
        print(f"  no result directory at {results_dir}")

    # 3. Move board item back to "Ready To Pick Up" and post a reset comment
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            board = await fetch_board(client, PROJECT_OWNER, PROJECT_NUMBER)
        except Exception as e:
            print(f"  ERROR: could not fetch board: {e}")
            return

        item = next((i for i in board if i["issue_number"] == ticket), None)
        if item is None:
            print(f"  ticket #{ticket} not found on board — board move skipped")
            return

        if dry_run:
            print(f"  [DRY] would move #{ticket} to 'Ready To Pick Up' and post reset comment")
            return

        try:
            await _gql_move_status(client, item["item_id"], "Ready To Pick Up",
                                   PROJECT_NODE_ID, STATUS_FIELD_ID, STATUS)
            print(f"  moved #{ticket} to 'Ready To Pick Up'")
        except Exception as e:
            print(f"  WARNING: board move failed: {e}")

        try:
            body = (
                f"🔄 **Pipeline reset by operator**\n\n"
                f"Checkpoints and result files cleared. The ticket has been moved back to "
                f"**Ready To Pick Up** and will restart from scratch on the next poll.\n\n"
                f"<!-- pipeline-reset -->"
            )
            await post_issue_comment(client, item["issue_node_id"], body)
            print(f"  posted reset comment on #{ticket}")
        except Exception as e:
            print(f"  WARNING: could not post reset comment: {e}")

    print(f"=== reset-thread #{ticket} complete ===")


if __name__ == "__main__":
    import sys as _sys
    args = _sys.argv[1:]
    if args and args[0] == "reset-thread":
        if len(args) < 2:
            print("Usage: python pipeline_poller.py reset-thread <ticket_number> [--dry-run]")
            _sys.exit(1)
        _ticket = int(args[1])
        _dry = "--dry-run" in args
        asyncio.run(reset_thread(_ticket, dry_run=_dry))
    else:
        asyncio.run(main())
