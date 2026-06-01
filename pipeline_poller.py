"""
Polling pipeline orchestrator for GitHub Project #2 (juanocampovgr).

Polls the board every N seconds. For each ticket:
  - if status is AI-actionable: spawn Claude if not already done; else move forward when artifact marker is present.
  - if status is a human review gate: check the approval label and move forward if set.

Spawn modes per stage:
  - AI Planning, Ready To Ship - AI:  headless `claude -p ...` async subprocess, logged to per-run files.
  - AI Implementation:                opens a Terminal.app window so the user can watch Claude live.

Stale-spawn watchdog: tracks `last_fired_at` per ticket. If the marker doesn't appear within a
configurable threshold, `agentic-dev-pipe metrics` flags the ticket with ⚠ stale.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

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

# Local paths to repos (used for worktree creation in AI Implementation stage)
ANDROID_REPO_PATH = os.environ.get("ANDROID_REPO_PATH", "")
IOS_REPO_PATH     = os.environ.get("IOS_REPO_PATH", "")
BACKEND_REPO_PATH = os.environ.get("BACKEND_REPO_PATH", "")
REPO_PATH_MAP: dict[str, str] = {
    "grindr-android-agent": ANDROID_REPO_PATH,
    "grindr-android":       ANDROID_REPO_PATH,
    "grindr-3.0-ios":       IOS_REPO_PATH,
    "backend":              BACKEND_REPO_PATH,
}

# Stale-spawn thresholds (seconds since last_fired_at without marker → flagged in metrics)
STALE_THRESHOLD = {
    "AI Planning":         int(os.environ.get("STALE_PLAN_SECONDS",  "900")),    # 15 min
    "AI Implementation":   int(os.environ.get("STALE_IMPL_SECONDS",  "3600")),   # 60 min
    "Ready To Ship - AI":  int(os.environ.get("STALE_SHIP_SECONDS",  "1800")),   # 30 min
}

PIPELINE_DIR  = Path(os.environ.get("PIPELINE_DIR", Path.home() / ".pipeline"))
STATE_FILE    = Path(os.environ.get("STATE_FILE", PIPELINE_DIR / "state.json"))
LOG_DIR       = Path(os.environ.get("LOG_DIR", PIPELINE_DIR / "logs"))
WORKTREES_DIR = PIPELINE_DIR / "worktrees"
PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
WORKTREES_DIR.mkdir(parents=True, exist_ok=True)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def _validate_config() -> None:
    """Fail fast with clear messages if critical configuration is missing."""
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


# Status option IDs (board v2)
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
}

# AI-actionable statuses
# spawn_mode: "headless" → `claude -p ...` async subprocess (output logged to file)
#             "terminal" → opens a Terminal.app window so the user can watch live
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
}

# Human-gate statuses: approval-label, next-status
HUMAN_GATES = {
    "Ready to Review then Plan":      {"label": "plan-approved",  "next_status": "AI Implementation"},
    "Ready to review Implementation": {"label": "impl-approved",  "next_status": "Ready To Ship - AI"},
}


def _is_spike(item: dict) -> bool:
    """Return True if the ticket is a spike (label 'spike' or 'spike' in title)."""
    if "spike" in [l.lower() for l in item.get("labels", [])]:
        return True
    return "spike" in item.get("title", "").lower()


# ── State ────────────────────────────────────────────────────────────────────

@dataclass
class TicketState:
    issue_number: int
    repo: str
    last_seen_status: str
    last_acted_status: str | None  # which AI stage we already spawned for
    last_run_id: str | None
    updated_at: float
    last_fired_at: float | None = None      # Unix ts when we last fired a spawn for the current stage
    worktree_path: str | None = None        # path to the git worktree for AI Implementation
    repo_local_path: str | None = None      # local repo path used to create the worktree


def load_state() -> dict[int, TicketState]:
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text())
    result = {}
    for k, v in raw.items():
        # Migrate old field name last_spawned_at → last_fired_at (backward compat)
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


# ── GitHub GraphQL ───────────────────────────────────────────────────────────

PROJECT_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content {
            __typename
            ... on Issue {
              id
              number
              title
              repository { nameWithOwner }
              labels(first: 20) { nodes { name } }
            }
          }
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""

ISSUE_COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      body
      comments(last: 30) { nodes { body createdAt } }
    }
  }
}
"""

UPDATE_STATUS_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId,
    itemId: $itemId,
    fieldId: $fieldId,
    value: { singleSelectOptionId: $optionId }
  }) { projectV2Item { id } }
}
"""

LABEL_ID_QUERY = """
query($owner: String!, $repo: String!, $name: String!) {
  repository(owner: $owner, name: $repo) {
    label(name: $name) { id }
  }
}
"""

REMOVE_LABEL_MUTATION = """
mutation($labelableId: ID!, $labelIds: [ID!]!) {
  removeLabelsFromLabelable(input: { labelableId: $labelableId, labelIds: $labelIds }) {
    clientMutationId
  }
}
"""


async def gql(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    resp = await client.post(
        "https://api.github.com/graphql",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "User-Agent": "pipeline-poller",
        },
        json={"query": query, "variables": variables},
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    return data["data"]


async def gql_with_retry(
    client: httpx.AsyncClient, query: str, variables: dict, *, retries: int = 3
) -> dict:
    """Call gql() with exponential-backoff retry for transient errors.

    Auth errors (401/403) are re-raised immediately — retrying them is pointless.
    Other errors (network, 5xx, GraphQL) are retried up to `retries` times (2s, 4s, 8s).
    """
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(retries):
        try:
            return await gql(client, query, variables)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise  # auth failure — don't retry
            last_exc = e
        except Exception as e:
            last_exc = e
        if attempt < retries - 1:
            wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
            _log(f"  gql retry {attempt + 1}/{retries - 1} in {wait}s ({last_exc})")
            await asyncio.sleep(wait)
    raise last_exc


async def fetch_board(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = await gql_with_retry(client, PROJECT_QUERY, {
            "owner": PROJECT_OWNER, "number": PROJECT_NUMBER, "cursor": cursor,
        })
        page = data["user"]["projectV2"]["items"]
        for node in page["nodes"]:
            content = node.get("content") or {}
            if content.get("__typename") != "Issue":
                continue
            status = next(
                (
                    fv["name"]
                    for fv in node["fieldValues"]["nodes"]
                    if fv and fv.get("field", {}).get("name") == "Status"
                ),
                None,
            )
            if not status:
                continue
            items.append({
                "item_id":       node["id"],
                "issue_node_id": content["id"],
                "issue_number":  content["number"],
                "title":         content.get("title", ""),
                "repo_full":     content["repository"]["nameWithOwner"],
                "repo":          content["repository"]["nameWithOwner"].split("/")[1],
                "status":        status,
                "labels":        [l["name"] for l in content["labels"]["nodes"]],
            })
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return items


async def issue_has_marker(
    client: httpx.AsyncClient,
    repo_full: str,
    number: int,
    marker: str,
    after_timestamp: float | None = None,
) -> bool:
    """Return True if marker exists in comments, optionally only counting comments posted after after_timestamp."""
    owner, repo = repo_full.split("/")
    data = await gql_with_retry(client, ISSUE_COMMENTS_QUERY, {
        "owner": owner, "repo": repo, "number": number,
    })
    comments = data["repository"]["issue"]["comments"]["nodes"]
    for c in comments:
        if marker not in (c.get("body") or ""):
            continue
        if after_timestamp is not None:
            created = datetime.fromisoformat(c["createdAt"].replace("Z", "+00:00"))
            if created.timestamp() <= after_timestamp:
                continue  # marker predates current spawn — stale, ignore
        return True
    return False


async def move_status(client: httpx.AsyncClient, item_id: str, target_status: str) -> None:
    option_id = STATUS[target_status]
    await gql_with_retry(client, UPDATE_STATUS_MUTATION, {
        "projectId": PROJECT_NODE_ID,
        "itemId":    item_id,
        "fieldId":   STATUS_FIELD_ID,
        "optionId":  option_id,
    })
    _log(f"    moved → {target_status}")


async def remove_label(client: httpx.AsyncClient, issue_node_id: str, repo_full: str, label_name: str) -> None:
    owner, repo = repo_full.split("/")
    data = await gql_with_retry(client, LABEL_ID_QUERY, {
        "owner": owner, "repo": repo, "name": label_name,
    })
    label = data["repository"]["label"]
    if not label:
        return
    await gql_with_retry(client, REMOVE_LABEL_MUTATION, {
        "labelableId": issue_node_id,
        "labelIds":    [label["id"]],
    })


# ── Failure reporting ────────────────────────────────────────────────────────

def post_failure_comment(repo_full: str, ticket: int, stage: str, error: str) -> None:
    """Post a visible failure comment on the GitHub issue (best-effort, never raises).

    Uses the gh CLI so no token plumbing is needed here.
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    body = (
        f"⚠️ **Pipeline failure — {stage}**\n\n"
        f"**Error:** {error}\n"
        f"**Time:** {ts}\n\n"
        f"The ticket has been paused. To retry, move it back to **{stage}** on the board.\n\n"
        f"<!-- pipeline-error:{stage} -->"
    )
    _log(f"  post_failure_comment: posting on #{ticket} in {repo_full} stage={stage!r}")
    result = subprocess.run(
        ["gh", "issue", "comment", str(ticket), "--repo", repo_full, "--body", body],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        _log(f"  WARNING: could not post failure comment on #{ticket}: {result.stderr.strip()}")
    else:
        _log(f"  posted failure comment on #{ticket}")


# ── Session guard ─────────────────────────────────────────────────────────────

def kill_existing_claude_for_ticket(ticket: int) -> None:
    """Kill any running claude process for this ticket to prevent duplicate sessions.

    Sends SIGTERM first (3s grace), then SIGKILL to any survivors.
    The worktree is cleaned up separately by setup_worktree().
    """
    _log(f"  #{ticket}: checking for existing claude sessions...")
    result = subprocess.run(
        ["pgrep", "-f", f"claude.*--ticket {ticket}"],
        capture_output=True, text=True,
    )
    pids = [int(p) for p in result.stdout.strip().split() if p.strip().isdigit()]
    if not pids:
        _log(f"  #{ticket}: no existing claude sessions found")
        return
    _log(f"  #{ticket}: killing {len(pids)} existing claude session(s): {pids}")
    for pid in pids:
        subprocess.run(["kill", "-SIGTERM", str(pid)], capture_output=True, check=False)
    _log(f"  #{ticket}: sent SIGTERM, waiting 3s...")
    time.sleep(3)
    survived = []
    for pid in pids:
        still_alive = subprocess.run(["kill", "-0", str(pid)], capture_output=True, check=False)
        if still_alive.returncode == 0:
            _log(f"  #{ticket}: pid {pid} still alive — sending SIGKILL")
            subprocess.run(["kill", "-SIGKILL", str(pid)], capture_output=True, check=False)
            survived.append(pid)
    if survived:
        _log(f"  #{ticket}: SIGKILL sent to {survived}")
    else:
        _log(f"  #{ticket}: all sessions terminated cleanly")


# ── Spawners ─────────────────────────────────────────────────────────────────

_ticket_locks: dict[int, asyncio.Lock] = {}
_concurrency = asyncio.Semaphore(int(os.environ.get("MAX_CONCURRENT_RUNS", "2")))


async def spawn_headless(stage_status: str, command: str, allowed_tools: str, ticket: int) -> str:
    """Spawn `claude -p ... --bare` and stream output to a per-run log file.

    Respects _concurrency semaphore (MAX_CONCURRENT_RUNS). Output is tailable via
    `agentic-dev-pipe logs`.
    """
    run_id = f"{stage_status.replace(' ', '_')}-{ticket}-{uuid.uuid4().hex[:8]}"
    log_path = LOG_DIR / f"{run_id}.log"
    lock = _ticket_locks.setdefault(ticket, asyncio.Lock())
    async with lock, _concurrency:
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
    """Create an isolated git worktree for the ticket branch, rooted at origin/master.

    If a worktree already exists for this ticket (re-queue), it is removed first and
    the local branch is recreated fresh from origin/master.
    Returns the worktree path.
    """
    branch = f"juanocampovgr/{ticket}"
    worktree_path = str(WORKTREES_DIR / str(ticket))

    _log(f"  setup_worktree: repo={repo_local_path} branch={branch} worktree={worktree_path}")

    # Remove any existing worktree for this ticket
    _log(f"  setup_worktree: removing any existing worktree at {worktree_path}")
    r = subprocess.run(
        ["git", "-C", repo_local_path, "worktree", "remove", "--force", worktree_path],
        capture_output=True, text=True, check=False,
    )
    _log(f"  setup_worktree: worktree remove rc={r.returncode} stderr={r.stderr.strip()!r}")
    if Path(worktree_path).exists():
        _log(f"  setup_worktree: directory still exists — removing with shutil")
        shutil.rmtree(worktree_path, ignore_errors=True)

    # Fetch and fast-forward local master to match origin/master
    _log(f"  setup_worktree: fetching origin master:master")
    r = subprocess.run(
        ["git", "-C", repo_local_path, "fetch", "origin", "master:master"],
        capture_output=True, text=True, check=False,
    )
    _log(f"  setup_worktree: fetch rc={r.returncode} stderr={r.stderr.strip()!r}")

    # If the repo's main worktree is currently on this branch, switch it to master first.
    # Use -f to discard any uncommitted changes (the worktree is meant as a clean base).
    _log(f"  setup_worktree: checking current branch of main worktree")
    current = subprocess.run(
        ["git", "-C", repo_local_path, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    _log(f"  setup_worktree: main worktree is on branch '{current}'")
    if current == branch:
        _log(f"  setup_worktree: main worktree is on target branch — checking out master first")
        r = subprocess.run(
            ["git", "-C", repo_local_path, "checkout", "-f", "master"],
            capture_output=True, text=True, check=False,
        )
        _log(f"  setup_worktree: checkout -f master rc={r.returncode} stderr={r.stderr.strip()!r}")
        if r.returncode != 0:
            raise RuntimeError(f"checkout -f master failed: {r.stderr.strip()}")
        _log(f"  setup_worktree: checked out master in main worktree (was on {branch})")

    # Remove local branch if it exists so we can recreate it fresh from master
    _log(f"  setup_worktree: deleting local branch {branch} (if exists)")
    r = subprocess.run(
        ["git", "-C", repo_local_path, "branch", "-D", branch],
        capture_output=True, text=True, check=False,
    )
    _log(f"  setup_worktree: branch -D rc={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")
    if r.returncode != 0 and "not found" not in r.stderr and branch not in r.stderr:
        raise RuntimeError(f"branch -D {branch} failed: {r.stderr.strip()}")

    # Create worktree with a new branch from origin/master
    _log(f"  setup_worktree: running git worktree add -b {branch} {worktree_path} origin/master")
    result = subprocess.run(
        ["git", "-C", repo_local_path, "worktree", "add", "-b", branch, worktree_path, "origin/master"],
        capture_output=True, text=True, check=False,
    )
    _log(f"  setup_worktree: worktree add rc={result.returncode} stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}")
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    _log(f"  setup_worktree: SUCCESS — worktree created at {worktree_path} on branch {branch}")
    return worktree_path


def cleanup_worktree(worktree_path: str, repo_local_path: str, ticket: int) -> None:
    """Remove the git worktree and delete the local branch after Claude has pushed the branch.

    Called by the poller after detecting the ai-impl:done marker. Best-effort — logs
    errors but never raises (a leftover worktree is harmless; it will be cleaned on re-queue).
    """
    branch = f"juanocampovgr/{ticket}"
    _log(f"  cleanup_worktree: removing worktree {worktree_path}")
    r = subprocess.run(
        ["git", "-C", repo_local_path, "worktree", "remove", "--force", worktree_path],
        capture_output=True, text=True, check=False,
    )
    _log(f"  cleanup_worktree: worktree remove rc={r.returncode} stderr={r.stderr.strip()!r}")
    if Path(worktree_path).exists():
        _log(f"  cleanup_worktree: directory still exists — removing with shutil")
        shutil.rmtree(worktree_path, ignore_errors=True)
    r = subprocess.run(
        ["git", "-C", repo_local_path, "branch", "-D", branch],
        capture_output=True, text=True, check=False,
    )
    _log(f"  cleanup_worktree: branch -D rc={r.returncode} stdout={r.stdout.strip()!r}")


def spawn_terminal(stage_status: str, command: str, allowed_tools: str, ticket: int,
                   worktree_path: str = "") -> str:
    """Open a Terminal.app window running claude so the user can watch live execution.

    Writes the command to a temp shell script first — avoids all AppleScript
    string-escaping issues that caused the original -1712 timeout errors.
    If worktree_path is given, the script cds there before running claude.

    Returns a run_id for state-tracking. Completion is detected later via the
    artifact marker on the GitHub issue.
    """
    import tempfile

    run_id = f"{stage_status.replace(' ', '_')}-{ticket}-{uuid.uuid4().hex[:8]}-terminal"
    win_title = f"Claude #{ticket} \u2014 {stage_status}"
    _log(f"  spawn_terminal: run_id={run_id}")
    _log(f"  spawn_terminal: worktree_path={worktree_path!r}")
    _log(f"  spawn_terminal: CLAUDE_BIN={CLAUDE_BIN}")
    _log(f"  spawn_terminal: command='{command} --ticket {ticket}'")

    # Write the command to a temp script so osascript never sees special characters
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
    _log(f"  spawn_terminal: script written to {script_path}")
    _log(f"  spawn_terminal: script contents:\n" + "\n".join(f"    {line}" for line in script_lines))

    _log(f"  spawn_terminal: calling osascript to open Terminal.app")
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
    _log(f"  spawn_terminal: osascript rc={result.returncode} stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}")
    if result.returncode != 0:
        raise RuntimeError(f"osascript failed: {result.stderr.strip() or result.stdout.strip()}")
    _log(f"  spawn_terminal: SUCCESS — Terminal opened for #{ticket} (run_id={run_id})")
    return run_id


# ── Repo inference ───────────────────────────────────────────────────────────

async def _infer_repo_from_plan(client: httpx.AsyncClient, repo_full: str, ticket: int) -> str:
    """Infer the local repo path from the issue's plan comment or body.

    Resolution order:
    1. Scan comments for '## Implementation Plan' + '**Affected**: Android/iOS/Backend'
    2. Scan the issue body for the same Affected line
    3. Keyword sniff: .kt/.android in body → Android; .swift/ios → iOS
    Returns empty string if no match is found.
    """
    _log(f"    _infer_repo_from_plan: fetching comments+body for #{ticket} in {repo_full}")
    try:
        owner, repo = repo_full.split("/")
        data = await gql_with_retry(client, ISSUE_COMMENTS_QUERY, {
            "owner": owner, "repo": repo, "number": ticket,
        })
        issue = data["repository"]["issue"]
        comments = issue["comments"]["nodes"]
        issue_body = issue.get("body") or ""

        # 1. Scan comments for Implementation Plan with Affected line
        _log(f"    _infer_repo_from_plan: found {len(comments)} comment(s), scanning for '## Implementation Plan'")
        for c in reversed(comments):
            body = c.get("body") or ""
            if "## Implementation Plan" not in body:
                continue
            _log(f"    _infer_repo_from_plan: found Implementation Plan comment ({len(body)} chars)")
            body_lower = body.lower()
            if "affected**: android" in body_lower or "affected: android" in body_lower:
                _log(f"    _infer_repo_from_plan: detected Android → {ANDROID_REPO_PATH}")
                return ANDROID_REPO_PATH
            if "affected**: ios" in body_lower or "affected: ios" in body_lower:
                _log(f"    _infer_repo_from_plan: detected iOS → {IOS_REPO_PATH}")
                return IOS_REPO_PATH
            if "affected**: backend" in body_lower or "affected: backend" in body_lower:
                _log(f"    _infer_repo_from_plan: detected Backend → {BACKEND_REPO_PATH}")
                return BACKEND_REPO_PATH
            snippet = body_lower[:300].replace("\n", " ")
            _log(f"    _infer_repo_from_plan: no Affected line found in plan snippet: {snippet!r}")
        _log(f"    _infer_repo_from_plan: no Implementation Plan comment found — checking issue body")

        # 2. Check issue body for Affected line
        body_lower = issue_body.lower()
        if "affected**: android" in body_lower or "affected: android" in body_lower:
            _log(f"    _infer_repo_from_plan: issue body has Android → {ANDROID_REPO_PATH}")
            return ANDROID_REPO_PATH
        if "affected**: ios" in body_lower or "affected: ios" in body_lower:
            _log(f"    _infer_repo_from_plan: issue body has iOS → {IOS_REPO_PATH}")
            return IOS_REPO_PATH
        if "affected**: backend" in body_lower or "affected: backend" in body_lower:
            _log(f"    _infer_repo_from_plan: issue body has Backend → {BACKEND_REPO_PATH}")
            return BACKEND_REPO_PATH

        # 3. Keyword sniff on issue body
        if ".kt" in issue_body or "android" in body_lower:
            _log(f"    _infer_repo_from_plan: body keyword sniff → Android {ANDROID_REPO_PATH}")
            return ANDROID_REPO_PATH
        if ".swift" in issue_body or "ios" in body_lower:
            _log(f"    _infer_repo_from_plan: body keyword sniff → iOS {IOS_REPO_PATH}")
            return IOS_REPO_PATH

        _log(f"    _infer_repo_from_plan: no match found in comments or body")
    except Exception as e:
        _log(f"    _infer_repo_from_plan: ERROR: {e}")
    return ""


# ── Reconciliation loop ──────────────────────────────────────────────────────

async def reconcile_once(state: dict[int, TicketState]) -> None:
    _log("polling board...")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            board = await fetch_board(client)
        except Exception as e:
            _log(f"  ERROR: fetch failed: {e}")
            return

        _log(f"  board returned {len(board)} item(s)")
        now = time.time()
        for item in board:
            ticket = item["issue_number"]
            status = item["status"]
            _log(f"  #{ticket} [{item['repo']}] status='{status}'")

            prev = state.get(ticket)
            if prev is None:
                _log(f"    new ticket — adding to state")
                prev = TicketState(
                    issue_number=ticket, repo=item["repo"],
                    last_seen_status=status, last_acted_status=None,
                    last_run_id=None, updated_at=now,
                )
                state[ticket] = prev
            else:
                old_status = prev.last_seen_status  # save before overwriting
                prev.last_seen_status = status
                prev.updated_at = now

                if old_status != status:
                    _log(f"    status changed: '{old_status}' → '{status}'")

                # Detect re-queue: ticket moved back to an AI stage it previously completed.
                if old_status != status and status in AI_STAGES and prev.last_acted_status == status:
                    _log(f"    re-queued to '{status}' — resetting for re-run")
                    prev.last_acted_status = None
                    prev.last_fired_at = None
                    prev.worktree_path = None

            # ── Ready To Pick Up → auto-advance (spike → AI Implementation, else → AI Planning) ─
            if status == "Ready To Pick Up":
                spike = _is_spike(item)
                target = "AI Implementation" if spike else "AI Planning"
                _log(f"    'Ready To Pick Up' detected (spike={spike}) — moving to '{target}'")
                try:
                    await move_status(client, item["item_id"], target)
                    status = target
                    item["status"] = target
                    prev.last_seen_status = target
                    # Reset prior state for the target stage so it spawns fresh
                    if prev.last_acted_status == target:
                        _log(f"    resetting prior '{target}' state (re-queue)")
                        prev.last_acted_status = None
                        prev.last_fired_at = None
                        prev.worktree_path = None
                        prev.repo_local_path = None
                    _log(f"    moved to '{target}' — falling through to spawn")
                except Exception as e:
                    _log(f"    ERROR: could not move to '{target}': {e}")
                    continue
                # fall through — status updated, handled by AI_STAGES block below

            # ── AI-actionable status ─────────────────────────────────────
            if status in AI_STAGES:
                cfg = AI_STAGES[status]

                if prev.last_acted_status == status:
                    # Already spawned — waiting for completion or error marker
                    elapsed = int(now - prev.last_fired_at) if prev.last_fired_at else 0
                    _log(f"    already spawned {elapsed}s ago — checking for markers")
                    try:
                        done = await issue_has_marker(
                            client, item["repo_full"], ticket, cfg["done_marker"],
                            after_timestamp=prev.last_fired_at,
                        )
                        errored = await issue_has_marker(
                            client, item["repo_full"], ticket, cfg["error_marker"],
                            after_timestamp=prev.last_fired_at,
                        ) if not done else False
                    except Exception as e:
                        _log(f"    ERROR: marker check failed: {e}")
                        continue

                    if errored:
                        _log(f"    error marker found — moving ticket to Error")
                        if status == "AI Implementation" and prev.worktree_path and prev.repo_local_path:
                            cleanup_worktree(prev.worktree_path, prev.repo_local_path, ticket)
                        try:
                            await move_status(client, item["item_id"], "Error")
                        except Exception as e:
                            _log(f"    ERROR: could not move to Error: {e}")
                        prev.last_acted_status = None
                        prev.last_fired_at = None
                        prev.worktree_path = None
                        prev.repo_local_path = None
                        save_state(state)
                    elif done:
                        _log(f"    done marker found!")
                        # For AI Implementation: Claude has already committed+pushed.
                        # Clean up the worktree and local branch.
                        if status == "AI Implementation" and prev.worktree_path:
                            repo_local = prev.repo_local_path or ""
                            if repo_local:
                                _log(f"    cleaning up worktree: {prev.worktree_path}")
                                cleanup_worktree(prev.worktree_path, repo_local, ticket)
                            else:
                                _log(f"    WARNING: repo_local_path not stored — skipping worktree cleanup")
                        _log(f"    artifact found → advancing to '{cfg['next_status']}'")
                        try:
                            await move_status(client, item["item_id"], cfg["next_status"])
                            prev.last_acted_status = None
                            prev.last_fired_at = None
                            prev.worktree_path = None
                            prev.repo_local_path = None
                        except Exception as e:
                            _log(f"    ERROR: move failed: {e}")
                    else:
                        _log(f"    no marker yet — still waiting (run_id={prev.last_run_id})")
                    continue  # waiting or just advanced

                # Haven't spawned for this stage yet (new ticket or re-queued after rejection)
                mode = cfg["spawn_mode"]
                command = cfg["command"]
                # Spikes in AI Implementation run headlessly via /spike-tickets (no worktree)
                if status == "AI Implementation" and _is_spike(item):
                    mode = "headless"
                    command = "/spike-tickets"
                    _log(f"    spike ticket — overriding to headless /spike-tickets")
                _log(f"    → spawning '{command}' (mode={mode})")

                if mode == "terminal":
                    # Resolve local repo path — direct map, then label routing, then plan inference
                    repo_local = REPO_PATH_MAP.get(item["repo"], "")
                    _log(f"    REPO_PATH_MAP lookup for '{item['repo']}': '{repo_local}'")
                    if not repo_local:
                        labels_lower = [l.lower() for l in item.get("labels", [])]
                        _log(f"    checking labels for routing: {labels_lower}")
                        if "android" in labels_lower:
                            repo_local = ANDROID_REPO_PATH
                            _log(f"    label 'android' → {repo_local}")
                        elif "ios" in labels_lower:
                            repo_local = IOS_REPO_PATH
                            _log(f"    label 'ios' → {repo_local}")
                        elif "backend" in labels_lower:
                            repo_local = BACKEND_REPO_PATH
                            _log(f"    label 'backend' → {repo_local}")
                    if not repo_local:
                        _log(f"    no routing label — inferring from plan comment/body...")
                        repo_local = await _infer_repo_from_plan(client, item["repo_full"], ticket)
                    if not repo_local:
                        err = (
                            f"Cannot determine local repo for '{item['repo']}'. "
                            f"Set ANDROID/IOS/BACKEND_REPO_PATH in the launchd plist, "
                            f"or add '**Affected**: Android/iOS/Backend' to the implementation plan."
                        )
                        _log(f"    ERROR: {err}")
                        post_failure_comment(item["repo_full"], ticket, status, err)
                        try:
                            await move_status(client, item["item_id"], "Error")
                        except Exception as me:
                            _log(f"    ERROR: could not move to Error: {me}")
                        prev.last_acted_status = None
                        prev.last_fired_at = None
                        save_state(state)
                        continue

                    # Kill any existing claude session for this ticket before spawning fresh
                    kill_existing_claude_for_ticket(ticket)

                    _log(f"    using repo: {repo_local}")
                    try:
                        _log(f"    calling setup_worktree(repo={repo_local}, ticket={ticket})")
                        worktree_path = setup_worktree(repo_local, ticket)
                    except Exception as e:
                        err = f"Worktree setup failed: {e}"
                        _log(f"    ERROR: {err}")
                        post_failure_comment(item["repo_full"], ticket, status, err)
                        try:
                            await move_status(client, item["item_id"], "Error")
                        except Exception as me:
                            _log(f"    ERROR: could not move to Error: {me}")
                        prev.last_acted_status = None
                        prev.last_fired_at = None
                        save_state(state)
                        continue

                    # Record BEFORE firing
                    prev.last_acted_status = status
                    prev.last_fired_at = time.time()
                    prev.worktree_path = worktree_path
                    prev.repo_local_path = repo_local
                    save_state(state)
                    try:
                        _log(f"    calling spawn_terminal for #{ticket} worktree={worktree_path}")
                        run_id = spawn_terminal(status, cfg["command"], cfg["tools"], ticket, worktree_path)
                    except Exception as e:
                        err = f"Terminal spawn failed: {e}"
                        _log(f"    ERROR: {err}")
                        post_failure_comment(item["repo_full"], ticket, status, err)
                        try:
                            await move_status(client, item["item_id"], "Error")
                        except Exception as me:
                            _log(f"    ERROR: could not move to Error: {me}")
                        prev.last_acted_status = None
                        prev.last_fired_at = None
                        prev.worktree_path = None
                        prev.repo_local_path = None
                        save_state(state)
                        continue
                    prev.last_run_id = run_id
                    save_state(state)
                else:
                    # Record BEFORE firing so watchdog works even if we crash mid-spawn
                    prev.last_acted_status = status
                    prev.last_fired_at = time.time()
                    save_state(state)
                    # Headless: spawn as background asyncio task so it runs across poll cycles
                    async def _spawn_headless(t=ticket, s=status, cmd=command, c=cfg, p=prev):
                        run_id = await spawn_headless(s, cmd, c["tools"], t)
                        p.last_run_id = run_id
                        save_state(state)
                    asyncio.create_task(_spawn_headless())
                continue

            # ── Human-gate status ────────────────────────────────────────
            if status in HUMAN_GATES:
                gate = HUMAN_GATES[status]
                _log(f"    human gate — checking for label '{gate['label']}' in {item['labels']}")
                if gate["label"] in item["labels"]:
                    # Spikes skip shipping — go straight to Done after impl review
                    if status == "Ready to review Implementation" and _is_spike(item):
                        next_status = "Done"
                        _log(f"    spike: skipping ship stage → advancing to 'Done'")
                    else:
                        next_status = gate["next_status"]
                    _log(f"    label '{gate['label']}' present → advancing to '{next_status}'")
                    try:
                        await move_status(client, item["item_id"], next_status)
                        await remove_label(client, item["issue_node_id"], item["repo_full"], gate["label"])
                    except Exception as e:
                        _log(f"    ERROR: advance failed: {e}")
                else:
                    _log(f"    label not present — waiting for human approval")
                continue

            # ── Anything else (Backlog, In PR) ───────────────────────────
            # do nothing

        save_state(state)


async def main():
    _validate_config()
    _log("=== pipeline-poller starting ===")
    _log(f"  project:  #{PROJECT_NUMBER} owner={PROJECT_OWNER}")
    _log(f"  claude:   {CLAUDE_BIN}")
    _log(f"  android:  {ANDROID_REPO_PATH or '(not set)'}")
    _log(f"  ios:      {IOS_REPO_PATH or '(not set)'}")
    _log(f"  backend:  {BACKEND_REPO_PATH or '(not set)'}")
    _log(f"  interval: {POLL_INTERVAL}s  max_concurrent={_concurrency._value}")
    _log(f"  state:    {STATE_FILE}")
    _log(f"  logs:     {LOG_DIR}")
    state = load_state()
    _log(f"loaded state: {len(state)} tickets known")
    for num, ts in state.items():
        _log(f"  #{num} repo={ts.repo} last_acted={ts.last_acted_status} worktree={ts.worktree_path} repo_local={ts.repo_local_path}")
    while True:
        try:
            await reconcile_once(state)
        except Exception as e:
            _log(f"ERROR: reconcile error: {e}")
        _log(f"sleeping {POLL_INTERVAL}s until next poll")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
