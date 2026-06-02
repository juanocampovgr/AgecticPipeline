"""GitHub GraphQL/REST API utilities — extracted from pipeline_poller.py."""

import os
import subprocess
import time
from datetime import datetime
from typing import Any

import httpx


# ── Auth ─────────────────────────────────────────────────────────────────────

def _get_token() -> str:
    t = os.environ.get("GITHUB_TOKEN")
    if t:
        return t
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("No GITHUB_TOKEN set and `gh auth token` failed — run `gh auth login`")
    return result.stdout.strip()


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── GraphQL queries & mutations ──────────────────────────────────────────────

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

PR_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      url
      number
      headRefName
      state
      reviewThreads(first: 50) {
        nodes {
          id
          isResolved
          comments(first: 10) {
            nodes {
              id
              databaseId
              body
              path
              line
              author { login }
              createdAt
            }
          }
        }
      }
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

ADD_ISSUE_COMMENT_MUTATION = """
mutation($subjectId: ID!, $body: String!) {
  addComment(input: { subjectId: $subjectId, body: $body }) {
    commentEdge { node { id } }
  }
}
"""

REPLY_TO_REVIEW_THREAD_MUTATION = """
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {
    pullRequestReviewThreadId: $threadId,
    body: $body
  }) {
    comment { id }
  }
}
"""

REPO_ID_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) { id }
}
"""

CREATE_ISSUE_MUTATION = """
mutation($repositoryId: ID!, $title: String!, $body: String!, $labelIds: [ID!]) {
  createIssue(input: {
    repositoryId: $repositoryId,
    title: $title,
    body: $body,
    labelIds: $labelIds
  }) {
    issue { number url }
  }
}
"""


# ── Core request helpers ──────────────────────────────────────────────────────

async def gql(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    resp = await client.post(
        "https://api.github.com/graphql",
        headers={
            "Authorization": f"Bearer {_get_token()}",
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
    """Call gql() with exponential-backoff retry (2s, 4s, 8s). Auth errors fail-fast."""
    import asyncio
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(retries):
        try:
            return await gql(client, query, variables)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise
            last_exc = e
        except Exception as e:
            last_exc = e
        if attempt < retries - 1:
            wait = 2 ** (attempt + 1)
            _log(f"  gql retry {attempt + 1}/{retries - 1} in {wait}s ({last_exc})")
            await asyncio.sleep(wait)
    raise last_exc


# ── Board fetching ────────────────────────────────────────────────────────────

async def fetch_board(
    client: httpx.AsyncClient,
    owner: str,
    project_number: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = await gql_with_retry(client, PROJECT_QUERY, {
            "owner": owner, "number": project_number, "cursor": cursor,
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
                "labels":        [lbl["name"] for lbl in content["labels"]["nodes"]],
            })
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return items


# ── Marker detection ──────────────────────────────────────────────────────────

async def issue_has_marker(
    client: httpx.AsyncClient,
    repo_full: str,
    number: int,
    marker: str,
    after_timestamp: float | None = None,
) -> bool:
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
                continue
        return True
    return False


# ── Status / label mutations ──────────────────────────────────────────────────

async def move_status(
    client: httpx.AsyncClient,
    item_id: str,
    target_status: str,
    project_node_id: str,
    status_field_id: str,
    status_map: dict[str, str],
) -> None:
    option_id = status_map.get(target_status)
    if not option_id:
        _log(f"    WARNING: no status option ID for '{target_status}' — skipping move")
        return
    await gql_with_retry(client, UPDATE_STATUS_MUTATION, {
        "projectId": project_node_id,
        "itemId":    item_id,
        "fieldId":   status_field_id,
        "optionId":  option_id,
    })
    _log(f"    moved → {target_status}")


async def remove_label(
    client: httpx.AsyncClient,
    issue_node_id: str,
    repo_full: str,
    label_name: str,
) -> None:
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


async def post_issue_comment(
    client: httpx.AsyncClient,
    issue_node_id: str,
    body: str,
) -> None:
    await gql_with_retry(client, ADD_ISSUE_COMMENT_MUTATION, {
        "subjectId": issue_node_id,
        "body": body,
    })


# ── CI status (REST API) ──────────────────────────────────────────────────────

async def fetch_ci_status(
    client: httpx.AsyncClient,
    repo_full: str,
    pr_number: int,
) -> dict:
    """Return {"status": "pass"|"fail"|"pending"|"unknown", "failed_runs": [...]}."""
    owner, repo = repo_full.split("/")
    token = _get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Get the PR to find the head SHA
    resp = await client.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers=headers,
    )
    if resp.status_code == 404:
        return {"status": "unknown", "failed_runs": []}
    resp.raise_for_status()
    pr_data = resp.json()
    sha = pr_data["head"]["sha"]
    pr_state = pr_data["state"]
    merged = pr_data.get("merged", False)

    if pr_state == "closed" or merged:
        return {"status": "done", "failed_runs": [], "merged": merged}

    # Get check runs for the head commit
    resp = await client.get(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-runs",
        headers=headers,
        params={"per_page": 50},
    )
    resp.raise_for_status()
    runs_data = resp.json()
    runs = runs_data.get("check_runs", [])

    if not runs:
        return {"status": "pending", "failed_runs": []}

    failed = [
        {
            "name": r["name"],
            "conclusion": r["conclusion"],
            "details_url": r.get("details_url", ""),
            "output_title": r.get("output", {}).get("title", ""),
            "output_summary": r.get("output", {}).get("summary", ""),
        }
        for r in runs
        if r["conclusion"] in ("failure", "timed_out", "action_required")
    ]
    in_progress = any(r["status"] in ("in_progress", "queued", "waiting") for r in runs)

    if failed:
        return {"status": "fail", "failed_runs": failed}
    if in_progress:
        return {"status": "pending", "failed_runs": []}
    return {"status": "pass", "failed_runs": []}


# ── PR review comments ────────────────────────────────────────────────────────

async def fetch_pr_review_threads(
    client: httpx.AsyncClient,
    repo_full: str,
    pr_number: int,
) -> list[dict]:
    """Return unresolved review threads with their comments."""
    owner, repo = repo_full.split("/")
    data = await gql_with_retry(client, PR_REVIEW_THREADS_QUERY, {
        "owner": owner, "repo": repo, "number": pr_number,
    })
    pr = data["repository"]["pullRequest"]
    threads = []
    for thread in pr["reviewThreads"]["nodes"]:
        if thread["isResolved"]:
            continue
        threads.append({
            "thread_id": thread["id"],
            "comments": thread["comments"]["nodes"],
        })
    return threads


async def reply_to_review_thread(
    client: httpx.AsyncClient,
    thread_id: str,
    body: str,
) -> None:
    await gql_with_retry(client, REPLY_TO_REVIEW_THREAD_MUTATION, {
        "threadId": thread_id,
        "body": body,
    })


# ── Issue creation (for follow-up tickets) ────────────────────────────────────

async def create_issue(
    client: httpx.AsyncClient,
    repo_full: str,
    title: str,
    body: str,
    label_names: list[str] | None = None,
) -> dict:
    owner, repo = repo_full.split("/")
    repo_data = await gql_with_retry(client, REPO_ID_QUERY, {
        "owner": owner, "name": repo,
    })
    repo_id = repo_data["repository"]["id"]

    label_ids: list[str] = []
    if label_names:
        for name in label_names:
            lbl_data = await gql_with_retry(client, LABEL_ID_QUERY, {
                "owner": owner, "repo": repo, "name": name,
            })
            lbl = lbl_data["repository"]["label"]
            if lbl:
                label_ids.append(lbl["id"])

    result = await gql_with_retry(client, CREATE_ISSUE_MUTATION, {
        "repositoryId": repo_id,
        "title": title,
        "body": body,
        "labelIds": label_ids or None,
    })
    return result["createIssue"]["issue"]
