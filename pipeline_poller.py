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
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import httpx

from github_api import (
    fetch_board,
    fetch_issue_body,
    issue_has_marker,
    find_marker_comment,
    move_status as _gql_move_status,
    remove_label as _gql_remove_label,
    post_issue_comment,
    fetch_ci_status,
)

# ── Config ───────────────────────────────────────────────────────────────────

def _gh_token() -> str:
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Could not get GitHub token from gh CLI — run `gh auth login` first")
    return result.stdout.strip()


GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN") or _gh_token()
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

STALE_THRESHOLD = {
    "AI Planning":         int(os.environ.get("STALE_PLAN_SECONDS",  "900")),
    "AI Implementation":   int(os.environ.get("STALE_IMPL_SECONDS",  "3600")),
    "Ready To Ship - AI":  int(os.environ.get("STALE_SHIP_SECONDS",  "1800")),
}

MIN_DESCRIPTION_CHARS = int(os.environ.get("MIN_DESCRIPTION_CHARS", "20"))
MAX_TICKET_FAILURES   = int(os.environ.get("MAX_TICKET_FAILURES", "3"))

PIPELINE_DIR  = Path(os.environ.get("PIPELINE_DIR", Path.home() / ".pipeline"))
STATE_FILE    = Path(os.environ.get("STATE_FILE", PIPELINE_DIR / "state.json"))
LOG_DIR       = Path(os.environ.get("LOG_DIR", PIPELINE_DIR / "logs"))
WORKTREES_DIR = PIPELINE_DIR / "worktrees"
GRAPH_DB_PATH = PIPELINE_DIR / "graph_checkpoints.db"

PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
WORKTREES_DIR.mkdir(parents=True, exist_ok=True)


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
    "Ready To Ship - AI": {
        "command":      "/ship-tickets",
        "tools":        "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "done_marker":  "<!-- ai-ship:done -->",
        "error_marker": "<!-- ai-ship:error -->",
        "next_status":  "In PR",
        "spawn_mode":   "headless",
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

# ── Human-gate configs ────────────────────────────────────────────────────────

HUMAN_GATES = {
    "Ready to Review then Plan": {
        "label": "plan-approved",
        "next_status": "AI Implementation",
    },
    "Ready to review Implementation": {
        "label": "impl-approved",
        "next_status": "Ready To Ship - AI",
    },
}

# New label gates (detected separately in reconcile loop)
NEW_LABEL_GATES = {
    "In PR": {
        "comments-approved": {
            "interrupt": "waiting_pr_outcome",
            "resume_payload": {"outcome": "respond"},
        },
    },
    "Ready to review Implementation": {
        "followup-approved": {
            "interrupt": "waiting_impl_approval",
            "resume_payload": {"label": "followup-approved"},
        },
        "impl-approved": {
            "interrupt": "waiting_impl_approval",
            "resume_payload": {"label": "impl-approved"},
        },
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


# ── Legacy state (for backward-compat with agentic_dev_pipe status command) ──

@dataclass
class TicketState:
    issue_number: int
    repo: str
    last_seen_status: str
    last_acted_status: str | None
    last_run_id: str | None
    updated_at: float
    last_fired_at: float | None = None
    worktree_path: str | None = None
    repo_local_path: str | None = None
    consecutive_failures: int = 0


def load_state() -> dict[int, TicketState]:
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text())
    result = {}
    for k, v in raw.items():
        if "last_spawned_at" in v:
            v["last_fired_at"] = v.pop("last_spawned_at")
        v.setdefault("last_fired_at", None)
        v.setdefault("worktree_path", None)
        v.setdefault("repo_local_path", None)
        v.setdefault("consecutive_failures", 0)
        result[int(k)] = TicketState(**v)
    return result


def save_state(state: dict[int, TicketState]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({str(k): asdict(v) for k, v in state.items()}, indent=2))
    tmp.replace(STATE_FILE)


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


# ── Interrupt → stage mapping (used by staleness detection and legacy sync) ───

INTERRUPT_TO_STAGE: dict[str, str] = {
    "waiting_plan_marker":        "AI Planning",
    "waiting_impl_marker":        "AI Implementation",
    "waiting_self_review_marker": "Self Review",
    "waiting_ship_marker":        "Ready To Ship - AI",
    "waiting_fix_ci_marker":      "AI-PR Assistance",
    "waiting_respond_marker":     "AI-PR Assistance",
    "waiting_followups_marker":   "Ready To Ship - AI",
}


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


# ── Spawners ──────────────────────────────────────────────────────────────────

async def spawn_headless(
    stage_status: str, command: str, allowed_tools: str, ticket: int,
    timeout_seconds: int | None = None,
) -> str:
    run_id = f"{stage_status.replace(' ', '_')}-{ticket}-{uuid.uuid4().hex[:8]}"
    log_path = LOG_DIR / f"{run_id}.log"
    lock = _ticket_locks.setdefault(ticket, asyncio.Lock())
    async with lock, _get_total_semaphore():
        cmd = [
            CLAUDE_BIN,
            "-p", f"{command} --ticket {ticket}",
            "--allowedTools", allowed_tools,
        ]
        _log(f"headless spawn for #{ticket} → {log_path.name}")
        with open(log_path, "wb") as logf:
            logf.write(
                f"=== run_id={run_id} ticket={ticket} stage='{stage_status}' "
                f"mode=headless started={time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n".encode()
            )
            logf.flush()
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=logf, stderr=asyncio.subprocess.STDOUT
            )
            try:
                if timeout_seconds:
                    rc = await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
                else:
                    rc = await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                rc = -1
                _log(f"run {run_id} timed out after {timeout_seconds}s — killed")
            logf.write(f"\n=== exited rc={rc} ===\n".encode())
        _log(f"run {run_id} exited rc={rc}")
    return run_id


def setup_worktree(repo_local_path: str, ticket: int, branch_id: str = "", base: str = "origin/master") -> str:
    branch = f"juanocampovgr/{branch_id or ticket}"
    worktree_path = str(WORKTREES_DIR / str(ticket))

    _log(f"  setup_worktree: repo={repo_local_path} branch={branch} base={base} worktree={worktree_path}")

    r = subprocess.run(
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

    result = subprocess.run(
        ["git", "-C", repo_local_path, "worktree", "add", "-b", branch, worktree_path, base],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    _log(f"  setup_worktree: SUCCESS at {worktree_path}")
    return worktree_path


def cleanup_worktree(worktree_path: str, repo_local_path: str, ticket: int, branch_id: str = "") -> None:
    branch = f"juanocampovgr/{branch_id or ticket}"
    r = subprocess.run(
        ["git", "-C", repo_local_path, "worktree", "remove", "--force", worktree_path],
        capture_output=True, text=True, check=False,
    )
    if Path(worktree_path).exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    subprocess.run(
        ["git", "-C", repo_local_path, "branch", "-D", branch],
        capture_output=True, text=True, check=False,
    )


def spawn_terminal(stage_status: str, command: str, allowed_tools: str, ticket: int,
                   worktree_path: str = "") -> str:
    import tempfile
    run_id = f"{stage_status.replace(' ', '_')}-{ticket}-{uuid.uuid4().hex[:8]}-terminal"
    win_title = f"Claude #{ticket} — {stage_status}"

    script_lines = ["#!/bin/zsh"]
    if worktree_path:
        script_lines.append(f'cd "{worktree_path}"')
    script_lines += [
        f"echo '=== Claude #{ticket} — {stage_status} ==='",
        f"echo ''",
        f"{CLAUDE_BIN} -p '{command} --ticket {ticket}' --allowedTools '{allowed_tools}'",
        f"echo ''",
        f"echo '=== done (press any key to close) ==='",
        f"read -k1",
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, prefix="claude_pipe_") as f:
        f.write("\n".join(script_lines) + "\n")
        script_path = f.name
    os.chmod(script_path, 0o755)

    result = subprocess.run(
        [
            "osascript",
            "-e", 'tell application "Terminal"',
            "-e", "activate",
            "-e", f'set t to do script "{script_path}"',
            "-e", f'set custom title of t to "{win_title}"',
            "-e", "end tell",
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript failed: {result.stderr.strip() or result.stdout.strip()}")
    return run_id


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


# ── LangGraph interrupt handler ───────────────────────────────────────────────

async def _handle_interrupt(
    client: httpx.AsyncClient,
    item: dict,
    graph_state_values: dict,
    interrupt_value: str,
    workflow,
    config: dict,
    legacy_state: dict[int, TicketState],
) -> bool:
    """Detect conditions and resume the graph when the right event has occurred.

    Returns True if the graph was resumed (a marker/label was found and acted on),
    False if no trigger was detected yet.
    """
    from langgraph.types import Command  # noqa: PLC0415

    ticket = item["issue_number"]
    repo_full = item["repo_full"]
    labels = item.get("labels", [])
    last_fired = graph_state_values.get("last_fired_at")
    _resumed = False

    async def resume(payload: dict) -> None:
        nonlocal _resumed
        await workflow.ainvoke(Command(resume=payload), config)
        _log(f"    #{ticket}: resumed graph with {payload}")
        _resumed = True

    # ── Plan marker ───────────────────────────────────────────────────────────
    if interrupt_value == "waiting_plan_marker":
        done = await issue_has_marker(client, repo_full, ticket,
                                      AI_STAGES["AI Planning"]["done_marker"], last_fired)
        if done:
            await resume({"outcome": "done"})
            return _resumed
        errored = await issue_has_marker(client, repo_full, ticket,
                                         AI_STAGES["AI Planning"]["error_marker"], last_fired)
        if errored:
            await resume({"outcome": "error"})
            return _resumed
        insufficient = await issue_has_marker(client, repo_full, ticket,
                                              "<!-- ai-plan:insufficient-context -->", last_fired)
        if insufficient:
            await resume({"outcome": "error"})

    # ── Plan approval ─────────────────────────────────────────────────────────
    elif interrupt_value == "waiting_plan_approval":
        if "plan-approved" in labels:
            _log(f"    #{ticket}: plan-approved label detected")
            await resume({"approved": True})
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "plan-approved")

    # ── Impl marker (code-tickets or spike-tickets) ───────────────────────────
    elif interrupt_value == "waiting_impl_marker":
        done = await issue_has_marker(client, repo_full, ticket,
                                      AI_STAGES["AI Implementation"]["done_marker"], last_fired)
        if done:
            await resume({"outcome": "done"})
            return _resumed
        errored = await issue_has_marker(client, repo_full, ticket,
                                         AI_STAGES["AI Implementation"]["error_marker"], last_fired)
        if errored:
            await resume({"outcome": "error"})

    # ── Self-review marker ────────────────────────────────────────────────────
    elif interrupt_value == "waiting_self_review_marker":
        done = await issue_has_marker(client, repo_full, ticket,
                                      AI_STAGES["Self Review"]["done_marker"], last_fired)
        if done:
            await resume({"outcome": "done"})
            return _resumed
        errored = await issue_has_marker(client, repo_full, ticket,
                                         AI_STAGES["Self Review"]["error_marker"], last_fired)
        if errored:
            await resume({"outcome": "error"})

    # ── Impl approval / followup-approved ────────────────────────────────────
    elif interrupt_value == "waiting_impl_approval":
        if "followup-approved" in labels:
            _log(f"    #{ticket}: followup-approved label detected")
            await resume({"label": "followup-approved"})
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "followup-approved")
        elif "impl-approved" in labels:
            _log(f"    #{ticket}: impl-approved label detected")
            await resume({"label": "impl-approved"})
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "impl-approved")

    # ── Ship marker ───────────────────────────────────────────────────────────
    elif interrupt_value == "waiting_ship_marker":
        done = await issue_has_marker(client, repo_full, ticket,
                                      AI_STAGES["Ready To Ship - AI"]["done_marker"], last_fired)
        if done:
            comment_body = await find_marker_comment(
                client, repo_full, ticket,
                AI_STAGES["Ready To Ship - AI"]["done_marker"], last_fired,
            )
            pr_number = 0
            if comment_body:
                m = re.search(r'github\.com/[^/]+/[^/]+/pull/(\d+)', comment_body)
                if m:
                    pr_number = int(m.group(1))
            await resume({"outcome": "done", "pr_number": pr_number})
            return _resumed
        errored = await issue_has_marker(client, repo_full, ticket,
                                         AI_STAGES["Ready To Ship - AI"]["error_marker"], last_fired)
        if errored:
            await resume({"outcome": "error"})
            return _resumed
        # Retry: user reset the ticket to impl-review and re-added impl-approved
        if "impl-approved" in labels:
            _log(f"    #{ticket}: impl-approved at waiting_ship_marker → retrying ship")
            await resume({"outcome": "retry"})
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "impl-approved")

    # ── PR outcome monitoring ─────────────────────────────────────────────────
    elif interrupt_value == "waiting_pr_outcome":
        # 1. comments-approved label takes priority
        if "comments-approved" in labels:
            _log(f"    #{ticket}: comments-approved label detected")
            await resume({"outcome": "respond"})
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "comments-approved")
            return _resumed

        # 2. Check CI status
        pr_number = graph_state_values.get("pr_number", 0)
        if pr_number:
            ci = await fetch_ci_status(client, repo_full, pr_number)
            if ci["status"] == "done":
                await resume({"outcome": "done"})
                return _resumed
            if ci["status"] == "fail":
                ci_fix_count = graph_state_values.get("ci_fix_count", 0)
                if ci_fix_count >= 3:
                    _log(f"    #{ticket}: CI still failing after {ci_fix_count} fix attempts")
                    await resume({"outcome": "needs_human"})
                    return _resumed
                _log(f"    #{ticket}: CI failure detected, spawning fix")
                await resume({"outcome": "fix_ci"})

    # ── CI fix marker ─────────────────────────────────────────────────────────
    elif interrupt_value == "waiting_fix_ci_marker":
        done = await issue_has_marker(client, repo_full, ticket,
                                      AI_STAGES["AI-PR Assistance (CI Fix)"]["done_marker"],
                                      last_fired)
        if done:
            # Move ticket back to In PR
            async with httpx.AsyncClient(timeout=30) as c2:
                await _gql_move_status(c2, item["item_id"], "In PR",
                                       PROJECT_NODE_ID, STATUS_FIELD_ID, STATUS)
            await resume({"outcome": "done"})
            return _resumed
        needs_human = await issue_has_marker(client, repo_full, ticket,
                                             AI_STAGES["AI-PR Assistance (CI Fix)"]["error_marker"],
                                             last_fired)
        if needs_human:
            await resume({"outcome": "needs_human"})

    # ── Review response marker ────────────────────────────────────────────────
    elif interrupt_value == "waiting_respond_marker":
        done = await issue_has_marker(client, repo_full, ticket,
                                      AI_STAGES["AI-PR Assistance (Review)"]["done_marker"],
                                      last_fired)
        if done:
            async with httpx.AsyncClient(timeout=30) as c2:
                await _gql_move_status(c2, item["item_id"], "In PR",
                                       PROJECT_NODE_ID, STATUS_FIELD_ID, STATUS)
            await resume({"outcome": "done"})
            return _resumed
        needs_human = await issue_has_marker(client, repo_full, ticket,
                                             AI_STAGES["AI-PR Assistance (Review)"]["error_marker"],
                                             last_fired)
        if needs_human:
            await resume({"outcome": "needs_human"})

    # ── Followups marker ──────────────────────────────────────────────────────
    elif interrupt_value == "waiting_followups_marker":
        done = await issue_has_marker(client, repo_full, ticket,
                                      AI_STAGES["Spike Follow-ups"]["done_marker"], last_fired)
        if done:
            await resume({"outcome": "done"})
            return _resumed
        errored = await issue_has_marker(client, repo_full, ticket,
                                         AI_STAGES["Spike Follow-ups"]["error_marker"], last_fired)
        if errored:
            await resume({"outcome": "error"})

    else:
        _log(f"    #{ticket}: unknown interrupt value '{interrupt_value}' — no action")

    return _resumed


# ── Legacy state sync (for agentic_dev_pipe status/metrics compat) ───────────

def _sync_legacy_state(
    legacy_state: dict[int, TicketState],
    board: list[dict],
    workflow,
) -> None:
    """Update legacy state.json from LangGraph thread state for backward compat."""
    now = time.time()
    for item in board:
        ticket = item["issue_number"]
        config = {"configurable": {"thread_id": str(ticket)}}
        try:
            graph_snapshot = workflow.get_state(config)
        except Exception:
            graph_snapshot = None

        gv = graph_snapshot.values if graph_snapshot and graph_snapshot.values else {}

        if ticket not in legacy_state:
            legacy_state[ticket] = TicketState(
                issue_number=ticket,
                repo=item["repo"],
                last_seen_status=item["status"],
                last_acted_status=None,
                last_run_id=None,
                updated_at=now,
            )
        ts = legacy_state[ticket]
        ts.last_seen_status = item["status"]
        ts.updated_at = now
        if gv:
            ts.last_fired_at = gv.get("last_fired_at")
            ts.last_run_id = gv.get("last_run_id")
            ts.worktree_path = gv.get("worktree_path")
            ts.repo_local_path = gv.get("repo_local_path")
            # Approximate last_acted_status from graph interrupt point
            interrupt_val = None
            if graph_snapshot and graph_snapshot.tasks:
                for task in graph_snapshot.tasks:
                    if task.interrupts:
                        interrupt_val = task.interrupts[0].value
                        break
            ts.last_acted_status = INTERRUPT_TO_STAGE.get(interrupt_val or "")


# ── Main reconciliation loop ──────────────────────────────────────────────────

async def reconcile_once(
    legacy_state: dict[int, TicketState],
    workflow,
) -> None:
    from langgraph.types import Command  # noqa: PLC0415

    _log("polling board...")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            board = await fetch_board(client, PROJECT_OWNER, PROJECT_NUMBER)
        except Exception as e:
            _log(f"  ERROR: fetch failed: {e}")
            return

        _log(f"  board returned {len(board)} item(s)")

        for item in board:
            ticket = item["issue_number"]
            status = item["status"]
            _log(f"  #{ticket} [{item['repo']}] status='{status}'")

            config = {"configurable": {"thread_id": str(ticket)}}

            try:
                graph_snapshot = await workflow.aget_state(config)
            except Exception as e:
                _log(f"    #{ticket}: ERROR getting graph state: {e}")
                continue

            has_thread = bool(graph_snapshot and graph_snapshot.values)

            if not has_thread:
                if status == "Ready To Pick Up":
                    # Tier 1: deterministic empty-description check
                    try:
                        body = await fetch_issue_body(client, item["repo_full"], ticket)
                    except Exception as e:
                        _log(f"    #{ticket}: WARNING: body fetch failed ({e}) — retry next poll")
                        continue
                    if len(body.strip()) < MIN_DESCRIPTION_CHARS:
                        await _escalate_to_error(client, item,
                            "This ticket has no usable description, so the agent has nothing to "
                            "work from. Add a description and move it back to **Ready To Pick Up**.")
                        continue

                    jira_ticket_id = extract_jira_ticket_id(item.get("title", ""), body)
                    if jira_ticket_id:
                        jira_ticket_id = make_branch_id(jira_ticket_id, item.get("title", ""))
                        _log(f"    #{ticket}: branch id '{jira_ticket_id}'")

                    _log(f"    #{ticket}: 'Ready To Pick Up' → starting new graph thread")
                    initial: dict[str, Any] = {
                        "ticket_number":   ticket,
                        "item_id":         item["item_id"],
                        "issue_node_id":   item["issue_node_id"],
                        "repo":            item["repo"],
                        "repo_full":       item["repo_full"],
                        "labels":          item.get("labels", []),
                        "is_spike":        _is_spike(item),
                        "errors":          [],
                        "ci_fix_count":    0,
                        "review_comment_round": 0,
                        "self_review_retry_count": 0,
                        "jira_ticket_id":  jira_ticket_id or "",
                    }
                    try:
                        await workflow.ainvoke(initial, config)
                        if ticket in legacy_state:
                            legacy_state[ticket].consecutive_failures = 0
                    except Exception as e:
                        _log(f"    #{ticket}: ERROR starting graph: {e}")
                        if ticket in legacy_state:
                            ts_entry = legacy_state[ticket]
                            ts_entry.consecutive_failures += 1
                            if ts_entry.consecutive_failures >= MAX_TICKET_FAILURES:
                                await _escalate_to_error(client, item,
                                    f"Graph start failed {ts_entry.consecutive_failures} consecutive "
                                    f"times. Last error: {e}")
                                ts_entry.consecutive_failures = 0
                elif status == "Ready to Review then Plan" and "plan-approved" in item.get("labels", []):
                    # Recovery: approved plan exists but graph thread was lost (ended in Error,
                    # DB reset, or ticket placed here manually). Start a fresh thread routed
                    # straight to implementation, reusing the approved plan comment already on
                    # the issue.
                    _log(f"    #{ticket}: thread-less + plan-approved → recovery start at implementation")
                    try:
                        body = await fetch_issue_body(client, item["repo_full"], ticket)
                    except Exception as e:
                        _log(f"    #{ticket}: WARNING: body fetch failed ({e}) — retry next poll")
                        continue

                    jira_ticket_id = extract_jira_ticket_id(item.get("title", ""), body)
                    if jira_ticket_id:
                        jira_ticket_id = make_branch_id(jira_ticket_id, item.get("title", ""))
                        _log(f"    #{ticket}: branch id '{jira_ticket_id}'")

                    initial: dict[str, Any] = {
                        "ticket_number":   ticket,
                        "item_id":         item["item_id"],
                        "issue_node_id":   item["issue_node_id"],
                        "repo":            item["repo"],
                        "repo_full":       item["repo_full"],
                        "labels":          item.get("labels", []),
                        "is_spike":        _is_spike(item),
                        "errors":          [],
                        "ci_fix_count":    0,
                        "review_comment_round": 0,
                        "self_review_retry_count": 0,
                        "jira_ticket_id":  jira_ticket_id or "",
                        "entry_point":     "implement",
                    }
                    try:
                        await workflow.ainvoke(initial, config)
                        await _gql_remove_label(client, item["issue_node_id"], item["repo_full"], "plan-approved")
                        if ticket in legacy_state:
                            legacy_state[ticket].consecutive_failures = 0
                    except Exception as e:
                        _log(f"    #{ticket}: ERROR starting recovery graph: {e}")
                        if ticket in legacy_state:
                            ts_entry = legacy_state[ticket]
                            ts_entry.consecutive_failures += 1
                            if ts_entry.consecutive_failures >= MAX_TICKET_FAILURES:
                                await _escalate_to_error(client, item,
                                    f"Recovery graph start failed {ts_entry.consecutive_failures} consecutive "
                                    f"times. Last error: {e}")
                                ts_entry.consecutive_failures = 0
                else:
                    _log(f"    #{ticket}: no graph thread and not 'Ready To Pick Up' — skipping")
                continue

            # Graph thread exists — find the interrupt point
            interrupt_value: str | None = None
            if graph_snapshot.tasks:
                for task in graph_snapshot.tasks:
                    if task.interrupts:
                        interrupt_value = task.interrupts[0].value
                        break

            if interrupt_value is None:
                _log(f"    #{ticket}: graph not at interrupt (next={graph_snapshot.next})")
                continue

            _log(f"    #{ticket}: at interrupt '{interrupt_value}'")
            gv = graph_snapshot.values or {}

            # Always try to detect completion first — a present marker always wins over a timeout
            resumed = False
            try:
                resumed = await _handle_interrupt(
                    client, item, gv, interrupt_value, workflow, config, legacy_state,
                )
                if ticket in legacy_state:
                    legacy_state[ticket].consecutive_failures = 0
            except Exception as e:
                _log(f"    #{ticket}: ERROR handling interrupt: {e}")
                if ticket in legacy_state:
                    ts_entry = legacy_state[ticket]
                    ts_entry.consecutive_failures += 1
                    if ts_entry.consecutive_failures >= MAX_TICKET_FAILURES:
                        await _escalate_to_error(client, item,
                            f"Pipeline failed {ts_entry.consecutive_failures} consecutive times at "
                            f"interrupt '{interrupt_value}'. Last error: {e}")
                        ts_entry.consecutive_failures = 0

            if resumed:
                continue

            # Stale spawn check — only if marker was not found and threshold elapsed
            stage = INTERRUPT_TO_STAGE.get(interrupt_value)
            threshold = STALE_THRESHOLD.get(stage) if stage else None
            fired = gv.get("last_fired_at")
            if threshold and fired and (time.time() - fired) > threshold:
                _log(f"    #{ticket}: stale spawn at '{stage}' — escalating to Error")
                kill_existing_claude_for_ticket(ticket)
                await _escalate_to_error(client, item,
                    f"No result appeared within {threshold // 60} min while waiting at "
                    f"'{stage}'. The spawned Claude session likely died or stalled.")

        # Sync legacy state for backward compat with agentic_dev_pipe
        try:
            _sync_legacy_state(legacy_state, board, workflow)
            save_state(legacy_state)
        except Exception as e:
            _log(f"  WARNING: legacy state sync failed: {e}")


# ── Main entry point ──────────────────────────────────────────────────────────

async def main():
    _validate_config()
    _log("=== pipeline-poller starting (LangGraph edition) ===")
    _log(f"  project:   #{PROJECT_NUMBER} owner={PROJECT_OWNER}")
    _log(f"  claude:    {CLAUDE_BIN}")
    _log(f"  android:   {ANDROID_REPO_PATH or '(not set)'}")
    _log(f"  ios:       {IOS_REPO_PATH or '(not set)'}")
    _log(f"  backend:   {BACKEND_REPO_PATH or '(not set)'}")
    _log(f"  interval:  {POLL_INTERVAL}s  max_per_repo={_MAX_PER_REPO}  max_total={_MAX_TOTAL}")
    _log(f"  state:     {STATE_FILE}")
    _log(f"  graph_db:  {GRAPH_DB_PATH}")

    legacy_state = load_state()
    _log(f"  loaded {len(legacy_state)} tickets from legacy state")

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
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _on_signal)

        while not stop_event.is_set():
            try:
                await reconcile_once(legacy_state, workflow)
            except Exception as e:
                _log(f"ERROR: reconcile error: {e}")
            _log(f"sleeping {POLL_INTERVAL}s until next poll")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

        _log("=== pipeline-poller stopped ===")


if __name__ == "__main__":
    asyncio.run(main())
