"""
`agentic-dev-pipe` CLI: control & inspect the polling pipeline.

Usage:
    agentic-dev-pipe start | stop | restart | status | metrics | logs | poller
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

# ── Config ───────────────────────────────────────────────────────────────────

PIPELINE_ROOT = Path.home() / "Documents" / "Grindr" / "AgecticPipeline"
PYTHON_BIN    = PIPELINE_ROOT / ".venv" / "bin" / "python"
POLLER_SCRIPT = PIPELINE_ROOT / "pipeline_poller.py"
ENV_FILE      = PIPELINE_ROOT / ".env"

PIPELINE_DIR  = Path.home() / ".pipeline"
LOG_DIR       = PIPELINE_DIR / "logs"
STATE_FILE    = PIPELINE_DIR / "state.json"
POLLER_LOG    = PIPELINE_DIR / "poller.log"

PLIST_NAME    = "dev.juan.pipeline-poller"
PLIST_PATH    = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_NAME}.plist"


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


def _read_plist_env() -> dict[str, str]:
    """Extract EnvironmentVariables dict from the launchd plist (XML parsing)."""
    if not PLIST_PATH.exists():
        return {}
    try:
        import plistlib
        with open(PLIST_PATH, "rb") as f:
            pl = plistlib.load(f)
        return pl.get("EnvironmentVariables", {})
    except Exception:
        return {}


# ── Daemon lifecycle (launchctl) ─────────────────────────────────────────────

def cmd_start() -> None:
    if not PLIST_PATH.exists():
        print(f"error: plist not found at {PLIST_PATH}")
        print("Create it first — see pipeline setup docs.")
        return
    result = subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"loaded {PLIST_NAME}")
    else:
        print(f"launchctl load failed: {result.stderr.strip() or result.stdout.strip()}")


def cmd_stop() -> None:
    result = subprocess.run(
        ["launchctl", "unload", str(PLIST_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"unloaded {PLIST_NAME}")
    else:
        print(f"launchctl unload: {result.stderr.strip() or result.stdout.strip()}")


def cmd_restart() -> None:
    cmd_stop()
    time.sleep(1)
    cmd_start()


# ── Status ───────────────────────────────────────────────────────────────────

def _get_launchctl_pid() -> int | None:
    """Return PID if the launchd-managed daemon is running, else None."""
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == PLIST_NAME:
            pid_str = parts[0]
            return int(pid_str) if pid_str != "-" else None
    return None


def _get_last_poll_time() -> datetime | None:
    """Parse the most recent 'polling board' line from poller.log."""
    if not POLLER_LOG.exists():
        return None
    try:
        result = subprocess.run(
            ["grep", "polling board", str(POLLER_LOG)],
            capture_output=True, text=True,
        )
        lines = result.stdout.splitlines()
        if not lines:
            return None
        last = lines[-1]
        m = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", last)
        if not m:
            return None
        now = datetime.now()
        t = datetime.strptime(m.group(1), "%H:%M:%S").time()
        dt = datetime.combine(now.date(), t)
        # If the parsed time is in the future, the log line is from yesterday
        if dt > now:
            dt -= timedelta(days=1)
        return dt
    except Exception:
        return None


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def cmd_status() -> None:
    env = load_env()
    plist_env = _read_plist_env()
    interval_str = env.get("POLL_INTERVAL_SECONDS") or plist_env.get("POLL_INTERVAL_SECONDS", "120")
    interval = int(interval_str)
    pid = _get_launchctl_pid()

    if pid is None:
        print(f"daemon:  NOT RUNNING")
        print(f"  start: agentic-dev-pipe start")
        return

    print(f"daemon:  running  (pid {pid})")

    last = _get_last_poll_time()
    if last is None:
        print(f"  last poll:  (no poll yet)")
        print(f"  next poll:  within {interval}s")
    else:
        now = datetime.now()
        elapsed = int((now - last).total_seconds())
        next_poll = last + timedelta(seconds=interval)
        delta = (next_poll - now).total_seconds()
        stale_warn = ""
        if elapsed > 3600:
            stale_warn = "  ✗ poller not responding"
        elif elapsed > interval * 3:
            stale_warn = "  ⚠ poller appears stalled"
        print(f"  last poll:  {last.strftime('%H:%M:%S')}  ({elapsed}s ago){stale_warn}")
        if delta > 0:
            print(f"  next poll:  {next_poll.strftime('%H:%M:%S')}  (in {int(delta)}s)")
        else:
            print(f"  next poll:  overdue by {int(-delta)}s")

    print(f"  interval:   {interval}s")
    print(f"  poller log: {POLLER_LOG}")

    # Show active tickets from state.json
    state = _load_state()
    active = [
        (int(k), v)
        for k, v in state.items()
        if v.get("last_acted_status") is not None
    ]
    if active:
        print(f"\nactive tickets:")
        now_ts = time.time()
        for num, v in sorted(active):
            stage   = v["last_acted_status"]
            fired   = v.get("last_fired_at")
            elapsed = int(now_ts - fired) if fired else 0
            mins, secs = divmod(elapsed, 60)
            duration = f"{mins}m {secs}s" if mins else f"{secs}s"
            print(f"  #{num}  {v['repo']}  {stage}  (spawned {duration} ago)")
    else:
        print(f"\nno active tickets")


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

AI_ACTIONABLE_STATUSES = {
    "AI Planning",
    "AI Implementation",
    "Ready To Ship - AI",
}

_DEFAULT_STALE = {
    "AI Planning":         900,
    "AI Implementation":   3600,
    "Ready To Ship - AI":  1800,
}


def _stale_threshold(status: str, env: dict[str, str], plist_env: dict[str, str]) -> int:
    key_map = {
        "AI Planning":         "STALE_PLAN_SECONDS",
        "AI Implementation":   "STALE_IMPL_SECONDS",
        "Ready To Ship - AI":  "STALE_SHIP_SECONDS",
    }
    k = key_map.get(status, "")
    return int(env.get(k) or plist_env.get(k) or _DEFAULT_STALE.get(status, 900))


PROJECT_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content {
            __typename
            ... on Issue { number }
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


def _gh_token() -> str:
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Could not get GitHub token — run `gh auth login` first")
    return result.stdout.strip()


def fetch_counts(env: dict[str, str], plist_env: dict[str, str]) -> list[dict]:
    token  = env.get("GITHUB_TOKEN") or plist_env.get("GITHUB_TOKEN") or _gh_token()
    owner  = env.get("PROJECT_OWNER") or plist_env.get("PROJECT_OWNER")
    number = int(env.get("PROJECT_NUMBER") or plist_env.get("PROJECT_NUMBER", "2"))

    if not owner:
        raise RuntimeError("PROJECT_OWNER not found in .env or plist")

    items: list[dict] = []
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
                if status:
                    items.append({"issue_number": content["number"], "status": status})
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    return items


def cmd_metrics() -> None:
    env = load_env()
    plist_env = _read_plist_env()
    try:
        items = fetch_counts(env, plist_env)
    except Exception as e:
        print(f"error fetching board: {e}")
        sys.exit(1)

    counts: dict[str, int] = {s: 0 for s in STATUS_ORDER}
    for item in items:
        if item["status"] in counts:
            counts[item["status"]] += 1

    # Cross-reference state.json to detect stale spawns
    poller_state = _load_state()
    now = time.time()
    stales: list[tuple[int, str, int, int]] = []
    for item in items:
        status = item["status"]
        if status not in AI_ACTIONABLE_STATUSES:
            continue
        st = poller_state.get(str(item["issue_number"]))
        if not st:
            continue
        last_fired = st.get("last_fired_at")
        if last_fired is None:
            continue
        threshold = _stale_threshold(status, env, plist_env)
        elapsed = int(now - last_fired)
        if elapsed > threshold:
            stales.append((item["issue_number"], status, elapsed, threshold))

    owner  = env.get("PROJECT_OWNER") or plist_env.get("PROJECT_OWNER", "?")
    number = env.get("PROJECT_NUMBER") or plist_env.get("PROJECT_NUMBER", "?")
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

    if stales:
        print("\nStale spawns (artifact never appeared):")
        for ticket, status, elapsed, threshold in stales:
            mins  = elapsed // 60
            tmins = threshold // 60
            print(f"  #{ticket} in {status.ljust(width)}  \u26a0 {mins} min since spawn (threshold {tmins} min)")


# ── Logs ─────────────────────────────────────────────────────────────────────

def cmd_logs() -> None:
    """tail -F all per-run log files (headless Planning + Shipping stages)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_files = list(LOG_DIR.glob("*.log"))
    if not log_files:
        print(f"No run logs yet in {LOG_DIR}")
        print("(Implementation stage runs in Terminal — watch the Terminal window directly)")
        return
    os.execvp("tail", ["tail", "-F", *(str(p) for p in sorted(log_files))])


def cmd_poller() -> None:
    """tail -F the daemon's own log (poller heartbeat and state transitions)."""
    POLLER_LOG.parent.mkdir(parents=True, exist_ok=True)
    POLLER_LOG.touch(exist_ok=True)
    os.execvp("tail", ["tail", "-F", str(POLLER_LOG)])


def cmd_errors(n: int = 20) -> None:
    """Show the last N error/warning lines from poller.log."""
    if not POLLER_LOG.exists():
        print("No poller log found.")
        return

    error_keywords = (
        "WARNING:", "failed", "ERROR", "FATAL",
        "pipeline-error", "worktree setup failed", "cannot determine repo",
        "Terminal spawn failed", "git push failed", "git commit failed",
        "git add failed", "checkout -f master failed",
    )
    matches: list[str] = []
    with open(POLLER_LOG) as f:
        for line in f:
            line = line.rstrip()
            if any(kw.lower() in line.lower() for kw in error_keywords):
                matches.append(line)

    recent = matches[-n:]
    if not recent:
        print("No errors found in poller log.")
        return

    print(f"=== Last {len(recent)} pipeline error(s) ===\n")
    for line in recent:
        print(line)


# ── Dispatch ─────────────────────────────────────────────────────────────────

COMMANDS = {
    "start":   cmd_start,
    "stop":    cmd_stop,
    "restart": cmd_restart,
    "status":  cmd_status,
    "metrics": cmd_metrics,
    "logs":    cmd_logs,
    "poller":  cmd_poller,
    "errors":  cmd_errors,
}


def usage() -> None:
    print("usage: agentic-dev-pipe {start|stop|restart|status|metrics|logs|poller|errors}")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        usage()
        sys.exit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
