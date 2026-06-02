# Deployment Guide — LangGraph Pipeline

## Prerequisites
- The daemon is currently running the old `pipeline_poller.py`
- Existing tickets in progress should be allowed to finish or manually moved to `Done` / `Error` before switching (the new graph engine starts fresh threads; it won't pick up mid-flight tickets from the old state machine)

---

## Step 1 — Stop the daemon

```bash
pipe-stop
```

Verify it's stopped:
```bash
adp status   # should say "NOT RUNNING"
```

---

## Step 2 — Check out the branch

```bash
cd ~/Documents/Grindr/AgecticPipeline
git fetch origin
git checkout juanocampovgr/project-readme-features
```

Confirm the new files are present:
```bash
ls graph/ github_api.py requirements.txt .claude/commands/
```

---

## Step 3 — Install new dependencies

```bash
.venv/bin/pip install langgraph langgraph-checkpoint-sqlite
```

Verify:
```bash
.venv/bin/python -c "import langgraph; print('langgraph OK')"
```

---

## Step 4 — Add two new statuses to GitHub Project board

The pipeline uses two new board statuses. You need to create them in the GitHub Project settings UI and then paste their option IDs into the plist.

1. Go to **github.com → Projects → Project #2 → Settings → Status field**
2. Add two new options:
   - `AI-PR Assistance`
   - `Needs Human`
3. To get each option's ID, use the GitHub GraphQL explorer or run:

```bash
gh api graphql -f query='
query {
  node(id: "PVT_kwHOCX568c4BYUZZ") {
    ... on ProjectV2 {
      fields(first: 20) {
        nodes {
          ... on ProjectV2SingleSelectField {
            name
            options { id name }
          }
        }
      }
    }
  }
}' | jq '.data.node.fields.nodes[] | select(.name == "Status") | .options[] | select(.name | test("AI-PR|Needs Human"))'
```

Copy the `id` values (short hex strings like `"a1b2c3d4"`).

---

## Step 5 — Update the plist

Open `~/Library/LaunchAgents/dev.juan.pipeline-poller.plist` and make these changes:

**a) Paste the new status IDs** (add inside `<dict>` under EnvironmentVariables):
```xml
<key>STATUS_ID_AI_PR_ASSISTANCE</key><string>PASTE_ID_HERE</string>
<key>STATUS_ID_NEEDS_HUMAN</key><string>PASTE_ID_HERE</string>
```

**b) Add per-repo concurrency limit** (optional, default is 1):
```xml
<key>MAX_CONCURRENT_PER_REPO</key><string>1</string>
```

> If you skip step 4/5, the pipeline still works — tickets that need `AI-PR Assistance` or `Needs Human` will log a warning and skip the board move, but all other stages function normally.

---

## Step 6 — Smoke-test the import before starting

```bash
cd ~/Documents/Grindr/AgecticPipeline
.venv/bin/python -c "
from graph.workflow import visualize_workflow
print(visualize_workflow())
"
```

Should print a Mermaid diagram. If it errors, check the error before proceeding.

Also verify the CLI works:
```bash
adp graph    # same diagram via CLI
adp status   # should say NOT RUNNING (daemon still stopped)
```

---

## Step 7 — Start the daemon

```bash
pipe-start
```

Immediately tail the log to catch startup errors:
```bash
pipe-poller-log
```

Expected first lines:
```
=== pipeline-poller starting (LangGraph edition) ===
  project:   #2 owner=juanocampovgr
  ...
  LangGraph workflow compiled and ready
polling board...
```

If you see `ModuleNotFoundError` or `KeyError`, stop the daemon and fix the issue before proceeding.

---

## Step 8 — End-to-end verification

Work through the verification table in order. Use a **test spike ticket** first (shortest path).

| Test | What to do | Expected result |
|---|---|---|
| Daemon starts | `pipe-start` + `pipe-poller-log` | "LangGraph workflow compiled and ready" in log |
| Import check | `adp graph` | Mermaid diagram prints |
| Status check | `adp status` | Shows daemon PID and poll time |
| Ticket pickup | Move any ticket to `Ready To Pick Up` | Ticket advances to `AI Planning` or `AI Implementation` (spike) within 2 min |
| Plan approval | Add `plan-approved` label | Ticket advances to `AI Implementation` |
| Impl approval | Add `impl-approved` label | Ticket advances to `Ready To Ship - AI` |
| Ship done | `/ship-tickets` posts done marker | Ticket advances to `In PR` |
| CI failure (in-scope) | Break CI with a small lint error on the PR | Ticket moves to `AI-PR Assistance`, fix pushed, moves back to `In PR` |
| CI failure (out-of-scope) | Pre-existing CI failure | Comment posted explaining skip, ticket moves to `Needs Human` |
| Review comments | Human leaves comments, add `comments-approved` label | Ticket moves to `AI-PR Assistance`, AI replies "Done" on addressed threads, moves back to `In PR` |
| Spike + followups | Add `followup-approved` label on spike in `Ready to review Implementation` | Ticket moves to `Ready To Ship - AI`, follow-up tickets created, ticket moves to `Done` |

---

## Rollback

If anything is broken and you need to revert immediately:

```bash
pipe-stop
cd ~/Documents/Grindr/AgecticPipeline
git checkout main          # or whichever branch was running before
pipe-start
pipe-poller-log            # confirm "polling board..." appears
```

The old `~/.pipeline/state.json` is untouched by the new code, so the old daemon picks up existing ticket state correctly.

The new `~/.pipeline/graph_checkpoints.db` can be deleted safely if you roll back — it only contains LangGraph state.

---

## Notes

- **In-flight tickets**: The new daemon ignores tickets that are mid-flight in the old state machine (no graph thread exists for them). Simplest approach: let them finish naturally before deploying, or manually move them back to `Ready To Pick Up` after deploying so the new graph picks them up fresh.
- **graph_checkpoints.db**: Persists across restarts. If you want a completely clean slate, delete `~/.pipeline/graph_checkpoints.db` before starting.
- **Stale threshold metrics**: `adp metrics` still works and reports stale spawns using the same thresholds as before.
