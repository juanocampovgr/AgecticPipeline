"""Stage runner — idempotent skill launcher + result-file awaiter.

run_stage() is the single entry point all machine nodes use:
  1. Compute a deterministic result_path (ticket/stage/attempt).
  2. If the result file already exists and is complete → return immediately (re-entry guard).
  3. If the file exists but is incomplete (crash mid-write) → await with timeout.
  4. Fresh entry → launch the skill (terminal or headless) and await the result file.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from graph.schemas import RunRecord, StageResult

if TYPE_CHECKING:
    from graph.state import TicketState

# ── Constants ─────────────────────────────────────────────────────────────────

PIPELINE_DIR  = Path(os.environ.get("PIPELINE_DIR", Path.home() / ".pipeline"))
RESULTS_DIR   = Path(os.environ.get("PIPELINE_RESULTS_DIR", PIPELINE_DIR / "results"))
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

RESULT_POLL_INTERVAL = 10  # seconds between result-file polls

# Retry config for transient subprocess failures (API 529, network errors, etc.)
MAX_SUBPROCESS_RETRIES = 3
SUBPROCESS_RETRY_BACKOFF = (2, 4, 8)  # seconds between retry attempts
# Byte patterns in the log tail that indicate a transient (retryable) error
TRANSIENT_PATTERNS = (b"overloaded_error", b"529", b"RateLimit", b"rate_limit_error", b"timeout")

# Per-stage timeout in seconds (how long to wait for the result file to appear)
STALE_THRESHOLD: dict[str, int] = {
    "AI Planning":         int(os.environ.get("STALE_PLAN_SECONDS",     "900")),
    "AI Implementation":   int(os.environ.get("STALE_IMPL_SECONDS",     "3600")),
    "AI Quality Check":    int(os.environ.get("STALE_QUALITY_SECONDS",  "2400")),
    "Ready To Ship - AI":  int(os.environ.get("STALE_SHIP_SECONDS",     "600")),
    "Self Review":         int(os.environ.get("STALE_SELF_REVIEW_SECONDS", "1800")),
    "Fix CI":              int(os.environ.get("STALE_FIX_CI_SECONDS",   "3600")),
    "Respond To Review":   int(os.environ.get("STALE_RESPOND_SECONDS",  "3600")),
    "Spike Followups":     int(os.environ.get("STALE_FOLLOWUPS_SECONDS","1800")),
}
_DEFAULT_TIMEOUT = 3600


# ── Late imports from pipeline_poller (avoid circular at module load time) ─────

def _claude_bin() -> str:
    """Return CLAUDE_BIN from pipeline_poller if importable, else env/default."""
    try:
        import pipeline_poller as _pp  # noqa: PLC0415
        return _pp.CLAUDE_BIN
    except Exception:
        return os.environ.get("CLAUDE_BIN", "claude")


def _log_dir() -> Path:
    """Return LOG_DIR from pipeline_poller if importable, else derive from PIPELINE_DIR."""
    try:
        import pipeline_poller as _pp  # noqa: PLC0415
        return _pp.LOG_DIR
    except Exception:
        d = PIPELINE_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _worktrees_dir() -> Path:
    """Return WORKTREES_DIR from pipeline_poller if importable, else derive from PIPELINE_DIR."""
    try:
        import pipeline_poller as _pp  # noqa: PLC0415
        return _pp.WORKTREES_DIR
    except Exception:
        d = PIPELINE_DIR / "worktrees"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _scan_log_for_transient_error(log_path: Path) -> bool:
    """Return True if the log tail contains any transient-error pattern."""
    try:
        with open(log_path, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - 4096))  # read last 4 KB
            tail = f.read()
        return any(p in tail for p in TRANSIENT_PATTERNS)
    except Exception:
        return False


# ── Path computation ──────────────────────────────────────────────────────────

def compute_result_path(state: "TicketState", stage: str) -> Path:
    """Deterministic path — (ticket, stage, attempt) → file.

    Same state values always map to the same file.  Attempt counter
    disambiguates repeated stages (retry loops get distinct paths).
    """
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    attempt  = _attempt_for_stage(state, stage)
    slug     = stage.lower().replace(" ", "_")
    return RESULTS_DIR / str(ticket) / f"{slug}_{attempt}.json"


def _attempt_for_stage(state: "TicketState", stage: str) -> int:
    """Return the current attempt index for stages that can repeat."""
    impl   = state.get("impl")   or {}
    ship   = state.get("ship")   or {}
    review = state.get("review") or {}
    slug   = stage.lower().replace(" ", "_")
    return {
        "ai_implementation":  impl.get("self_review_retry_count", 0),
        "self_review":        impl.get("self_review_retry_count", 0),
        "fix_ci":             ship.get("ci_fix_count", 0),
        "respond_to_review":  review.get("review_comment_round", 0),
    }.get(slug, 0)


# ── Result-file helpers ───────────────────────────────────────────────────────

def _try_read_complete(result_path: Path) -> dict | None:
    """Return parsed JSON dict if the file exists and contains a valid 'outcome'. Else None."""
    try:
        text = result_path.read_text(encoding="utf-8")
        raw  = json.loads(text)
        if "outcome" in raw:
            return raw
    except Exception:
        pass
    return None


async def _wait_and_parse(result_path: Path, timeout: float, stage: str) -> StageResult:
    """Poll result_path until it appears and is complete, or timeout elapses."""
    started = time.time()
    while True:
        if result_path.exists():
            raw = _try_read_complete(result_path)
            if raw is not None:
                try:
                    result = StageResult.model_validate(raw)
                except Exception as e:
                    result = StageResult(
                        outcome="error",
                        error=f"Result file parse error: {e}",
                        record=RunRecord(
                            stage=stage, started_at=started,
                            finished_at=time.time(), outcome="error",
                            result_path=str(result_path),
                        ),
                    )
                # Backfill record if skill didn't write one
                if not result.record.result_path:
                    result.record.result_path = str(result_path)
                if not result.record.stage:
                    result.record.stage = stage
                result.record.finished_at = time.time()
                return result

        elapsed = time.time() - started
        if elapsed >= timeout:
            return StageResult(
                outcome="timeout",
                error=f"Stage '{stage}' timed out after {int(timeout)}s waiting for {result_path}",
                record=RunRecord(
                    stage=stage, started_at=started,
                    finished_at=time.time(), outcome="timeout",
                    result_path=str(result_path),
                ),
            )
        await asyncio.sleep(RESULT_POLL_INTERVAL)


# ── Launchers ─────────────────────────────────────────────────────────────────

def _launch_visible_terminal(
    state: "TicketState",
    stage: str,
    result_path: Path,
    context: dict,
) -> str:
    """Open a macOS Terminal window running the skill.  Returns a run_id string."""
    identity      = state.get("identity") or {}
    ticket        = identity.get("ticket_number", 0)
    worktree_path = context.get("worktree_path", "")
    command       = context["command"]
    allowed_tools = context.get("tools", "Bash,Read,Grep,Glob,Edit,Write,Agent")
    extra_args    = context.get("extra_args", "").strip()
    claude        = _claude_bin()
    run_id        = f"{stage.replace(' ', '_')}-{ticket}-{uuid.uuid4().hex[:8]}-terminal"
    win_title     = f"Claude #{ticket} — {stage}"

    result_path.parent.mkdir(parents=True, exist_ok=True)

    full_cmd = f"{command} --ticket {ticket}"
    if extra_args:
        full_cmd = f"{command} {extra_args} --ticket {ticket}"

    script_lines = ["#!/bin/zsh"]
    script_lines.append(f'export PIPELINE_RESULT_PATH="{result_path}"')
    if worktree_path:
        script_lines.append(f'cd "{worktree_path}"')
    script_lines += [
        f"echo '=== Claude #{ticket} — {stage} ==='",
        "echo ''",
        f"{claude} -p '{full_cmd}' --allowedTools '{allowed_tools}'",
        "echo ''",
        "echo '=== done (press any key to close) ==='",
        "read -k1",
    ]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, prefix="claude_pipe_"
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
        raise RuntimeError(
            f"osascript failed for {stage}: {result.stderr.strip() or result.stdout.strip()}"
        )
    print(f"[{time.strftime('%H:%M:%S')}] terminal spawned for #{ticket} → {stage}", flush=True)
    return run_id


async def _launch_headless(
    state: "TicketState",
    stage: str,
    result_path: Path,
    context: dict,
) -> str:
    """Launch a headless Claude process (fire-and-forget). Returns run_id."""
    identity      = state.get("identity") or {}
    ticket        = identity.get("ticket_number", 0)
    command       = context["command"]
    allowed_tools = context.get("tools", "Bash,Read,Grep,Glob,Agent")
    extra_args    = context.get("extra_args", "").strip()
    claude        = _claude_bin()
    run_id        = f"{stage.replace(' ', '_')}-{ticket}-{uuid.uuid4().hex[:8]}"
    log_path      = _log_dir() / f"{run_id}.log"

    result_path.parent.mkdir(parents=True, exist_ok=True)

    full_cmd = f"{command} --ticket {ticket}"
    if extra_args:
        full_cmd = f"{command} {extra_args} --ticket {ticket}"

    env = os.environ.copy()
    env["PIPELINE_RESULT_PATH"] = str(result_path)

    cmd = [claude, "-p", full_cmd, "--allowedTools", allowed_tools]

    logf = open(log_path, "wb")  # noqa: WPS515 — kept open by background task
    logf.write(
        f"=== run_id={run_id} ticket={ticket} stage='{stage}' "
        f"mode=headless started={time.strftime('%Y-%m-%dT%H:%M:%S')} "
        f"result_path={result_path} ===\n".encode()
    )
    logf.flush()

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=logf, stderr=asyncio.subprocess.STDOUT, env=env,
    )

    async def _watch() -> None:
        try:
            rc = await proc.wait()
        except Exception:
            rc = -1
        finally:
            try:
                logf.write(f"\n=== exited rc={rc} ===\n".encode())
                logf.close()
            except Exception:
                pass

    asyncio.create_task(_watch())
    print(f"[{time.strftime('%H:%M:%S')}] headless spawned for #{ticket} → {stage} [{log_path.name}]", flush=True)
    return run_id


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_stage(
    state: "TicketState",
    stage: str,
    context: dict,
) -> StageResult:
    """Idempotent stage runner.

    context keys:
        command     (str)  — slash-command to run, e.g. "/plan-github-tickets"
        tools       (str)  — comma-separated allowed tools
        spawn_mode  (str)  — "terminal" | "headless"
        extra_args  (str)  — optional extra args prepended to --ticket
        worktree_path (str) — cd target for terminal spawns

    Re-entry scenarios:
        result file complete   → return without re-launching
        result file incomplete → await (may time out → crash sentinel)
        file absent            → launch + await
    """
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    timeout  = STALE_THRESHOLD.get(stage, _DEFAULT_TIMEOUT)

    result_path = compute_result_path(state, stage)

    if result_path.exists():
        raw = _try_read_complete(result_path)
        if raw is not None:
            # Already complete — return without re-launching (idempotency guard)
            print(
                f"[{time.strftime('%H:%M:%S')}] #{ticket}: {stage} result already exists → skip launch",
                flush=True,
            )
            try:
                result = StageResult.model_validate(raw)
            except Exception as e:
                result = StageResult(
                    outcome="error",
                    error=f"Cached result parse error: {e}",
                    record=RunRecord(stage=stage, result_path=str(result_path), outcome="error"),
                )
            return result
        # File exists but incomplete (crash mid-write) — await without re-launching
        print(
            f"[{time.strftime('%H:%M:%S')}] #{ticket}: {stage} result incomplete → awaiting",
            flush=True,
        )
        return await _wait_and_parse(result_path, timeout, stage)

    # Fresh entry: launch then await (with transient-error retry)
    spawn_mode = context.get("spawn_mode", "headless")
    started_at = time.time()
    last_log_path: Path | None = None
    last_result: StageResult | None = None

    for attempt in range(MAX_SUBPROCESS_RETRIES):
        if spawn_mode == "terminal":
            # Terminal spawns are visible to the user; don't retry silently
            _launch_visible_terminal(state, stage, result_path, context)
            last_result = await _wait_and_parse(result_path, timeout, stage)
            break
        else:
            run_id = await _launch_headless(state, stage, result_path, context)
            last_log_path = _log_dir() / f"{run_id}.log"
            last_result = await _wait_and_parse(result_path, timeout, stage)

        # On success or non-transient failure, stop retrying
        if last_result.outcome == "done":
            break
        is_transient = (
            last_result.outcome in ("timeout", "crash")
            and last_log_path is not None
            and _scan_log_for_transient_error(last_log_path)
        )
        if not is_transient:
            break

        if attempt < MAX_SUBPROCESS_RETRIES - 1:
            wait = SUBPROCESS_RETRY_BACKOFF[attempt]
            print(
                f"[{time.strftime('%H:%M:%S')}] #{ticket}: {stage} transient error "
                f"(attempt {attempt + 1}/{MAX_SUBPROCESS_RETRIES}) — retrying in {wait}s",
                flush=True,
            )
            await asyncio.sleep(wait)
            # Remove partial result file before re-launching so idempotency guard doesn't fire
            try:
                result_path.unlink(missing_ok=True)
            except Exception:
                pass

    result = last_result or StageResult(
        outcome="error",
        error=f"Stage '{stage}' failed to produce a result after {MAX_SUBPROCESS_RETRIES} attempts",
        record=RunRecord(stage=stage, result_path=str(result_path), outcome="error"),
    )

    # Ensure the record has started_at from before the launch
    result.record.started_at = started_at
    if not result.record.stage:
        result.record.stage = stage

    return result
