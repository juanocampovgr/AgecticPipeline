"""
Polling pipeline orchestrator for GitHub Project #2 (juanocampovgr).

Polls the board every N seconds. For each ticket:
  - if status is AI-actionable: spawn claude if not already done; else move forward when artifact marker is present.
  - if status is a human review gate: check the approval label and move forward if set.
"""

import asyncio
import json
import os
import subprocess
import time
import uuid  # kept for run_id uniqueness
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

PIPELINE_DIR = Path(os.environ.get("PIPELINE_DIR", Path.home() / ".pipeline"))
STATE_FILE   = Path(os.environ.get("STATE_FILE", PIPELINE_DIR / "state.json"))
LOG_DIR      = Path(os.environ.get("LOG_DIR", PIPELINE_DIR / "logs"))
PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Status option IDs (board v2)
STATUS = {
    "Backlog":                        "f75ad846",
    "AI Planning":                    "61e4505c",
    "Ready to Review then Plan":      "47fc9ee4",
    "AI Implementation":              "df73e18b",
    "Ready to review Implementation": "98236657",
    "Ready To Ship - AI":             "c08c27e2",
    "In PR":                          "484abe4c",
}

# AI-actionable statuses: command, allowed-tools, marker, next-status-on-marker-found
AI_STAGES = {
    "AI Planning": {
        "command":     "/plan-github-tickets",
        "tools":       "Bash,Read,Grep,Glob,Agent",
        "done_marker": "<!-- ai-plan:done -->",
        "next_status": "Ready to Review then Plan",
    },
    "AI Implementation": {
        "command":     "/code-tickets",
        "tools":       "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "done_marker": "<!-- ai-impl:done -->",
        "next_status": "Ready to review Implementation",
    },
    "Ready To Ship - AI": {
        "command":     "/ship-tickets",
        "tools":       "Bash,Read,Grep,Glob,Edit,Write,Agent",
        "done_marker": "<!-- ai-ship:done -->",
        "next_status": "In PR",
    },
}

# Human-gate statuses: approval-label, next-status
HUMAN_GATES = {
    "Ready to Review then Plan":      {"label": "plan-approved",  "next_status": "AI Implementation"},
    "Ready to review Implementation": {"label": "impl-approved",  "next_status": "Ready To Ship - AI"},
}


# ── State ────────────────────────────────────────────────────────────────────

@dataclass
class TicketState:
    issue_number: int
    repo: str
    last_seen_status: str
    last_acted_status: str | None  # which AI stage we already spawned for
    last_run_id: str | None
    last_spawned_at: float | None  # epoch time when we last spawned for the current stage
    updated_at: float


def load_state() -> dict[int, TicketState]:
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text())
    result = {}
    for k, v in raw.items():
        v.setdefault("last_spawned_at", None)  # backward compat with old state files
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


async def fetch_board(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = await gql(client, PROJECT_QUERY, {
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
    data = await gql(client, ISSUE_COMMENTS_QUERY, {
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
    await gql(client, UPDATE_STATUS_MUTATION, {
        "projectId": PROJECT_NODE_ID,
        "itemId":    item_id,
        "fieldId":   STATUS_FIELD_ID,
        "optionId":  option_id,
    })
    print(f"    moved → {target_status}")


async def remove_label(client: httpx.AsyncClient, issue_node_id: str, repo_full: str, label_name: str) -> None:
    owner, repo = repo_full.split("/")
    data = await gql(client, LABEL_ID_QUERY, {
        "owner": owner, "repo": repo, "name": label_name,
    })
    label = data["repository"]["label"]
    if not label:
        return
    await gql(client, REMOVE_LABEL_MUTATION, {
        "labelableId": issue_node_id,
        "labelIds":    [label["id"]],
    })


# ── Claude subprocess ────────────────────────────────────────────────────────

def run_claude_in_terminal(stage_status: str, command: str, allowed_tools: str, ticket: int) -> str:
    """Open a Terminal.app window running claude directly. Fire-and-forget."""
    run_id = f"{stage_status.replace(' ', '_')}-{ticket}-{uuid.uuid4().hex[:8]}"
    win_title = f"Claude #{ticket} \u2014 {stage_status}"
    # Build the shell command that will run inside Terminal
    claude_cmd = (
        f"{CLAUDE_BIN} -p '{command} --ticket {ticket}' "
        f"--allowedTools '{allowed_tools}'; "
        f"echo ''; echo '=== done (press any key to close) ==='; read -k1"
    )
    subprocess.run(
        [
            "osascript",
            "-e", 'tell application "Terminal"',
            "-e", f'set t to do script "{claude_cmd}"',
            "-e", f'set custom title of t to "{win_title}"',
            "-e", "end tell",
        ],
        check=False,
    )
    print(f"[{time.strftime('%H:%M:%S')}] opened Terminal for #{ticket} ({stage_status})")
    return run_id


# ── Reconciliation loop ──────────────────────────────────────────────────────

async def reconcile_once(state: dict[int, TicketState]) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] polling board...")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            board = await fetch_board(client)
        except Exception as e:
            print(f"  fetch failed: {e}")
            return

        now = time.time()
        for item in board:
            ticket = item["issue_number"]
            status = item["status"]
            prev = state.get(ticket)
            if prev is None:
                prev = TicketState(
                    issue_number=ticket, repo=item["repo"],
                    last_seen_status=status, last_acted_status=None,
                    last_run_id=None, last_spawned_at=None, updated_at=now,
                )
                state[ticket] = prev
            else:
                old_status = prev.last_seen_status  # save before overwriting
                prev.last_seen_status = status
                prev.updated_at = now

                # Detect re-queue: ticket moved back to an AI stage it previously completed.
                # (e.g. human rejected the plan and moved it back to "AI Planning")
                if old_status != status and status in AI_STAGES and prev.last_acted_status == status:
                    print(f"  #{ticket}: re-queued to '{status}' — resetting for re-run")
                    prev.last_acted_status = None
                    prev.last_spawned_at = None

            # ── AI-actionable status ─────────────────────────────────────
            if status in AI_STAGES:
                cfg = AI_STAGES[status]

                if prev.last_acted_status == status:
                    # Already spawned for this stage — check for completion marker.
                    # Only accept markers posted AFTER we spawned (ignores stale markers from prior runs).
                    try:
                        done = await issue_has_marker(
                            client, item["repo_full"], ticket, cfg["done_marker"],
                            after_timestamp=prev.last_spawned_at,
                        )
                    except Exception as e:
                        print(f"  #{ticket}: marker check failed: {e}")
                        continue

                    if done:
                        print(f"  #{ticket} ({status}): artifact found → advancing")
                        try:
                            await move_status(client, item["item_id"], cfg["next_status"])
                            prev.last_acted_status = None
                            prev.last_spawned_at = None
                        except Exception as e:
                            print(f"    move failed: {e}")
                    continue  # waiting or just advanced

                # Haven't spawned for this stage yet (new ticket or re-queued after rejection)
                print(f"  → spawning {cfg['command']} for #{ticket}")
                run_id = run_claude_in_terminal(status, cfg["command"], cfg["tools"], ticket)
                prev.last_acted_status = status
                prev.last_run_id = run_id
                prev.last_spawned_at = time.time()
                continue

            # ── Human-gate status ────────────────────────────────────────
            if status in HUMAN_GATES:
                gate = HUMAN_GATES[status]
                if gate["label"] in item["labels"]:
                    print(f"  #{ticket} ({status}): label '{gate['label']}' present → advancing")
                    try:
                        await move_status(client, item["item_id"], gate["next_status"])
                        await remove_label(client, item["issue_node_id"], item["repo_full"], gate["label"])
                    except Exception as e:
                        print(f"    advance failed: {e}")
                continue

            # ── Anything else (Backlog, In PR) ───────────────────────────
            # do nothing

        save_state(state)


async def main():
    state = load_state()
    print(f"loaded state: {len(state)} tickets known")
    print(f"polling every {POLL_INTERVAL}s")
    while True:
        try:
            await reconcile_once(state)
        except Exception as e:
            print(f"reconcile error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
