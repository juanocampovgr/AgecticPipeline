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
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from graph import events
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
    "Ready To Ship - AI":  int(os.environ.get("STALE_SHIP_SECONDS",     "120")),
    "Self Review":         int(os.environ.get("STALE_SELF_REVIEW_SECONDS", "1800")),
    "Fix CI":              int(os.environ.get("STALE_FIX_CI_SECONDS",   "3600")),
    "Respond To Review":   int(os.environ.get("STALE_RESPOND_SECONDS",  "3600")),
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


_OFFSCRIPT_LOG_PATTERNS: dict[str, tuple[bytes, ...]] = {
    "Ready To Ship - AI": (
        b"./gradlew",
        b"BUILD SUCCESSFUL",
        b"Waiting for unit tests",
        b"Lint \xe2\x9c\x85",  # "Lint ✅"
    ),
}


def _scan_log_for_offscript(log_path: Path, stage: str) -> bytes | None:
    """Return the first forbidden pattern found in the log, or None."""
    patterns = _OFFSCRIPT_LOG_PATTERNS.get(stage)
    if not patterns:
        return None
    try:
        return next((p for p in patterns if p in log_path.read_bytes()), None)
    except Exception:
        return None


# Stage-specific log patterns that confirm successful completion.
# Used to auto-recover when a skill exits rc=0 without writing its result file.
# Each stage maps to a tuple of accepted markers (a stage may run more than one
# skill — e.g. AI Planning runs /plan-github-tickets for normal tickets and
# /spike-tickets for spikes).
_SUCCESS_LOG_PATTERNS: dict[str, tuple[bytes, ...]] = {
    "AI Quality Check":    (b"quality-check Complete",),
    "AI Implementation":   (b"code-tickets Complete", b"background commit task completed successfully", b"Implementation complete \xe2\x80\x94 branch pushed"),
    "Self Review":         (b"Self-review passed",),
    "Ready To Ship - AI":  (b"Ship complete",),
    "Fix CI":              (b"fix-ci-failure Complete",),
    "Respond To Review":   (b"respond-to-review Complete",),
    "AI Planning":         (b"plan-github-tickets Complete", b"spike-tickets Complete"),
}


def _try_recover_result_from_log(
    log_path: Path,
    result_path: Path,
    stage: str,
) -> bool:
    """If the log signals successful completion but no result file was written,
    write a minimal 'done' result so the pipeline can advance.

    Returns True if recovery succeeded (result file now exists and is valid).
    """
    patterns = _SUCCESS_LOG_PATTERNS.get(stage)
    if not patterns:
        return False
    try:
        content = log_path.read_bytes()
        if not any(p in content for p in patterns):
            return False
        # Log shows successful completion — write a minimal done result.
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({"outcome": "done", "auto_recovered": True,
                        "note": f"result file written by runner after {stage} log showed success"}),
            encoding="utf-8",
        )
        print(
            f"[{time.strftime('%H:%M:%S')}] {stage}: log shows success but no result file — "
            f"auto-wrote done result",
            flush=True,
        )
        return True
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] {stage}: log-recovery failed: {e}", flush=True)
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


_PROC_EXIT_GRACE = 30  # seconds to wait after subprocess exits before declaring crash


async def _wait_and_parse(
    result_path: Path,
    timeout: float,
    stage: str,
    proc: "asyncio.subprocess.Process | None" = None,
    log_path: "Path | None" = None,
) -> StageResult:
    """Poll result_path until it appears and is complete, or timeout elapses.

    If proc is provided and exits without writing the result file, wait a short
    grace window then return outcome='crash' instead of polling for the full timeout.
    """
    started = time.time()
    proc_exited_at: float | None = None

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

        # Detect if the skill ran a forbidden command (e.g. gradlew inside ship).
        # Surface this as an attributable error immediately rather than timing out.
        if log_path:
            offscript = _scan_log_for_offscript(log_path, stage)
            if offscript:
                err_msg = f"ship ran forbidden command: {offscript.decode(errors='replace')}"
                try:
                    result_path.parent.mkdir(parents=True, exist_ok=True)
                    result_path.write_text(
                        json.dumps({"outcome": "error", "error": err_msg}),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
                return StageResult(
                    outcome="error",
                    error=err_msg,
                    record=RunRecord(
                        stage=stage, started_at=started,
                        finished_at=time.time(), outcome="error",
                        result_path=str(result_path),
                    ),
                )

        # If the subprocess already exited but hasn't written the result file yet,
        # start a grace countdown so we don't stall for the full timeout.
        if proc is not None and proc.returncode is not None:
            if proc_exited_at is None:
                proc_exited_at = time.time()
                print(
                    f"[{time.strftime('%H:%M:%S')}] {stage}: subprocess exited "
                    f"rc={proc.returncode} — waiting {_PROC_EXIT_GRACE}s grace for result file",
                    flush=True,
                )
            elif time.time() - proc_exited_at >= _PROC_EXIT_GRACE:
                # Before declaring crash, attempt log-based recovery for stages
                # where the skill logs a success marker but skips the file write.
                if log_path and _try_recover_result_from_log(log_path, result_path, stage):
                    # Recovery wrote the file — loop will pick it up next tick
                    proc_exited_at = None  # reset so we don't re-trigger
                    continue
                return StageResult(
                    outcome="crash",
                    error=(
                        f"Stage '{stage}' subprocess exited rc={proc.returncode} "
                        f"without writing result file after {_PROC_EXIT_GRACE}s grace"
                    ),
                    record=RunRecord(
                        stage=stage, started_at=started,
                        finished_at=time.time(), outcome="crash",
                        result_path=str(result_path),
                    ),
                )

        await asyncio.sleep(RESULT_POLL_INTERVAL)


# ── Launcher ──────────────────────────────────────────────────────────────────

async def _launch_headless(
    state: "TicketState",
    stage: str,
    result_path: Path,
    context: dict,
) -> tuple[str, Path, "asyncio.subprocess.Process"]:
    """Launch a headless Claude process (fire-and-forget). Returns (run_id, log_path, proc)."""
    identity      = state.get("identity") or {}
    ticket        = identity.get("ticket_number", 0)
    command       = context["command"]
    allowed_tools = context.get("tools", "Bash,Read,Grep,Glob,Agent")
    extra_args    = context.get("extra_args", "").strip()
    worktree_path = context.get("worktree_path", "")
    claude        = _claude_bin()
    run_id        = f"{stage.replace(' ', '_')}-{ticket}-{uuid.uuid4().hex[:8]}"
    log_path      = _log_dir() / f"{run_id}.log"

    result_path.parent.mkdir(parents=True, exist_ok=True)

    full_cmd = f"{command} --ticket {ticket}"
    if extra_args:
        full_cmd = f"{command} {extra_args} --ticket {ticket}"

    env = os.environ.copy()
    env["PIPELINE_RESULT_PATH"] = str(result_path)

    model = context.get("model", "").strip()
    # bypassPermissions: subprocesses run non-interactively; any `ask`-rule prompt
    # (e.g. git push) would silently hang or fail. All pipeline stages already run
    # in isolated worktrees, so permission bypass here is safe.
    cmd = [
        claude, "-p", full_cmd,
        "--allowedTools", allowed_tools,
        "--permission-mode", "bypassPermissions",
    ]
    if model:
        cmd += ["--model", model]

    logf = open(log_path, "wb")  # noqa: WPS515 — kept open by background task
    logf.write(f"=== cmd={' '.join(cmd)} ===\n".encode())
    logf.write(
        f"=== run_id={run_id} ticket={ticket} stage='{stage}' "
        f"model={model or '(cli-default)'} "
        f"started={time.strftime('%Y-%m-%dT%H:%M:%S')} "
        f"cwd={worktree_path or os.getcwd()} "
        f"result_path={result_path} ===\n".encode()
    )
    logf.flush()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=logf,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        cwd=worktree_path or None,
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
    print(f"[{time.strftime('%H:%M:%S')}] launched #{ticket} → {stage} [{log_path.name}]", flush=True)
    return run_id, log_path, proc


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_stage(
    state: "TicketState",
    stage: str,
    context: dict,
) -> StageResult:
    """Idempotent stage runner — every stage runs headless.

    context keys:
        command       (str) — slash-command to run, e.g. "/plan-github-tickets"
        tools         (str) — comma-separated allowed tools
        extra_args    (str) — optional extra args prepended to --ticket
        worktree_path (str) — cd target for the subprocess (optional)

    `spawn_mode` is accepted for backwards compatibility but ignored; all
    stages now run headless and progress is surfaced via the per-ticket
    dashboard TUI (see graph/events.py + pipeline/dashboard.py).

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
            # Emit events so the dashboard overwrites any stale failed/crash state
            # from prior runs that are still in the append-only events file.
            _emit_stage_finish(ticket, stage, result, log_path=None)
            return result
        # File exists but incomplete (crash mid-write) — await without re-launching
        print(
            f"[{time.strftime('%H:%M:%S')}] #{ticket}: {stage} result incomplete → awaiting",
            flush=True,
        )
        events.emit(ticket, stage, "stage_started", {
            "result_path": str(result_path),
            "attempt": 0,
            "resumed": True,
        })
        last_result = await _wait_and_parse(result_path, timeout, stage)
        _emit_stage_finish(ticket, stage, last_result, log_path=None)
        return last_result

    # Fresh entry: launch then await (with transient-error retry)
    started_at = time.time()
    last_log_path: Path | None = None
    last_result: StageResult | None = None

    for attempt in range(MAX_SUBPROCESS_RETRIES):
        run_id, last_log_path, last_proc = await _launch_headless(state, stage, result_path, context)
        events.emit(ticket, stage, "stage_started", {
            "result_path": str(result_path),
            "log_path":    str(last_log_path),
            "run_id":      run_id,
            "attempt":     attempt,
        })
        last_result = await _wait_and_parse(result_path, timeout, stage, proc=last_proc, log_path=last_log_path)

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
            events.emit(ticket, stage, "stage_retry", {
                "attempt": attempt + 1,
                "max":     MAX_SUBPROCESS_RETRIES,
                "reason":  "transient error",
                "wait_s":  wait,
            })
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

    _emit_stage_finish(ticket, stage, result, log_path=last_log_path)
    return result


def _emit_stage_finish(
    ticket: int,
    stage: str,
    result: StageResult,
    log_path: Path | None,
) -> None:
    payload: dict = {
        "outcome": result.outcome,
        "log_path": str(log_path) if log_path else None,
    }
    if result.outcome == "done":
        if result.branch:
            payload["branch"] = result.branch
        if result.pr_url:
            payload["pr_url"] = result.pr_url
        if result.pr_number:
            payload["pr_number"] = result.pr_number
        events.emit(ticket, stage, "stage_completed", payload)
    else:
        payload["error"] = (result.error or "")[:500]
        events.emit(ticket, stage, "stage_failed", payload)
