"""node_recover — graph entry point for ALL pipeline restarts.

Two scenarios route here:
  1. Error recovery (dead-END): operator moved a ticket from Error back to
     Ready To Pick Up.  `identity.is_recovery = True` and (optionally)
     `identity.recovery_target` indicate the failed stage.
  2. Poller restart: the poller crashed or was restarted while a stage was
     executing.  The poller clears the checkpoint, sets `is_recovery = True`,
     and re-enters here so all recovery logic is centralised.

For normal (non-recovery) runs, this node is a transparent pass-through to
`route_entry`.

Recovery target is resolved in priority order:
  1. `identity.recovery_target` if set (explicit, e.g. from dead-END path).
  2. Result files on disk — find the first incomplete or missing stage.
  3. `run_history` scan (belt-and-suspenders fallback).
  4. Fall through to `route_entry` if no target can be determined.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import httpx
from langgraph.types import Command

from graph import events
from graph.nodes._base import _log, move_status, resolve_worktree
from graph.state import TicketState


# Nodes that need a clean worktree to operate correctly.
_WORKTREE_NODES = frozenset({"implement", "quality", "self_review", "ship"})

# Stage name (as written into RunRecord.stage) → graph node name.
_STAGE_TO_NODE: dict[str, str] = {
    "AI Planning":        "plan",
    "AI Implementation":  "implement",
    "AI Quality Check":   "quality",
    "Self Review":        "self_review",
    "Ready To Ship - AI": "ship",
    "Fix CI":             "fix_ci",
    "Respond To Review":  "respond",
}

# Reverse mapping — node name → stage name (for result-file lookup).
_NODE_TO_STAGE: dict[str, str] = {v: k for k, v in _STAGE_TO_NODE.items()}

# Board status to display while a stage is (re)running.
_NODE_TO_BOARD_STATUS: dict[str, str] = {
    "plan":        "AI Planning",
    "implement":   "AI Implementation",
    "quality":     "AI Implementation",
    "self_review": "AI Implementation",
    "ship":        "Ready To Ship - AI",
    "monitor_pr":  "In PR",
    "fix_ci":      "AI-PR Assistance",
    "respond":     "AI-PR Assistance",
}

# Ordered pipeline stages for disk-based target inference.
# Gate nodes (gate_plan_approval, gate_impl_approval) are intentionally omitted;
# routing directly to their successor skips the stale interrupt.
_PIPELINE_ORDER: list[tuple[str, str]] = [
    ("AI Planning",        "plan"),
    ("AI Implementation",  "implement"),
    ("AI Quality Check",   "quality"),
    ("Self Review",        "self_review"),
    ("Ready To Ship - AI", "ship"),
]

# Post-ship stages that can run in any order / repeat.
_POST_SHIP_STAGES: list[tuple[str, str]] = [
    ("Fix CI",            "fix_ci"),
    ("Respond To Review", "respond"),
]

_RECOVER_TARGETS = Literal[
    "route_entry",
    "plan", "implement", "quality", "self_review",
    "ship", "monitor_pr", "fix_ci", "respond",
]


# ── Disk-based inference helpers ──────────────────────────────────────────────

def _best_result_outcome(ticket_dir: Path, stage_name: str) -> str | None:
    """Return the outcome of the highest-attempt result file for a stage, or None."""
    slug = stage_name.lower().replace(" ", "_")
    best: str | None = None
    for attempt in range(10):
        path = ticket_dir / f"{slug}_{attempt}.json"
        if not path.exists():
            break
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if "outcome" in raw:
                best = raw["outcome"]
        except Exception:
            pass
    return best


def _infer_target_from_results(ticket: int, is_spike: bool = False) -> str | None:
    """Scan result files on disk to determine the next stage to execute.

    Returns a node name, or None to fall through to run_history / route_entry.

    Algorithm:
      - Walk main pipeline stages in order; return the first that is not "done".
      - If all main stages are done, check post-ship stages for failures.
      - If ship is done and no post-ship failures, return "monitor_pr" so the
        graph re-enters the CI-polling gate rather than replaying the whole pipeline.
    """
    try:
        from graph.runner import RESULTS_DIR  # noqa: PLC0415
    except ImportError:
        return None

    ticket_dir = RESULTS_DIR / str(ticket)
    if not ticket_dir.exists():
        return None

    stages = [s for s in _PIPELINE_ORDER if not (is_spike and s[1] == "plan")]

    # First non-done (missing, failed, crashed, timed-out) stage is the resume point.
    for stage_name, node_name in stages:
        outcome = _best_result_outcome(ticket_dir, stage_name)
        if outcome != "done":
            return node_name

    # All main stages done — look for a post-ship failure.
    for stage_name, node_name in _POST_SHIP_STAGES:
        outcome = _best_result_outcome(ticket_dir, stage_name)
        if outcome is not None and outcome != "done":
            return node_name

    # Ship is done and no post-ship stage has a failure.
    # We're most likely parked in monitor_pr waiting for CI — resume there.
    if _best_result_outcome(ticket_dir, "Ready To Ship - AI") == "done":
        return "monitor_pr"

    return None


def _cleanup_orphaned_result(ticket: int, stage_name: str) -> None:
    """Delete result files for a stage that have no valid 'outcome' key.

    These are partial writes left by a crashed subprocess.  Removing them lets
    run_stage() do a fresh launch instead of stalling for the full timeout.
    """
    try:
        from graph.runner import RESULTS_DIR  # noqa: PLC0415
        ticket_dir = RESULTS_DIR / str(ticket)
        if not ticket_dir.exists():
            return
        slug = stage_name.lower().replace(" ", "_")
        for attempt in range(10):
            path = ticket_dir / f"{slug}_{attempt}.json"
            if not path.exists():
                break
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if "outcome" not in raw:
                    path.unlink()
                    _log(f"  #{ticket}: recover — removed orphaned result file {path.name}")
            except Exception:
                path.unlink()
                _log(f"  #{ticket}: recover — removed unparseable result file {path.name}")
    except Exception as e:
        _log(f"  #{ticket}: recover — cleanup_orphaned_result failed: {e}")


# ── Main node ─────────────────────────────────────────────────────────────────

async def node_recover(state: TicketState, store=None) -> Command[_RECOVER_TARGETS]:
    identity = state.get("identity") or {}
    ticket   = identity.get("ticket_number", 0)
    is_spike = identity.get("is_spike", False)

    # Fast path for fresh runs: 99% of invocations hit this branch.
    if not identity.get("is_recovery"):
        return Command(goto="route_entry")

    # Resolve target: explicit → disk inference → AI → history scan → fall through.
    repo_full = (identity.get("repo_full") or "").strip()
    target = (
        identity.get("recovery_target")
        or _infer_target_from_results(ticket, is_spike)
        or (await _infer_target_via_ai(ticket, repo_full, is_spike, state) if repo_full else None)
        or _scan_history_for_target(state)
    )

    if not target:
        _log(f"  #{ticket}: recover — no resume target found, falling through to route_entry")
        return Command(
            goto="route_entry",
            update={"identity": {"is_recovery": False, "recovery_target": ""}},
        )

    if target not in _NODE_TO_BOARD_STATUS:
        _log(f"  #{ticket}: recover — unknown target '{target}', falling through to route_entry")
        return Command(
            goto="route_entry",
            update={"identity": {"is_recovery": False, "recovery_target": ""}},
        )

    _log(f"  #{ticket}: recover → resuming at '{target}'")
    recent_errors = (state.get("errors") or [])[-3:]
    events.emit(ticket, "Recovery", "stage_started",
                {"target": target, "errors": recent_errors})

    board_status = _NODE_TO_BOARD_STATUS[target]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await move_status(client, identity["item_id"], board_status)
    except Exception as e:
        _log(f"  #{ticket}: recover — board move failed: {e} (continuing)")

    # Clean up any incomplete result files for the target stage so run_stage()
    # does a fresh launch rather than stalling on a partial write from a crash.
    stage_name = _NODE_TO_STAGE.get(target, "")
    if stage_name:
        _cleanup_orphaned_result(ticket, stage_name)

    # For nodes that use a worktree, rebuild it from a clean state so a crashed
    # subprocess cannot poison the retry with stale/contaminated files.
    # monitor_pr is a gate node — it never writes files, no worktree needed.
    #
    # Stages that run after implementation (quality, self_review, ship) require the
    # remote branch to exist.  If it doesn't, the implementation push failed — falling
    # back to master would produce a ghost-success.  Escalate to needs_human instead.
    _POST_IMPL_NODES = frozenset({"quality", "self_review", "ship"})
    worktree_update: dict = {}
    if target in _WORKTREE_NODES:
        try:
            needs_remote = target in _POST_IMPL_NODES
            worktree_path, repo_local = await resolve_worktree(state, store, require_remote_branch=needs_remote)
            _log(f"  #{ticket}: recover — rebuilt worktree at {worktree_path}")
            worktree_update = {
                "impl":     {"worktree_path": worktree_path},
                "identity": {"repo_local_path": repo_local},
            }
        except Exception as e:
            _log(f"  #{ticket}: recover — worktree rebuild failed: {e}")
            if target in _POST_IMPL_NODES:
                # Branch is missing at remote — implementation push failed.
                # Escalate to needs_human so the human can reset to AI Implementation.
                events.emit(ticket, "Recovery", "stage_completed", {"resumed_at": "needs_human"})
                return Command(
                    goto="needs_human",
                    update={
                        "identity": {"is_recovery": False, "recovery_target": ""},
                        "errors":   [f"--- recovery: worktree rebuild failed for '{target}' ---", str(e)],
                    },
                )
            _log(f"  #{ticket}: recover — continuing without worktree rebuild")

    events.emit(ticket, "Recovery", "stage_completed", {"resumed_at": target})

    # Reset retry counters for the target stage so operator-triggered recovery
    # gets a fresh budget rather than immediately escalating on the first retry.
    # Without this, a ticket that hit max_self_review_retries and was manually
    # sent back to 'Ready To Pick Up' would escalate on attempt 3 → max=2.
    retry_reset: dict = {}
    if target in {"implement", "self_review"}:
        retry_reset["impl"] = {"self_review_retry_count": 0}
    if target == "fix_ci":
        retry_reset["ship"] = {"ci_fix_count": 0}
    if target == "respond":
        retry_reset["impl"] = {"review_response_round": 0}

    # Merge impl-dicts if both worktree_update and retry_reset touch it.
    merged_update: dict = {**worktree_update}
    for key, value in retry_reset.items():
        if key in merged_update and isinstance(merged_update[key], dict) and isinstance(value, dict):
            merged_update[key] = {**merged_update[key], **value}
        else:
            merged_update[key] = value

    return Command(
        goto=target,
        update={
            "identity": {"is_recovery": False, "recovery_target": ""},
            "errors":   [f"--- recovery: resumed at '{target}' ---"],
            "control":  {"current_stage": target},
            **merged_update,
        },
    )


def _scan_history_for_target(state: TicketState) -> str | None:
    """Fallback: derive resume node from run_history when disk inference returns None."""
    history = state.get("run_history") or []
    for entry in reversed(history):
        outcome = entry.get("outcome")
        if outcome and outcome != "done":
            return _STAGE_TO_NODE.get(entry.get("stage", ""))
    return None


async def _infer_target_via_ai(ticket: int, repo_full: str, is_spike: bool, state: TicketState) -> str | None:
    """Use Claude to infer the recovery target from issue comment markers and disk state.

    Called only when disk inference and history scan both return None — i.e. the result
    files and run_history don't have enough signal (e.g. a DB reset wiped run_history and
    all the disk result files show 'done' through an intermediate stage, but a later stage
    completed only as a GitHub comment marker with no result file on disk).

    Returns a valid node name from _NODE_TO_BOARD_STATUS, or None on any failure.
    """
    import asyncio as _asyncio
    import subprocess as _subprocess

    # ── Gather evidence: issue comment markers ────────────────────────────────
    markers: list[dict] = []
    try:
        result = _subprocess.run(
            ["gh", "issue", "view", str(ticket), "--repo", repo_full,
             "--json", "comments"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            for comment in json.loads(result.stdout).get("comments", []):
                body = comment.get("body", "")
                created_at = comment.get("createdAt", "")
                for line in body.splitlines():
                    line = line.strip()
                    if line.startswith("<!-- ai-") and line.endswith("-->"):
                        markers.append({"marker": line, "timestamp": created_at})
    except Exception as exc:
        _log(f"  #{ticket}: recover — AI inference: comment fetch failed: {exc}")

    # ── Gather evidence: disk result files ────────────────────────────────────
    disk_state: list[dict] = []
    try:
        from graph.runner import RESULTS_DIR  # noqa: PLC0415
        ticket_dir = RESULTS_DIR / str(ticket)
        all_stages = _PIPELINE_ORDER + _POST_SHIP_STAGES
        for stage_name, node_name in all_stages:
            outcome = _best_result_outcome(ticket_dir, stage_name)
            disk_state.append({"stage": stage_name, "node": node_name, "outcome": outcome})
    except Exception as exc:
        _log(f"  #{ticket}: recover — AI inference: disk state read failed: {exc}")

    # ── Build prompt ──────────────────────────────────────────────────────────
    pipeline_sequence = " → ".join(n for _, n in _PIPELINE_ORDER)
    valid_targets = list(_NODE_TO_BOARD_STATUS.keys())

    prompt = f"""You are determining the recovery point for a stalled CI pipeline ticket.

PIPELINE STAGE ORDER (in sequence):
{pipeline_sequence}
Post-ship stages (can repeat): fix_ci, respond

VALID RETURN VALUES (node names only): {valid_targets}

EVIDENCE — Issue comment markers (chronological, oldest first):
{json.dumps(markers, indent=2)}

EVIDENCE — Result files on disk (outcome=null means no file exists):
{json.dumps(disk_state, indent=2)}

RULES:
- A stage is "successfully completed" when its most recent marker ends with ":done".
- A stage should re-run if its most recent marker ends with ":error" or ":needs-human".
- If a later stage failed after an earlier one succeeded, resume at the failed stage.
- If implementation was retried AFTER quality/self-review passed, quality must re-run.
- Return "plan" only if planning never completed successfully.
- When uncertain, prefer resuming at an earlier stage over skipping ahead.

Respond with ONLY valid JSON — no markdown, no explanation outside the JSON:
{{"target": "<node_name>", "confidence": "high|medium|low", "reason": "<one concise sentence>"}}"""

    # ── Invoke claude -p ──────────────────────────────────────────────────────
    try:
        from graph.runner import _claude_bin  # noqa: PLC0415
        claude = _claude_bin()
        proc = await _asyncio.create_subprocess_exec(
            claude, "-p", prompt, "--output-format", "json",
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await _asyncio.wait_for(proc.communicate(), timeout=45)
        except _asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            _log(f"  #{ticket}: recover — AI inference timed out")
            return None

        raw = stdout.decode("utf-8", errors="replace").strip()

        # claude --output-format json wraps output: {"result": "<text>", ...}
        outer = json.loads(raw)
        inner_text = outer.get("result", raw)
        parsed = json.loads(inner_text) if isinstance(inner_text, str) else inner_text

        target     = (parsed.get("target") or "").strip()
        reason     = parsed.get("reason", "")
        confidence = parsed.get("confidence", "?")

        if target in _NODE_TO_BOARD_STATUS:
            _log(f"  #{ticket}: recover — AI inference [{confidence}]: target='{target}' — {reason}")
            return target

        _log(f"  #{ticket}: recover — AI inference returned unknown target '{target}' — skipping")
        return None

    except Exception as exc:
        _log(f"  #{ticket}: recover — AI inference failed: {exc}")
        return None
