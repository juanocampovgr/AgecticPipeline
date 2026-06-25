# code-tickets

Implements the code for a ticket in **AI Implementation** status. The poller has already:
- Created a git worktree at `~/.pipeline/worktrees/{N}` on branch `juanocampovgr/{N}`
- Opened this Terminal session in that worktree directory

This skill reads the most recent `## Implementation Plan` comment from the issue, implements
PR 1 from the plan in the **current directory**, commits and pushes the branch, then posts a
comment with the `<!-- ai-impl:done -->` marker. Quality checks are handled by the separate
`quality-check` pipeline node that runs after this skill completes. The poller then cleans
up the worktree and advances the ticket.

On any unrecoverable failure: post a `<!-- ai-impl:error -->` comment on the issue — the
poller detects this marker and moves the ticket to the **Error** column for human resolution.

**NEVER create a PR. NEVER change ticket status. NEVER run `git checkout` or `git branch`.**

---

## ERROR REPORTING PROCEDURE

Call this on **every** unrecoverable failure before EXIT:

```bash
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "⚠️ **Implementation failed — {STAGE}**

**Error:** {ERROR_DESCRIPTION}
**Time:** $(date '+%Y-%m-%d %H:%M:%S')

The ticket has been moved to **Error** for human review. Fix the issue and move it back to **AI Implementation** to retry.

<!-- ai-impl:error -->"
```

The `<!-- ai-impl:error -->` marker tells the poller to move the ticket to the Error column on
its next poll cycle.

---

## PHASE 0 — Bootstrap

Read `~/.claude/skills/plan-github-tickets/config.md` and parse every KEY=VALUE line:

```
owner        = GITHUB_OWNER
android_repo = ANDROID_REPO  (local path)
ios_repo     = IOS_REPO      (local path)
backend_repo = BACKEND_REPO  (local path)
```

Derive GitHub org/owner from git remotes when posting comments:
- `android_github_org` = from `git -C {android_repo} remote get-url origin`
- Same for ios and backend

Parse `$ARGUMENTS`:
- `--ticket <N>` → single ticket mode (required when called by poller)
- `--dry-run`    → print what would happen, skip checks/commit/push, post nothing
- `--repo <name>` → restrict to this repo (e.g. "grindr-android")

---

## PHASE 0b — Detect already-implemented remote branch

Before exploring any code, check whether the target remote branch already contains
a previous pipeline implementation. If it does, reuse it instead of re-implementing
from scratch — this prevents divergent-history push failures on pipeline restarts.

A branch is only reused when BOTH conditions hold:
- It is **1–3 commits** ahead of master (1 impl commit ± 1–2 auto-fix commits).
- It touches **≤ 50 files** (a larger footprint indicates contamination from a rebase replay or accumulated multi-retry runs).

If either bound is exceeded, fall through to a fresh implementation so the poller's
`setup_worktree` (which already deleted the over-large remote branch) can provide a clean base.

```bash
BRANCH="juanocampovgr/{ISSUE_NUMBER}"

# Does the remote branch exist?
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  # Fetch it locally so we can inspect it
  git fetch origin "$BRANCH" 2>/dev/null

  AHEAD=$(git rev-list --count "origin/master..origin/$BRANCH" 2>/dev/null || echo 0)
  FILES_TOUCHED=$(git diff --name-only "origin/master...origin/$BRANCH" 2>/dev/null | wc -l | tr -d ' ')

  if [ "$AHEAD" -ge 1 ] && [ "$AHEAD" -le 3 ] && [ "$FILES_TOUCHED" -le 50 ]; then
    echo "Remote branch '$BRANCH' is $AHEAD commit(s) ahead of master ($FILES_TOUCHED files) — reusing it."

    # Bring the worktree in sync with the remote branch tip
    git reset --hard "origin/$BRANCH"

    HEAD_SHA=$(git rev-parse HEAD)

    # Write a success result file so the pipeline can advance immediately
    if [ -n "$PIPELINE_RESULT_PATH" ]; then
      mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
      cat > "$PIPELINE_RESULT_PATH" << RESULT_EOF
{
  "outcome": "done",
  "branch": "$BRANCH",
  "files_changed": [],
  "impl_summary": "Reusing existing implementation from remote branch (pipeline restart)",
  "commit_shas": ["$HEAD_SHA"]
}
RESULT_EOF
    fi

    # Post the done marker so the poller can detect completion via comment too
    gh issue comment {ISSUE_NUMBER} \
      --repo {ISSUE_REPO_FULL} \
      --body "**Implementation reused — existing branch** \`$BRANCH\` already contains a valid implementation commit (\`$HEAD_SHA\`). Advancing to quality check.

<!-- ai-impl:done -->"

    EXIT  # Skip all remaining phases
  else
    echo "Remote branch '$BRANCH' failed sanity check (ahead=$AHEAD files=$FILES_TOUCHED) — skipping reuse, will implement fresh."
  fi
fi
```

If the branch does not exist or fails the sanity check, continue to PHASE 1.

---

## PHASE 1 — Find the ticket

If `--ticket N` passed: scan all three repos for the issue:
```bash
gh issue view {N} --repo {org}/{repo} --json number,title,labels,comments 2>/dev/null
```
Use the first repo that returns a result.

Build:
```
ticket = { issue_number, issue_repo_full (org/repo), repo_name, title, comments }
```

If no ticket found: print "Ticket #{N} not found in any configured repo." EXIT.

---

## PHASE 2 — Extract the implementation plan

From `ticket.comments`, find the most recent comment whose body contains `## Implementation Plan`.

Extract the **PR 1** section from the plan:
- Look for a heading like `### PR 1` or `**PR 1**` or the first PR entry
- Extract: PR title, affected files ("Exact Code Changes" section), tests to add/update

Store as:
```
plan_pr1 = {
  title:         "PR 1: ...",
  code_changes:  [...],   # list of { file_path, description, new_signatures }
  tests:         [...],   # list of { file_path, test_name, description }
}
```

If no Implementation Plan comment found: print error and EXIT.

---

## PHASE 3 — Implement (model: sonnet)

**Context:** The poller has already created a git worktree and opened this Terminal session in it.
You are already on branch `juanocampovgr/{ISSUE_NUMBER}`. Work in `pwd` (the current directory).

Launch a coding subagent with `model: "sonnet"`:

### Subagent prompt

```
You are a coding subagent. Implement the code described below.
Do NOT create a PR.
Do NOT change any ticket/issue status.
Work directly in the current directory (pwd) — the worktree is already configured on the correct branch.
Return a JSON result as described at the end.

## Ticket
- Issue: #{ISSUE_NUMBER} in {ISSUE_REPO_FULL}
- Title: {TITLE}

## Branch
You are already on branch `juanocampovgr/{ISSUE_NUMBER}` in a dedicated worktree.
Work directly in the current directory (pwd).

## PR 1 to implement
Title: {PR1_TITLE}

### Code changes
{CODE_CHANGES_FROM_PLAN}

### Tests to add/update
{TESTS_FROM_PLAN}

## Implementation rules
1. Read each file before editing to understand current state
2. Use Edit tool to modify existing files, Write tool for new files
3. Follow the exact signatures and patterns specified in the plan
4. If a file path from the plan doesn't exist, find the closest match via Glob/Grep
5. Add all tests listed in "Tests to add/update"

## CRITICAL — git rules
Allowed git invocations (read-only inspection only):
- `git status`
- `git diff`
- `git log` (read-only, e.g. to understand recent changes)

FORBIDDEN — do NOT run any of these under any circumstance:
- `git checkout`, `git branch`, `git switch`
- `git commit`, `git push`, `git pull`, `git fetch`
- `git rebase`, `git merge`, `git cherry-pick`
- `git reset`, `git restore`, `git stash`, `git add`

If the working tree appears stale, out-of-sync with master, or has unexpected files,
do NOT attempt to repair it with git commands. Instead return outcome=error with
reason "worktree out of sync — needs pipeline reset". The pipeline will rebuild the
worktree on the next attempt.

## Return
Return this JSON:
{
  "issue_number": {ISSUE_NUMBER},
  "branch_name": "juanocampovgr/{ISSUE_NUMBER}",
  "files_changed": ["path/to/file.kt", ...],
  "pr1_title": "{PR1_TITLE}",
  "error": null
}

On failure:
{
  "issue_number": {ISSUE_NUMBER},
  "branch_name": null,
  "files_changed": [],
  "pr1_title": null,
  "error": "<description>"
}
```

If dry_run == true: print what would be implemented, skip the subagent and all subsequent phases, EXIT.

Collect the result JSON. If subagent returned error: log and EXIT.

---

## PHASE 4 — Commit and push

```bash
git add -A
git commit -m "feat: AI implementation for #{ISSUE_NUMBER}"

BRANCH="juanocampovgr/{ISSUE_NUMBER}"

# Contamination guard: refuse to push if the branch has ballooned beyond the expected
# single-PR footprint.  This is the last line of defence — even if Phase 0b and the
# poller's setup_worktree both missed a contaminated branch, a tainted push is still
# prevented here.
LOCAL_AHEAD=$(git rev-list --count "origin/master..HEAD" 2>/dev/null || echo 0)
LOCAL_FILES=$(git diff --name-only "origin/master...HEAD" 2>/dev/null | wc -l | tr -d ' ')
if [ "$LOCAL_AHEAD" -gt 3 ] || [ "$LOCAL_FILES" -gt 50 ]; then
  echo "ERROR: refusing to push — branch has $LOCAL_AHEAD commits / $LOCAL_FILES files vs origin/master (contamination guard)"
  if [ -n "$PIPELINE_RESULT_PATH" ]; then
    mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
    printf '{"outcome":"error","error":"refusing to push: branch has %s commits / %s files vs origin/master (contamination guard)"}' \
      "$LOCAL_AHEAD" "$LOCAL_FILES" > "$PIPELINE_RESULT_PATH"
  fi
  exit 1
fi

# Guard: if the remote branch already existed, verify HEAD is a descendant before pushing.
# Fail fast instead of force-pushing (forbidden).
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  if ! git merge-base --is-ancestor "origin/$BRANCH" HEAD; then
    if [ -n "$PIPELINE_RESULT_PATH" ]; then
      mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
      printf '{"outcome":"error","error":"worktree HEAD is not a descendant of origin/%s — would require force-push (forbidden)"}' \
        "$BRANCH" > "$PIPELINE_RESULT_PATH"
    fi
    exit 1
  fi
fi
git push origin "$BRANCH"
```

On commit failure: print error and EXIT (do not post marker).
On push failure: print error and EXIT (do not post marker).

Build the compare URL:
```
compare_url = https://github.com/{ISSUE_REPO_FULL}/compare/master...juanocampovgr/{ISSUE_NUMBER}
```

---

## PHASE 4b — Write Pipeline Result

Before posting the `<!-- ai-impl:done -->` marker, write a structured result file so the
pipeline graph node can read the outcome without waiting for a GitHub comment:

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  # Replace placeholders with actual values
  cat > "$PIPELINE_RESULT_PATH" << RESULT_EOF
{
  "outcome": "done",
  "branch": "juanocampovgr/{ISSUE_NUMBER}",
  "files_changed": {FILES_CHANGED_JSON_ARRAY},
  "impl_summary": "Implementation complete for #{ISSUE_NUMBER}",
  "commit_shas": {COMMIT_SHAS_JSON_ARRAY}
}
RESULT_EOF
fi
```

On **any error path** (wherever you would post `<!-- ai-impl:error -->`), write first:

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"error","error":"%s"}' "{ERROR_DESCRIPTION}" > "$PIPELINE_RESULT_PATH"
fi
```

`$PIPELINE_RESULT_PATH` is set by the pipeline runner. If the variable is unset the skill runs
in standalone mode — the write is skipped and only the marker comment is used.

---

## PHASE 5 — Post implementation marker comment

Post to the issue with **exactly** this body (substitute values):

```markdown
**Implementation complete — branch pushed**

- **Branch:** `juanocampovgr/{ISSUE_NUMBER}`
- **Files changed:** {N} files
- **Compare vs master:** {compare_url}

<!-- ai-impl:done -->
```

```bash
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "{comment_body}"
```

On failure: log error. The poller will not advance the ticket (marker absent), which is correct.

---

## PHASE 6 — Summary

Print:
```
=== code-tickets Complete ===
  #{ISSUE_NUMBER} — "{TITLE}"
  Branch: juanocampovgr/{ISSUE_NUMBER}
  Files changed: N
  Pushed: yes
  Comment posted: yes/no
  (Poller will run quality checks next, then clean up worktree and advance ticket)
```

---

## EPILOGUE — Guaranteed result-file write

Before exiting for **any** reason (success, error, or unexpected branch), check that the
result file was written. If it was written correctly by an earlier phase this is a no-op:

```bash
if [ -n "$PIPELINE_RESULT_PATH" ] && [ ! -s "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"error","error":"skill exited without writing result"}' > "$PIPELINE_RESULT_PATH"
fi
```

This prevents the pipeline runner from polling for up to 1 hour on an unexpected exit.

---

## ERROR HANDLING

All unrecoverable failures follow the same pattern:
1. Run the **ERROR REPORTING PROCEDURE** (post `<!-- ai-impl:error -->` comment)
2. EXIT

The poller detects the error marker on the next poll and moves the ticket to **Error**.

| Scenario | Action |
|---|---|
| Ticket not found | EXIT with message (no comment — issue unknown) |
| No Implementation Plan comment | Run error procedure, EXIT |
| Subagent returns error | Run error procedure, EXIT |
| git commit fails | Run error procedure, EXIT |
| git push fails | Run error procedure, EXIT |
| Error comment post fails | Log and EXIT — poller staleness watchdog will flag it |
| gh 401 auth error | Print `gh auth refresh -s repo`. EXIT. |

