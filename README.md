# AgecticPipeline

An AI-powered GitHub Project automation system that continuously polls a project board and orchestrates Claude AI agents through a structured development workflow — from planning through implementation to shipping — across multiple repositories.

## Overview

AgecticPipeline watches a GitHub Project board and automatically drives tickets through AI-assisted stages. Human review gates keep a developer in the loop at key checkpoints, while the daemon handles the repetitive work: running LangGraph graph threads per ticket, managing git worktrees, detecting completion, and moving tickets forward.

The workflow is encoded as a checkpointed LangGraph `StateGraph`. Each ticket runs as an independent graph thread persisted to SQLite — resuming correctly after crashes without re-triggering completed stages. A dedicated `recover` entry node enables safe restart of errored tickets from the failed stage rather than from scratch.

```
Non-spike: [Ready To Pick Up] → AI Planning → [plan-approved]
         → AI Implementation → AI Quality Check → Self Review
         → [impl-approved] → Ready To Ship - AI → In PR
         → [CI fail → AI-PR Assistance → fix → In PR → ...]
         → [comments-approved → AI-PR Assistance → respond → In PR → ...]
         → Done

Spike:     [Ready To Pick Up] → AI Implementation
         → [impl-approved → Done | followup-approved → create follow-ups → Done]
```

## Architecture

| File/Dir | Purpose |
|----------|---------|
| `pipeline_poller.py` | Daemon. Polls GitHub Project board, drives LangGraph threads, manages worktrees, transitions ticket statuses. |
| `agentic_dev_pipe.py` | CLI. Start/stop/restart daemon, monitoring commands, graph visualizer. |
| `github_api.py` | GitHub GraphQL and REST utilities (project board, issues, labels, PRs, Actions). |
| `process_utils.py` | Process management utilities (SIGTERM/SIGKILL helpers, PID tracking). |
| `graph/state.py` | `TicketState` TypedDict — the graph thread state shape. |
| `graph/workflow.py` | `StateGraph` builder + Mermaid visualizer. |
| `graph/runner.py` | `run_stage()` — spawns Claude CLI subprocesses and awaits result files. |
| `graph/events.py` | In-process event bus for real-time dashboard updates. |
| `graph/schemas.py` | Pydantic result schemas for inter-process communication. |
| `graph/store.py` | LangGraph store helpers (retry caps, per-repo config). |
| `graph/terminal.py` | AppleScript/Terminal.app helpers for interactive spawns. |
| `graph/nodes/` | Individual node implementations (one file per node). |
| `graph/subgraphs/` | Reusable subgraph components. |
| `pipeline/dashboard.py` | Live terminal dashboard for pipeline status. |

The daemon runs as a macOS launchd service (`dev.juan.pipeline-poller`).

## Workflow

### Graph Topology

#### Phase 1 — Development (entry → ship)

```mermaid
flowchart TD
    START([START]) --> recover{recover}

    recover -->|normal run| route_entry{route_entry}
    recover -.->|recovery: resumes at failed stage| plan
    recover -.->|recovery| implement
    recover -.->|recovery| quality
    recover -.->|recovery| self_review
    recover -.->|recovery| ship

    route_entry -->|normal| plan[plan]
    route_entry -->|spike / entry=implement| implement[implement]

    plan -->|done| gate_plan_approval([gate_plan_approval\ninterrupt: plan-approved])
    plan -->|error / timeout / crash| escalate_error

    gate_plan_approval --> implement

    implement -->|non-spike done| quality[quality]
    implement -->|spike done| gate_impl_approval
    implement -->|error / timeout / crash| escalate_error

    quality -->|done| self_review[self_review]
    quality -->|needs_human| needs_human
    quality -->|error / timeout / crash| escalate_error

    self_review -->|passed| gate_impl_approval([gate_impl_approval\ninterrupt: impl-approved])
    self_review -->|retry| implement
    self_review -->|retry cap exceeded| escalate_error

    gate_impl_approval -->|impl-approved, non-spike| ship[ship]
    gate_impl_approval -->|impl-approved, spike| done
    gate_impl_approval -->|followup-approved, spike| followups[followups]

    ship -->|done| monitor_pr(["monitor_pr ···\n(Phase 2)"])
    ship -->|retry| ship
    ship -->|error / timeout / crash| escalate_error

    followups -->|done| done
    followups -->|error / timeout / crash| escalate_error

    done([done]) --> END([END])
    needs_human([needs_human]) --> END
    escalate_error([escalate_error]) --> END

    classDef spawn fill:#dbeafe,stroke:#3b82f6,color:#1e3a8a
    classDef wait fill:#fef9c3,stroke:#eab308,color:#713f12
    classDef terminal fill:#dcfce7,stroke:#22c55e,color:#14532d
    classDef error fill:#fee2e2,stroke:#ef4444,color:#7f1d1d
    classDef route fill:#ede9fe,stroke:#8b5cf6,color:#3b0764
    classDef handoff fill:#f0fdf4,stroke:#86efac,color:#14532d,stroke-dasharray:4 4

    class plan,implement,quality,self_review,ship,followups spawn
    class gate_plan_approval,gate_impl_approval wait
    class done terminal
    class needs_human,escalate_error error
    class recover,route_entry route
    class monitor_pr handoff
```

#### Phase 2 — Monitor (In PR → Done)

```mermaid
flowchart TD
    ship_done(["··· ship\n(Phase 1)"]) --> monitor_pr([monitor_pr\ninterrupt: CI / comments])

    recover -.->|recovery| fix_ci
    recover -.->|recovery| respond

    monitor_pr -->|done| done
    monitor_pr -->|fix_ci| fix_ci[fix_ci]
    monitor_pr -->|respond| respond[respond]
    monitor_pr -->|needs_human| needs_human

    fix_ci -->|done| monitor_pr
    fix_ci -->|needs_human / timeout / cap| needs_human

    respond -->|done| monitor_pr
    respond -->|needs_human / timeout / cap| needs_human

    done([done]) --> END([END])
    needs_human([needs_human]) --> END

    classDef spawn fill:#dbeafe,stroke:#3b82f6,color:#1e3a8a
    classDef wait fill:#fef9c3,stroke:#eab308,color:#713f12
    classDef terminal fill:#dcfce7,stroke:#22c55e,color:#14532d
    classDef error fill:#fee2e2,stroke:#ef4444,color:#7f1d1d
    classDef route fill:#ede9fe,stroke:#8b5cf6,color:#3b0764
    classDef handoff fill:#f0fdf4,stroke:#86efac,color:#14532d,stroke-dasharray:4 4

    class fix_ci,respond spawn
    class monitor_pr wait
    class done terminal
    class needs_human error
    class recover route
    class ship_done handoff
```

Node color key: **blue** = AI work · **yellow** = interrupt/wait gate · **green** = success terminal · **red** = error terminal · **purple** = router · dashed arrows = recovery paths · dashed border = cross-phase handoff

### Board Statuses

| Status | Owner | Description |
|--------|-------|-------------|
| Backlog | Human | Unstarted tickets |
| Ready To Pick Up | Human / System | Queued for next pipeline run; also set when operator requeues an errored ticket for recovery |
| AI Planning | AI | Claude generates an implementation plan |
| Ready to Review then Plan | Human | Review gate — approve with `plan-approved` label |
| AI Implementation | AI | Claude executes the plan in an isolated worktree |
| AI Quality Check | AI | Claude runs detekt/lint/unit tests on affected modules |
| Ready to review Implementation | Human | Review gate — approve with `impl-approved` or `followup-approved` label |
| Ready To Ship - AI | AI | Claude prepares and ships the PR (also set during spike follow-up creation) |
| In PR | Human | PR open, awaiting merge / monitoring CI |
| AI-PR Assistance | AI | Claude is actively fixing CI or implementing review comments |
| Error | Human | Unrecoverable error; operator can requeue by moving back to `Ready To Pick Up` |
| Done | — | Complete |

### Human Gates

| Gate | Label | Trigger status | What happens |
|------|-------|----------------|--------------|
| Plan approval | `plan-approved` | `Ready to Review then Plan` | Advances to AI Implementation |
| Impl approval | `impl-approved` | `Ready to review Implementation` | Non-spike: advances to shipping; spike: closes as Done |
| Spike follow-ups | `followup-approved` | `Ready to review Implementation` (spike only) | AI creates follow-up tickets, closes spike as Done |
| Review comments | `comments-approved` | `In PR` | Ticket → AI-PR Assistance, AI implements PR comments, returns to In PR |

The poller removes all approval labels after consuming them.

### Completion Detection

Claude agents signal completion by posting an HTML comment marker on the GitHub issue:

| Marker | Stage |
|--------|-------|
| `<!-- ai-plan:done -->` | Planning finished |
| `<!-- ai-impl:done -->` | Implementation finished |
| `<!-- ai-quality:done -->` | Quality check passed |
| `<!-- ai-self-review:done -->` | Self-review passed |
| `<!-- ai-ship:done -->` | Shipping finished |
| `<!-- ai-ci-fix:done -->` | CI fix applied |
| `<!-- ai-review-response:done -->` | Review comments addressed |
| `<!-- ai-followups:done -->` | Follow-up tickets created |

The poller only counts markers posted after the spawn timestamp, preventing stale comments from triggering false completions.

### Spike Tickets

Tickets labeled `spike` follow an abbreviated path:

- Skips planning — goes directly to AI Implementation.
- Skips quality check, self-review, and shipping.
- After impl review: `impl-approved` → Done; `followup-approved` → AI creates follow-up tickets → Done.
- Uses the `/spike-tickets` Claude skill instead of the normal implementation skill.

### Quality Check

Inserted after implementation for non-spike tickets, before self-review:

1. Determines mode: label `quality-mode:local` → run `gradlew` locally; default → dispatch GitHub Actions runner (canonical CI env).
2. Runs detekt, lint, and unit tests scoped to modules changed in the branch.
3. Auto-applies fixable issues and pushes to the branch.
4. Unrecoverable failures requiring human judgment route to `needs_human`; infrastructure/crash errors route to `escalate_error`.

### Self-Review

Inserted between quality check and human review for non-spike tickets:

1. Claude reviews its own implementation against the plan.
2. Runs an internal code review (`/grindr_code_review` on Android, `/code-review` elsewhere).
3. If review fails, loops back to implementation. Max retries are configurable per repo (default 2).
4. Retry cap exceeded → `escalate_error`.

### CI Auto-Fix

When a PR's GitHub Actions run fails, `monitor_pr` classifies the failure:

- **In-scope** (lint, formatting, compilation, test flake on current-branch changes): ticket → `AI-PR Assistance`, Claude fixes and pushes, posts a comment with fix summary + commit link, ticket returns to `In PR`, CI re-runs.
- **Out-of-scope** (pre-existing failure, infra issue, unrelated test): Claude posts an explanation comment, ticket → `needs_human`.
- Max fix attempts configurable per repo (default 3); cap exceeded → `needs_human`.

### PR Review Comment Responder

When human adds `comments-approved` label while ticket is `In PR`:

1. Ticket → `AI-PR Assistance`.
2. Claude fetches all unresolved PR comments and implements the requested changes.
3. Claude replies `"Done"` on each comment thread it addressed.
4. Ticket returns to `In PR`, `comments-approved` label removed.
5. Max response rounds configurable per repo (default 2); cap exceeded → `needs_human`.

### Error Recovery

When an operator moves a ticket from `Error` back to `Ready To Pick Up`, the poller:

1. Detects the ticket has an existing graph thread (from the prior run).
2. Scans run history to identify the last failed stage.
3. Sets `identity.is_recovery = True` and `identity.recovery_target` on the thread state.
4. The `recover` entry node routes directly to the failed stage rather than starting fresh.
5. For worktree-based stages (`implement`, `quality`, `self_review`, `ship`), the worktree is rebuilt clean so a crashed subprocess cannot poison the retry.

## Features

### LangGraph Checkpointing

Each ticket runs as a separate LangGraph thread with a unique `thread_id`. State is persisted to SQLite via `SqliteSaver` at `~/.pipeline/graph_checkpoints.db`. On daemon restart, tickets resume from their last completed graph node — no work is lost or re-triggered.

### Git Worktree Management

For each implementation task, the poller:

1. Fetches `origin/master` to ensure it is current.
2. Creates an isolated git worktree at `~/.pipeline/worktrees/{ticket_number}/`.
3. Creates a branch named `juanocampovgr/{ticket_number}`.
4. Cleans up the worktree and branch after the stage completes.
5. Force-cleans existing branches if a ticket is re-queued.

Worktrees are also rebuilt from a known-clean state during error recovery.

### Spawn Modes

- **Headless** (Planning, Quality Check, Self-Review, Follow-ups) — Async subprocess; output captured to per-run log files in `~/.pipeline/logs/`.
- **Terminal** (Implementation, Shipping, CI Fix, Review Response) — Opens a Terminal.app window via AppleScript so the developer can watch the live session.

### Per-Repo Concurrency

Each repository (Android, iOS, Backend) has its own concurrency budget (`MAX_CONCURRENT_PER_REPO`, default 1). This prevents two Android tickets from creating conflicting worktrees simultaneously, while still allowing Android and Backend to run in parallel.

### Session Guards

Before spawning a new Claude process for a ticket, the poller kills any existing Claude processes associated with it (SIGTERM with 3-second grace, then SIGKILL) to prevent duplicate sessions.

### Stale Spawn Detection

If a spawned task produces no completion marker within a configurable threshold, the poller flags it as stale.

| Stage | Default | Env var |
|-------|---------|---------|
| Planning | 15 minutes | `STALE_PLAN_SECONDS` |
| Implementation | 60 minutes | `STALE_IMPL_SECONDS` |
| Shipping | 30 minutes | `STALE_SHIP_SECONDS` |

Stale spawns appear in the `metrics` command output with elapsed time vs. threshold.

### State Persistence

Two complementary persistence layers:

- **LangGraph SQLite** — `~/.pipeline/graph_checkpoints.db` — primary source of truth, full node-level checkpoint per ticket.
- **Legacy state.json** — `~/.pipeline/state.json` — kept in sync from graph state for backwards compatibility with monitoring CLI commands.

### Error Handling & Recovery

- Missing repo path → posts a failure comment, moves ticket to **Error**.
- Worktree creation failure → posts a failure comment, moves ticket to **Error**.
- Terminal spawn failure → posts a failure comment, moves ticket to **Error**.
- GitHub GraphQL errors → retried with exponential backoff (2s, 4s, 8s).
- Auth errors (401/403) → fail fast, no retry.
- Quality check failures requiring human judgment → `needs_human` → **Error**.
- Self-review failures → re-implement loop (max retries before `escalate_error` → **Error**).
- CI fix failures → fix loop (max attempts before `needs_human` → **Error**).
- Review comment loops → max rounds before `needs_human` → **Error**.
- Operator requeue from Error → `recover` node resumes from the failed stage cleanly.

## CLI Reference

```
agentic-dev-pipe <command>
```

| Command | Description |
|---------|-------------|
| `start` | Load the launchd plist and start the daemon |
| `stop [--force]` | Unload the launchd plist and stop the daemon; `--force` kills the process immediately |
| `restart` | Stop then start the daemon |
| `status` | Show daemon PID, last/next poll time, and active tickets |
| `metrics` | Show ticket counts by status and stale spawn warnings |
| `logs` | Tail all per-run AI output logs |
| `poller` | Tail the daemon's own heartbeat log |
| `errors` | Show recent error/warning lines from the poller log |
| `graph` | Print a Mermaid diagram of the current workflow graph |

## Claude Skills

| Skill | Stage | Description |
|-------|-------|-------------|
| `/plan-github-tickets` | AI Planning | Generates implementation plan, posts to issue |
| `/code-tickets` | AI Implementation | Executes plan in worktree, opens interactive Terminal session |
| `/quality-check` | AI Quality Check | Runs detekt, lint, unit tests on affected modules; auto-fixes and pushes |
| `/self-review-ticket` | Self-Review | Reviews implementation against plan, runs internal code review |
| `/ship-agentic-ticket` | Ready To Ship - AI | Creates draft PR and pushes to remote |
| `/fix-ci-failure` | AI-PR Assistance (CI Fix) | Fetches CI logs, classifies and fixes in-scope failures |
| `/respond-to-review` | AI-PR Assistance (Review) | Implements unresolved PR review comments |
| `/spike-tickets` | AI Implementation (spike) | Research spike, posts findings as issue comment |
| `/spike-tickets --create-followups` | Ready To Ship - AI (spike) | Creates follow-up tickets from spike research |

## Configuration

Set via environment variables or a `.env` file in the pipeline root.

### Required

| Variable | Description |
|----------|-------------|
| `PROJECT_OWNER` | GitHub user or org owning the project board |
| `PROJECT_NUMBER` | Project board number |
| `PROJECT_NODE_ID` | GraphQL node ID of the project |
| `STATUS_FIELD_ID` | GraphQL node ID of the status field |
| `GITHUB_TOKEN` | GitHub PAT (falls back to `gh auth token`) |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_BIN` | `claude` | Path to the Claude CLI binary |
| `ANDROID_REPO_PATH` | — | Local path to the Android repository |
| `IOS_REPO_PATH` | — | Local path to the iOS repository |
| `BACKEND_REPO_PATH` | — | Local path to the Backend repository |
| `POLL_INTERVAL_SECONDS` | `120` | How often to poll the board (seconds) |
| `MAX_CONCURRENT_PER_REPO` | `1` | Maximum simultaneous AI spawns per repository |
| `STALE_PLAN_SECONDS` | `900` | Stale threshold for planning (seconds) |
| `STALE_IMPL_SECONDS` | `3600` | Stale threshold for implementation (seconds) |
| `STALE_SHIP_SECONDS` | `1800` | Stale threshold for shipping (seconds) |
| `PIPELINE_DIR` | `~/.pipeline` | Root directory for state and logs |
| `STATE_FILE` | `~/.pipeline/state.json` | Legacy state persistence file |
| `LOG_DIR` | `~/.pipeline/logs` | Per-run log directory |

## Requirements

- macOS (uses launchd, Terminal.app, AppleScript)
- Python 3.10+
- [`langgraph`](https://github.com/langchain-ai/langgraph) + `langgraph-checkpoint-sqlite`
- [`anthropic`](https://github.com/anthropics/anthropic-sdk-python)
- [`httpx`](https://www.python-httpx.org/)
- [`gh`](https://cli.github.com/) CLI, authenticated
- `git` with worktree support
- `claude` CLI in PATH

## Runtime File Locations

| Path | Purpose |
|------|---------|
| `~/.pipeline/graph_checkpoints.db` | LangGraph SQLite checkpoint store (primary state) |
| `~/.pipeline/state.json` | Legacy ticket state (synced from graph for CLI compat) |
| `~/.pipeline/poller.log` | Daemon heartbeat and error log |
| `~/.pipeline/logs/` | Per-run AI output logs |
| `~/.pipeline/worktrees/{n}/` | Isolated git worktrees per ticket |
| `~/Library/LaunchAgents/dev.juan.pipeline-poller.plist` | launchd service definition |
