"""Open exactly one macOS Terminal window per ticket running the dashboard TUI.

Uses a pid-file lock at `~/.pipeline/dashboards/{ticket}.pid` so concurrent
callers (poll loop hitting a recovery path more than once) don't open extra
windows. If the pid file exists and the process is alive, the call is a no-op.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PIPELINE_DIR = Path(os.environ.get("PIPELINE_DIR", Path.home() / ".pipeline"))
DASHBOARDS_DIR = Path(os.environ.get("PIPELINE_DASHBOARDS_DIR", PIPELINE_DIR / "dashboards"))

# Repo root = parent of the package directory containing this file.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _existing_dashboard_pid(ticket: int) -> int | None:
    pid_file = DASHBOARDS_DIR / f"{ticket}.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        return None
    return pid if _pid_alive(pid) else None


def open_dashboard(ticket: int, title: str) -> None:
    """Open the dashboard Terminal window for `ticket`. No-op if already running."""
    if not ticket:
        return
    DASHBOARDS_DIR.mkdir(parents=True, exist_ok=True)

    if _existing_dashboard_pid(ticket) is not None:
        return

    safe_title = (title or "").replace('"', "'")[:60]
    win_title = f"Ticket #{ticket} — {safe_title}" if safe_title else f"Ticket #{ticket}"
    pid_file = DASHBOARDS_DIR / f"{ticket}.pid"
    venv_dir = PIPELINE_DIR / "dashboard_venv"
    venv_python = venv_dir / "bin" / "python"

    # Wrapper script: bootstraps a dedicated venv with rich on first run,
    # writes its own PID, then execs the dashboard. The PID we care about
    # for liveness is the Python process, so we use `exec`.
    script_lines = [
        "#!/bin/zsh",
        f'cd "{REPO_ROOT}"',
        # Bootstrap venv if it doesn't exist or rich is missing
        f'if ! "{venv_python}" -c "import rich" 2>/dev/null; then',
        f'  echo "Setting up dashboard venv..."',
        f'  python3 -m venv "{venv_dir}" --clear',
        f'  "{venv_python}" -m pip install rich --quiet',
        f'fi',
        f'echo $$ > "{pid_file}"',
        f'exec "{venv_python}" -m pipeline.dashboard --ticket {ticket}',
    ]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, prefix=f"dashboard_{ticket}_"
    ) as f:
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
        # Don't crash the pipeline if the user is on Linux or osascript fails;
        # the dashboard is observability, not load-bearing.
        print(
            f"[{time.strftime('%H:%M:%S')}] dashboard launch failed for #{ticket}: "
            f"{result.stderr.strip() or result.stdout.strip()}",
            flush=True,
        )
        return

    print(
        f"[{time.strftime('%H:%M:%S')}] dashboard opened for #{ticket}",
        flush=True,
    )
