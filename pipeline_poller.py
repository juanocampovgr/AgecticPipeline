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
import shutil
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
    issue_has_marker,
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

PIPELINE_DIR  = Path(os.environ.get("PIPELINE_DIR", Path.home() / ".pipeline"))
STATE_FILE    = Path(os.environ.get("STATE_FILE", PIPELINE_DIR / "state.json"))
LOG_DIR       = Path(os.environ.get("LOG_DIR", PIPELINE_DIR / "logs"))
WORKTREES_DIR = PIPELINE_DIR / "worktrees"
GRAPH_DB_PATH = PIPELINE_DIR / "graph_checkpoints.db"

PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
WORKTREES_DIR.mkdir(parents=True, exist_ok=True)


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
    # New statuses — add these in GitHub Project settings and set the env vars:
    "AI-PR Assistance": os.environ.get("STATUS_ID_AI_PR_ASSISTANCE", ""),
    "Needs Human":      os.environ.get("STATUS_ID_NEEDS_HUMAN", ""),
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

_MAX_PER_REPO = int(os.environ.get("MAX_CONCURRENT_PER_REPO", "1"))
_MAX_TOTAL    = int(os.environ.get("MAX_CONCURRENT_RUNS", "2"))

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


# ── Session guard ─────────────────────────────────────────────────────────────

def kill_existing_claude_for_ticket(ticket: int) -> None:
    result = subprocess.run(
        ["pgrep", "-f", f"claude.*--ticket {ticket}"],
        capture_output=True, text=True,
    )
    pids = [int(p) for p in result.stdout.strip().split() if p.strip().isdigit()]
    if not pids:
        return
    _log(f"  #{ticket}: killing {len(pids)} existing claude session(s): {pids}")
    for pid in pids:
        subprocess.run(["kill", "-SIGTERM", str(pid)], capture_output=True, check=False)
    time.sleep(3)
    for pid in pids:
        still_alive = subprocess.run(["kill", "-0", str(pid)], capture_output=True, check=False)
        if still_alive.returncode == 0:
            subprocess.run(["kill", "-SIGKILL", str(pid)], capture_output=True, check=False)


# ── Spawners ──────────────────────────────────────────────────────────────────

async def spawn_headless(stage_status: str, command: str, allowed_tools: str, ticket: int) -> str:
    run_id = f"{stage_status.replace(' ', '_')}-{ticket}-{uuid.uuid4().hex[:8]}"
    log_path = LOG_DIR / f"{run_id}.log"
    repo = ""  # We don't always have repo here; use total semaphore for headless
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
            rc = await proc.wait()
            logf.write(f"\n=== exited rc={rc} ===\n".encode())
        _log(f"run {run_id} exited rc={rc}")
    return run_id


def setup_worktree(repo_local_path: str, ticket: int) -> str:
    branch = f"juanocampovgr/{ticket}"
    worktree_path = str(WORKTREES_DIR / str(ticket))

    _log(f"  setup_worktree: repo={repo_local_path} branch={branch} worktree={worktree_path}")

    r = subprocess.run(
        ["git", "-C", repo_local_path, "worktree", "remove", "--force", worktree_path],
        capture_output=True, text=True, check=False,
    )
    if Path(worktree_path).exists():
        shutil.rmtree(worktree_path, ignore_errors=True)

    r = subprocess.run(
        ["git", "-C", repo_local_path, "fetch", "origin", "master:master"],
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
        ["git", "-C", repo_local_path, "worktree", "add", "-b", branch, worktree_path, "origin/master"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    _log(f"  setup_worktree: SUCCESS at {worktree_path}")
    return worktree_path


def cleanup_worktree(worktree_path: str, repo_local_path: str, ticket: int) -> None:
    branch = f"juanocampovgr/{ticket}"
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
) -> None:
    """Detect conditions and resume the graph when the right event has occurred."""
    from langgraph.types import Command  # noqa: PLC0415

    ticket = item["issue_number"]
    repo_full = item["repo_full"]
    labels = item.get("labels", [])
    last_fired = graph_state_values.get("last_fired_at")

    async def resume(payload: dict) -> None:
        await workflow.ainvoke(Command(resume=payload), config)
        _log(f"    #{ticket}: resumed graph with {payload}")

    # ── Plan marker ───────────────────────────────────────────────────────────
    if interrupt_value == "waiting_plan_marker":
        done = await issue_has_marker(client, repo_full, ticket,
                                      AI_STAGES["AI Planning"]["done_marker"], last_fired)
        if done:
            await resume({"outcome": "done"})
            return
        errored = await issue_has_marker(client, repo_full, ticket,
                                         AI_STAGES["AI Planning"]["error_marker"], last_fired)
        if errored:
            await resume({"outcome": "error"})

    # ── Plan approval ─────────────────────────────────────────────────────────
    elif interrupt_value == "waiting_plan_approval":
        if "plan-approved" in labels:
            _log(f"    #{ticket}: plan-approved label detected")
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "plan-approved")
            await resume({})

    # ── Impl marker (code-tickets or spike-tickets) ───────────────────────────
    elif interrupt_value == "waiting_impl_marker":
        done = await issue_has_marker(client, repo_full, ticket,
                                      AI_STAGES["AI Implementation"]["done_marker"], last_fired)
        if done:
            await resume({"outcome": "done"})
            return
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
            return
        errored = await issue_has_marker(client, repo_full, ticket,
                                         AI_STAGES["Self Review"]["error_marker"], last_fired)
        if errored:
            await resume({"outcome": "error"})

    # ── Impl approval / followup-approved ────────────────────────────────────
    elif interrupt_value == "waiting_impl_approval":
        if "followup-approved" in labels:
            _log(f"    #{ticket}: followup-approved label detected")
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "followup-approved")
            await resume({"label": "followup-approved"})
        elif "impl-approved" in labels:
            _log(f"    #{ticket}: impl-approved label detected")
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "impl-approved")
            await resume({"label": "impl-approved"})

    # ── Ship marker ───────────────────────────────────────────────────────────
    elif interrupt_value == "waiting_ship_marker":
        done = await issue_has_marker(client, repo_full, ticket,
                                      AI_STAGES["Ready To Ship - AI"]["done_marker"], last_fired)
        if done:
            await resume({"outcome": "done"})
            return
        errored = await issue_has_marker(client, repo_full, ticket,
                                         AI_STAGES["Ready To Ship - AI"]["error_marker"], last_fired)
        if errored:
            await resume({"outcome": "error"})

    # ── PR outcome monitoring ─────────────────────────────────────────────────
    elif interrupt_value == "waiting_pr_outcome":
        # 1. comments-approved label takes priority
        if "comments-approved" in labels:
            _log(f"    #{ticket}: comments-approved label detected")
            await _gql_remove_label(client, item["issue_node_id"], repo_full, "comments-approved")
            await resume({"outcome": "respond"})
            return

        # 2. Check CI status
        pr_number = graph_state_values.get("pr_number", 0)
        if pr_number:
            ci = await fetch_ci_status(client, repo_full, pr_number)
            if ci["status"] == "done":
                await resume({"outcome": "done"})
                return
            if ci["status"] == "fail":
                ci_fix_count = graph_state_values.get("ci_fix_count", 0)
                if ci_fix_count >= 3:
                    _log(f"    #{ticket}: CI still failing after {ci_fix_count} fix attempts")
                    await resume({"outcome": "needs_human"})
                    return
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
            return
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
            return
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
            return
        errored = await issue_has_marker(client, repo_full, ticket,
                                         AI_STAGES["Spike Follow-ups"]["error_marker"], last_fired)
        if errored:
            await resume({"outcome": "error"})

    else:
        _log(f"    #{ticket}: unknown interrupt value '{interrupt_value}' — no action")


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
            stage_for_interrupt = {
                "waiting_plan_marker": "AI Planning",
                "waiting_impl_marker": "AI Implementation",
                "waiting_self_review_marker": "Self Review",
                "waiting_ship_marker": "Ready To Ship - AI",
                "waiting_fix_ci_marker": "AI-PR Assistance",
                "waiting_respond_marker": "AI-PR Assistance",
                "waiting_followups_marker": "Ready To Ship - AI",
            }
            ts.last_acted_status = stage_for_interrupt.get(interrupt_val or "")


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
                    }
                    try:
                        await workflow.ainvoke(initial, config)
                    except Exception as e:
                        _log(f"    #{ticket}: ERROR starting graph: {e}")
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

            try:
                await _handle_interrupt(
                    client, item, gv, interrupt_value, workflow, config, legacy_state,
                )
            except Exception as e:
                _log(f"    #{ticket}: ERROR handling interrupt: {e}")

        # Sync legacy state for backward compat with agentic_dev_pipe
        _sync_legacy_state(legacy_state, board, workflow)
        save_state(legacy_state)


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

        while True:
            try:
                await reconcile_once(legacy_state, workflow)
            except Exception as e:
                _log(f"ERROR: reconcile error: {e}")
            _log(f"sleeping {POLL_INTERVAL}s until next poll")
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
