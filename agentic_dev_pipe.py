"""
`agentic-dev-pipe` CLI: control & inspect the polling pipeline.

Usage:
    agentic-dev-pipe start | stop | restart | status | metrics | logs | poller
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

# ── Config ───────────────────────────────────────────────────────────────────

PIPELINE_ROOT = Path.home() / "Documents" / "Grindr" / "AgecticPipeline"
PYTHON_BIN    = PIPELINE_ROOT / ".venv" / "bin" / "python"
POLLER_SCRIPT = PIPELINE_ROOT / "pipeline_poller.py"
ENV_FILE      = PIPELINE_ROOT / ".env"

STATE_FILE    = Path.home() / ".pipeline" / "state.json"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _get_pid() -> int | None:
    """Return PID of running poller process, or None."""
    r = subprocess.run(["pgrep", "-f", "pipeline_poller.py"], capture_output=True, text=True)
    pids = [p for p in r.stdout.strip().split() if p]
    return int(pids[0]) if pids else None


def _open_terminal_with_cmd(title: str, cmd: str) -> None:
    """Open a new Terminal.app window running cmd directly."""
    subprocess.run(
        [
            "osascript",
            "-e", 'tell application "Terminal"',
            "-e", f'set t to do script "{cmd}"',
            "-e", f'set custom title of t to "{title}"',
            "-e", "end tell",
        ],
        check=False,
    )


# ── Daemon lifecycle ─────────────────────────────────────────────────────────

def cmd_start() -> None:
    pid = _get_pid()
    if pid:
        print(f"already running (pid {pid})")
        return
    start_cmd = f"cd {PIPELINE_ROOT} && set -a && source {ENV_FILE} && set +a && {PYTHON_BIN} {POLLER_SCRIPT}"
    _open_terminal_with_cmd("Pipeline Poller", start_cmd)
    print("Pipeline poller started — live output in the Terminal window")


def cmd_stop() -> None:
    pid = _get_pid()
    if pid is None:
        print("Pipeline poller is not running")
        return
    subprocess.run(["kill", str(pid)], check=False)
    print(f"Stopped pipeline poller (pid {pid})")


def cmd_restart() -> None:
    cmd_stop()
    time.sleep(1)
    cmd_start()


# ── Status ───────────────────────────────────────────────────────────────────

def cmd_status() -> None:
    env = load_env()
    interval = int(env.get("POLL_INTERVAL_SECONDS", "120"))
    pid = _get_pid()

    if pid is None:
        print("poller: NOT RUNNING  (start with: agentic-dev-pipe start)")
        return

    print(f"poller: running (pid {pid})")
    print(f"  poll interval: {interval}s")
    print(f"  state file:    {STATE_FILE}")


# ── Metrics ──────────────────────────────────────────────────────────────────

STATUS_ORDER = [
    "Backlog",
    "AI Planning",
    "Ready to Review then Plan",
    "AI Implementation",
    "Ready to review Implementation",
    "Ready To Ship - AI",
    "In PR",
]

HUMAN_GATE_STATUSES = {
    "Ready to Review then Plan",
    "Ready to review Implementation",
}

PROJECT_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content { __typename }
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


def _gh_token() -> str:
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Could not get GitHub token — run `gh auth login` first")
    return result.stdout.strip()


def fetch_counts(env: dict[str, str]) -> dict[str, int]:
    token  = env.get("GITHUB_TOKEN") or _gh_token()
    owner  = env["PROJECT_OWNER"]
    number = int(env["PROJECT_NUMBER"])

    counts: dict[str, int] = {s: 0 for s in STATUS_ORDER}
    cursor: str | None = None

    with httpx.Client(timeout=30) as client:
        while True:
            r = client.post(
                "https://api.github.com/graphql",
                headers={"Authorization": f"Bearer {token}", "User-Agent": "agentic-dev-pipe-cli"},
                json={"query": PROJECT_QUERY, "variables": {"owner": owner, "number": number, "cursor": cursor}},
            )
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                raise RuntimeError(f"GraphQL error: {data['errors']}")
            page = data["data"]["user"]["projectV2"]["items"]
            for node in page["nodes"]:
                content = node.get("content") or {}
                if content.get("__typename") != "Issue":
                    continue
                status = next(
                    (fv["name"] for fv in node["fieldValues"]["nodes"]
                     if fv and fv.get("field", {}).get("name") == "Status"),
                    None,
                )
                if status and status in counts:
                    counts[status] += 1
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    return counts


def cmd_metrics() -> None:
    env = load_env()
    try:
        counts = fetch_counts(env)
    except Exception as e:
        print(f"error fetching board: {e}")
        sys.exit(1)

    owner  = env.get("PROJECT_OWNER", "?")
    number = env.get("PROJECT_NUMBER", "?")
    print(f"Board metrics (Project #{number} · {owner})\n")

    total = 0
    width = max(len(s) for s in STATUS_ORDER)
    for s in STATUS_ORDER:
        n = counts[s]
        total += n
        marker = "   \u2190 awaiting your approval" if (n > 0 and s in HUMAN_GATE_STATUSES) else ""
        print(f"  {s.ljust(width)}  {n:>3}{marker}")
    print(f"  {'\u2500' * width}  {'\u2500' * 3}")
    print(f"  {'Total'.ljust(width)}  {total:>3}")


# ── Logs (open in Terminal window) ───────────────────────────────────────────

def cmd_logs() -> None:
    """Open a Terminal window showing the poller output (via pgrep + live scroll)."""
    pid = _get_pid()
    if pid is None:
        print("poller is not running — start it first with: agentic-dev-pipe start")
        return
    # Attach to the running process's stdout using script(1) is complex;
    # instead just open a fresh terminal that runs the poller's print output
    # by re-opening the poller window (user can use pipe-state for state).
    print(f"Poller is running (pid {pid}). Live output is in the 'Pipeline Poller' Terminal window.")
    print(f"State snapshot: agentic-dev-pipe metrics  or  cat {STATE_FILE} | jq")


def cmd_poller() -> None:
    """Re-open the poller Terminal window (start the poller if not running)."""
    cmd_start()


# ── Dispatch ─────────────────────────────────────────────────────────────────

COMMANDS = {
    "start":   cmd_start,
    "stop":    cmd_stop,
    "restart": cmd_restart,
    "status":  cmd_status,
    "metrics": cmd_metrics,
    "logs":    cmd_logs,
    "poller":  cmd_poller,
}


def usage() -> None:
    print("usage: agentic-dev-pipe {start|stop|restart|status|metrics|logs|poller}")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        usage()
        sys.exit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
