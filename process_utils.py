"""Utilities for finding and killing pipeline Claude processes.

Pure stdlib (subprocess, re, os, time) — no env reads, safe to import from both
pipeline_poller.py and agentic_dev_pipe.py.
"""

import os
import re
import subprocess
import time

PIPELINE_CLAUDE_PATTERN = r"claude.*--ticket"


def find_pipeline_claude_pids() -> list[tuple[int, int | None]]:
    """All pipeline claude procs as (pid, ticket_or_None).

    Runs `pgrep -fl 'claude.*--ticket'`, parses `--ticket (\\d+)` for the
    ticket number. Excludes os.getpid() and any cmdline containing 'pgrep'.
    """
    result = subprocess.run(
        ["pgrep", "-fl", "claude.*--ticket"],
        capture_output=True, text=True,
    )
    own_pid = os.getpid()
    procs: list[tuple[int, int | None]] = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line or "pgrep" in line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == own_pid:
            continue
        cmdline = parts[1]
        m = re.search(r"--ticket\s+(\d+)", cmdline)
        ticket = int(m.group(1)) if m else None
        procs.append((pid, ticket))
    return procs


def find_pipeline_claude_pids_for_ticket(ticket: int) -> list[int]:
    """pgrep -f 'claude.*--ticket {ticket}', minus own pid."""
    result = subprocess.run(
        ["pgrep", "-f", f"claude.*--ticket {ticket}"],
        capture_output=True, text=True,
    )
    own_pid = os.getpid()
    pids: list[int] = []
    for p in result.stdout.strip().split():
        if p.strip().isdigit():
            pid = int(p)
            if pid != own_pid:
                pids.append(pid)
    return pids


def kill_pids(pids: list[int], *, grace_seconds: float = 3.0, log=print) -> None:
    """SIGTERM all → wait grace_seconds → SIGKILL survivors."""
    if not pids:
        return
    for pid in pids:
        subprocess.run(["kill", "-SIGTERM", str(pid)], capture_output=True, check=False)
    time.sleep(grace_seconds)
    for pid in pids:
        still_alive = subprocess.run(["kill", "-0", str(pid)], capture_output=True, check=False)
        if still_alive.returncode == 0:
            log(f"  kill_pids: SIGKILL {pid}")
            subprocess.run(["kill", "-SIGKILL", str(pid)], capture_output=True, check=False)
