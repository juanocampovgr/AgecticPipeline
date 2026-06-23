# Pipeline Test Report — Ticket #24
**Date**: 2026-06-23  
**Ticket**: [#24 — Migrate all deep links to use InApp Deep link when app is open](https://github.com/juanocampovgr/AgecticPipeline/issues/24)  
**Jira**: ANDROID-18640  
**Target repo**: `grindrllc/grindr-android`  
**Branch**: `juanocampovgr/ANDROID-18640-migrate-all-deep-links-to-use-inapp-deep-link`  
**Test type**: Full end-to-end human-in-the-loop observation run  
**Final board status**: Ready To Ship - AI (stuck — no PR created)

---

## Executive Summary

Ticket #24 was selected as the test vehicle for the post-LangGraph-refactor pipeline. The run exposed **5 bugs that required manual intervention** and **2 behavioral defects** that did not block progress but represent correctness issues. The implementation quality was excellent (41 deep-link call sites correctly migrated, all CI checks passed), but the pipeline could not complete shipping autonomously.

**Root cause of most failures**: the pipeline poller (PID 57525) was started on Jun 22 at 16:39 and has never been restarted after the LangGraph refactor. It is running old in-memory code that diverges significantly from the on-disk version. The new architecture (`graph/runner.py`, `graph/nodes/`, `PIPELINE_RESULT_PATH` result files) has **never been exercised in production**.

---

## Execution Timeline

| Time (UTC) | Event | Manual? |
|---|---|---|
| 09:16 | First planning attempt dispatched | — |
| 09:24 | Pipeline auto-escalated to Error (API 529 overload, 15 min timeout) | — |
| ~09:28 | Deleted checkpoints, moved board back to "Ready To Pick Up" | ✋ |
| 09:52 | Second planning attempt started | — |
| 09:58 | Planning agent completed (rc=0), plan posted to GitHub | — |
| ~09:58 | `ai_planning_0.json` result file written manually (PIPELINE_RESULT_PATH bug) | ✋ |
| ~10:02 | `plan-approved` label added to unblock pipeline | ✋ |
| ~10:10 | Pipeline advanced to AI Implementation | — |
| ~10:20 | Implementation agent started in worktree | — |
| ~10:42 | Self Review agent started | — |
| ~10:50 | Self Review completed (rc=0) | — |
| 14:58 | Implementation plan comment timestamp on GitHub | — |
| 15:20 | Implementation complete — branch pushed (commit `28b30900437`) | — |
| ~15:30 | Quality check dispatched (detekt, lint, unit tests) | — |
| ~15:35 | All 3 CI checks passed | — |
| 15:41 | `<!-- ai-quality:done -->` comment posted manually (quality agent failed) | ✋ |
| 15:46 | Self-review passed comment on GitHub | — |
| 15:47 | `impl-approved` label added | ✋ |
| 15:48 | GitHub issue auto-closed (COMPLETED) — unexpected behavior | — |
| ~11:09 | Ship agent ran, exited silently: no PR created, no marker posted | — |
| Now | Pipeline stuck at `waiting_ship_marker`, no PR exists | ✋ |

---

## Bugs Found

### Bug 1: API 529 Overload → Immediate Error Escalation, No Retry
**Severity**: High  
**Stage affected**: AI Planning (first attempt)

When the Anthropic API returned a 529 "Overloaded" error during the planning subprocess, the pipeline did not retry. Instead:
1. The subprocess exited with an error
2. The poller found no result file after its 15-minute `STALE_THRESHOLD`
3. The pipeline posted an auto-escalation comment and moved the board to "Error" status

There is no retry mechanism. A transient API overload causes a full pipeline abort for the ticket. Manual recovery required:
- Delete LangGraph checkpoints from `~/.pipeline/graph_checkpoints.db`
- Move the board item back to "Ready To Pick Up" via GraphQL mutation

**Fix needed**: Add exponential backoff + retry in the subprocess spawn layer, or add an "auto-retry from Error" path in the graph that doesn't require checkpoint deletion.

---

### Bug 2: `PIPELINE_RESULT_PATH` Not Set by Old `spawn_headless`
**Severity**: High  
**Stage affected**: AI Planning (and any other skill-based stage)  
**Root cause**: `pipeline_poller.py`:`spawn_headless` (old code, still running in-memory)

The old `spawn_headless` function does not set the `PIPELINE_RESULT_PATH` environment variable in the subprocess environment. The planning skill (`plan-github-tickets.md`) contains a "Write Pipeline Result" section that writes to `$PIPELINE_RESULT_PATH`. Since the env var is absent, the result file is never created.

After the planning agent completed successfully (rc=0, plan posted to GitHub), the pipeline **hung for ~45 minutes** polling every 10 seconds for `~/.pipeline/results/24/ai_planning_0.json`. Manual intervention was needed to write the file:

```json
{"outcome":"done","plan_content":"","plan_comment_url":"https://github.com/juanocampovgr/AgecticPipeline/issues/24#issuecomment-4780515969"}
```

The new `graph/runner.py`:`_launch_headless` does set `PIPELINE_RESULT_PATH`, but it is not used by the currently-running poller.

**Fix needed**: Restart the pipeline poller to use the new in-memory code, which includes `_launch_headless` with `env["PIPELINE_RESULT_PATH"] = str(result_path)`.

---

### Bug 3: Quality Check Agent Failed to Post Completion Marker
**Severity**: High  
**Stage affected**: AI Quality Check

All 3 CI checks passed:
- Detekt: ✅ run `28036877749`
- Android Lint: ✅ run `28036878593`
- Unit Tests: ✅ run `28036880509`

Despite this, the quality check agent exited without posting the `<!-- ai-quality:done -->` comment to GitHub. The shell script (a separate zsh process spawned by the Terminal helper) was stuck at a `read -k1` prompt after Claude exited. This indicates a process lifecycle issue: the Terminal helper process that hosts the quality check did not observe Claude's exit and post the marker.

Manual intervention: posted the quality done comment manually to advance the pipeline.

**Fix needed**: Skill completion should not depend on a shell wrapper reading Claude's exit code interactively. The skill itself should post the marker before returning, not via a wrapper `read -k1` + `then post` flow.

---

### Bug 4: Ship Agent Exited Silently — No PR, No Marker
**Severity**: High  
**Stage affected**: Ship

The ship agent (PID 36146) ran `/ship-tickets-with-runner --ticket 24` in the worktree at `/Users/juanocampo/.pipeline/worktrees/24`. Confirmed state at time of run:
- Branch `juanocampovgr/ANDROID-18640-migrate-all-deep-links-to-use-inapp-deep-link` existed on remote
- Commit `28b30900437e20c2a8f1664e4f4afbc19772fa8c` confirmed via `git ls-remote`
- All CI had passed for this branch (from quality check stage)

After the agent ran:
- No log file created at `~/.pipeline/logs/Ready_To_Ship_-_AI-24-*.log`
- No PR created in `grindrllc/grindr-android`
- No `<!-- ai-ship:done -->` comment posted
- Pipeline stuck at `waiting_ship_marker` indefinitely

This is the same silent failure pattern as Bug 3 — the ship skill's Terminal helper exited without completing.

**Fix needed**: Same as Bug 3. Marker posting must be reliable and not depend on interactive shell wrappers.

---

### Bug 5: GitHub Issue Auto-Closed During "Ready To Ship" Transition
**Severity**: Medium  
**Stage affected**: Transition from self-review to ship

The GitHub issue was auto-closed at 15:48:36Z with `stateReason: COMPLETED`. This happened approximately 2 minutes after the self-review completed and 1 minute after `impl-approved` was added.

Closing the issue at "Ready To Ship" is semantically incorrect — the work has not been shipped yet (no PR exists). This makes it difficult to track that the ticket is still in-flight and prevents the pipeline from further commenting on the issue since it is closed.

**Fix needed**: GitHub issue should only be closed when the PR is actually merged (or at the `done` terminal node), not when transitioning to the ship stage.

---

## Architecture Observations

### OLD vs NEW Code Mismatch
The running poller (PID 57525, started Jun 22 16:39) executes code loaded at startup that diverges from the current on-disk code:

| Aspect | Running (old) | On-disk (new) |
|---|---|---|
| Interrupt names | `waiting_plan_marker`, `waiting_impl_marker`, `waiting_quality_marker`, `waiting_self_review_marker`, `waiting_ship_marker` | `waiting_plan_approval`, `waiting_impl_approval`, `waiting_pr_outcome` |
| Result file mechanism | Not set — no `PIPELINE_RESULT_PATH` in subprocess env | `_launch_headless` sets `env["PIPELINE_RESULT_PATH"]` |
| Gate handler | `_handle_gate` checks GitHub comments for HTML marker comments | `_handle_gate` checks LangGraph interrupt name, reads JSON result file |
| State schema | Flat dict (15+ top-level keys) | Nested `TicketState` with `identity`, `impl`, `ship`, `review` groups |
| Node names | `spawn_plan`, `wait_plan`, `wait_plan_approval`, etc. | `route_entry`, `plan`, `gate_plan_approval`, etc. |

The new architecture has **never been tested in production**. Every stage executed in this test run used the old code path.

### Checkpoint State at End of Run
```
thread_id: "24"
last_run_id: Ready_To_Ship_-_AI-24-0d74e3be-terminal
branch:to:wait_ship: None
_plan_marker_outcome: done
_impl_marker_outcome: done
_quality_check_outcome: done
_self_review_outcome: done
impl_approval_type: impl-approved
self_review_passed: True
```

The pipeline is waiting at `wait_ship` with `branch:to:wait_ship: None` (ship hasn't returned a result).

---

## What Worked Well

### Implementation Quality
The AI implementation agent correctly:
- Identified all 41 `startAsOnlyActivity` call sites in `GeneralDeepLinks.kt`
- Migrated every call to `startReusingTask` with the correct parameter mapping
- Modified `HomeActivity.startReusingTask` to accept an `allowLaunchInBackground` param for parity
- Changed exactly 3 files (no scope creep)
- Committed cleanly with message `feat: AI implementation for #24`

### CI Coverage
All three CI checks passed on the first attempt — no flaky tests, no lint issues, no static analysis failures.

### Self-Review Accuracy
The self-review agent correctly validated the implementation against the plan and confirmed all 41 migrations were present.

### Planning Quality
The planning agent correctly scoped the work (M complexity, ~4h), identified the correct approach, and flagged the right open questions about `openBoost` migration and handlers outside `GeneralDeepLinks`.

---

## Issues Requiring Immediate Action

1. **Restart the poller** — The running process (PID 57525) is using stale in-memory code. Restarting it will activate the new `runner.py` architecture with proper `PIPELINE_RESULT_PATH` handling.

2. **Manually advance ticket #24 ship stage** — The branch is on the remote. A PR needs to be created manually, or the `<!-- ai-ship:done -->` marker posted to unblock the pipeline for this run.

3. **Fix issue auto-close timing** — The poller's board-status transition logic should not close the GitHub issue until the PR is merged.

4. **Fix marker-posting reliability** — Skill completion markers must be written by the skill itself (before process exit), not by a shell wrapper that depends on observing Claude's exit code.

---

## Manual Intervention Count: 5

| # | Intervention | Stage | Root Cause |
|---|---|---|---|
| 1 | Delete checkpoints + reset board status | Planning | Bug 1: no retry on 529 |
| 2 | Write `ai_planning_0.json` result file | Planning | Bug 2: PIPELINE_RESULT_PATH not set |
| 3 | Add `plan-approved` label | Gate: Plan Approval | Expected human gate |
| 4 | Post `<!-- ai-quality:done -->` marker | Quality Check | Bug 3: quality agent silent failure |
| 5 | Add `impl-approved` label | Gate: Impl Approval | Expected human gate |

Interventions 1, 2, and 4 were caused by pipeline bugs. Interventions 3 and 5 are expected human gates.
