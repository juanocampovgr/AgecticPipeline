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

**Never create a PR. Never change ticket status. Never run `git checkout` or `git branch`.**

---

## ERROR REPORTING PROCEDURE

Call this on every unrecoverable failure. It writes the result file and posts the error
comment — do not double-write or double-post after calling it.

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"error","error":"%s"}' "{ERROR_DESCRIPTION}" > "$PIPELINE_RESULT_PATH"
fi
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "⚠️ **Implementation failed — {STAGE}**

**Error:** {ERROR_DESCRIPTION}
**Time:** $(date '+%Y-%m-%d %H:%M:%S')

The ticket has been moved to **Error** for human review. Fix the issue and move it back to **AI Implementation** to retry.

<!-- ai-impl:error -->"
```

---

## PHASE 0 — Bootstrap

Read `~/.claude/skills/plan-github-tickets/config.md` and parse every KEY=VALUE line:

```
owner        = GITHUB_OWNER
android_repo = ANDROID_REPO  (local path)
ios_repo     = IOS_REPO      (local path)
backend_repo = BACKEND_REPO  (local path)
```

Parse `$ARGUMENTS`:
- `--ticket <N>` → required; identifies the GitHub issue
- `--dry-run`    → print what would be implemented, skip execution, post nothing
- `--repo <name>` → restrict to this repo (e.g. "grindr-android")

---

## PHASE 0b — Detect already-implemented remote branch

Before exploring any code, check whether the target remote branch already contains
a previous pipeline implementation. If it does, reuse it instead of re-implementing
from scratch — this prevents divergent-history push failures on pipeline restarts.

A branch is only reused when ALL these conditions hold:
- It is **1–3 commits** ahead of master (1 impl commit ± 1–2 auto-fix commits).
- It touches **≤ 50 files** (a larger footprint indicates contamination from a rebase replay or accumulated multi-retry runs).
- **No self-review-failure marker is newer than the impl-done marker** — if
  `<!-- ai-self-review:failed -->` was posted AFTER the last `<!-- ai-impl:done -->`,
  the current branch is known-bad; a fresh implementation is required to address
  the review feedback rather than re-run the same code through self-review again.

If any bound fails, fall through to a fresh implementation so the poller's
`setup_worktree` (which already deleted the over-large remote branch) can provide a clean base.

```bash
BRANCH="juanocampovgr/{ISSUE_NUMBER}"

# Does the remote branch exist?
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  # Fetch it locally so we can inspect it
  git fetch origin "$BRANCH" 2>/dev/null

  AHEAD=$(git rev-list --count "origin/master..origin/$BRANCH" 2>/dev/null || echo 0)
  FILES_TOUCHED=$(git diff --name-only "origin/master...origin/$BRANCH" 2>/dev/null | wc -l | tr -d ' ')

  # Check for a self-review failure that supersedes the last impl-done marker.
  # If the newest ai-self-review:failed marker is newer than the newest ai-impl:done,
  # the current branch has known blocking issues — do NOT reuse it.
  SELF_REVIEW_BLOCKS_REUSE="no"
  COMMENTS_JSON=$(gh issue view {ISSUE_NUMBER} --repo {ISSUE_REPO_FULL} --json comments 2>/dev/null)
  if [ -n "$COMMENTS_JSON" ]; then
    LATEST_IMPL_DONE=$(echo "$COMMENTS_JSON" | jq -r '[.comments[] | select(.body | contains("<!-- ai-impl:done -->"))] | last | .createdAt // ""')
    LATEST_SELF_FAIL=$(echo "$COMMENTS_JSON" | jq -r '[.comments[] | select(.body | contains("<!-- ai-self-review:failed -->"))] | last | .createdAt // ""')
    if [ -n "$LATEST_SELF_FAIL" ] && [ "$LATEST_SELF_FAIL" \> "$LATEST_IMPL_DONE" ]; then
      SELF_REVIEW_BLOCKS_REUSE="yes"
      echo "Latest self-review failure ($LATEST_SELF_FAIL) is newer than last impl-done ($LATEST_IMPL_DONE) — implementing fresh to address feedback."
    fi
  fi

  if [ "$AHEAD" -ge 1 ] && [ "$AHEAD" -le 3 ] && [ "$FILES_TOUCHED" -le 50 ] && [ "$SELF_REVIEW_BLOCKS_REUSE" = "no" ]; then
    echo "Remote branch '$BRANCH' is $AHEAD commit(s) ahead of master ($FILES_TOUCHED files) — reusing it."

    # Bring the worktree in sync with the remote branch tip
    git reset --hard "origin/$BRANCH"

    HEAD_SHA=$(git rev-parse HEAD)

    # Write result file so the pipeline can advance immediately
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

    gh issue comment {ISSUE_NUMBER} \
      --repo {ISSUE_REPO_FULL} \
      --body "**Implementation reused — existing branch** \`$BRANCH\` already contains a valid implementation commit (\`$HEAD_SHA\`). Advancing to quality check.

<!-- ai-impl:done -->"

    exit 0  # Skip all remaining phases
  else
    echo "Remote branch '$BRANCH' failed sanity check (ahead=$AHEAD files=$FILES_TOUCHED) — skipping reuse, will implement fresh."
  fi
fi
```

If the branch does not exist or fails the sanity check, continue to PHASE 1.

---

## PHASE 1 — Find the ticket

Scan all three repos for the issue:
```bash
gh issue view {N} --repo {org}/{repo} --json number,title,labels,comments 2>/dev/null
```
Use the first repo that returns a result.

Build:
```
ticket = { issue_number, issue_repo_full (org/repo), repo_name, title, comments }
```

If no ticket found: print "Ticket #{N} not found in any configured repo." and EXIT.

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

If no Implementation Plan comment found: call ERROR REPORTING PROCEDURE (stage: "extract plan") and EXIT.

### Self-review feedback (only when re-implementing)

Also scan `ticket.comments` for the most recent comment marked with
`<!-- ai-self-review:failed -->`. If found AND its `createdAt` is newer than the
most recent `<!-- ai-impl:done -->` marker, extract the failure text and store as
`self_review_feedback`. This feedback names specific bugs found in the previous
implementation attempt — the coding subagent MUST address every issue listed
before completing the new implementation.

If no such marker or it predates the last impl-done, `self_review_feedback` is empty.

---

## PHASE 3 — Implement (model: sonnet)

**Context:** The poller has already created a git worktree and opened this Terminal session in it.
You are already on branch `juanocampovgr/{ISSUE_NUMBER}`. Work in `pwd` (the current directory).

If `--dry-run`: print what would be implemented and EXIT without running the subagent or any subsequent phases.

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

### Self-review feedback from previous attempt (if any)
{SELF_REVIEW_FEEDBACK}

If the above feedback section is non-empty, this is a re-implementation attempt.
The previous code on this branch was rejected during self-review for the specific
issues listed. Address EVERY issue before completing — a re-implementation that
does not resolve the listed bugs will fail self-review again.

## Implementation rules
1. Read each file before editing to understand current state
2. Use Edit tool to modify existing files, Write tool for new files
3. Follow the exact signatures and patterns specified in the plan
4. If a file path from the plan doesn't exist, find the closest match via Glob/Grep
5. Add all tests listed in "Tests to add/update"

## Git rules
Only use git for read-only inspection:
- `git status`, `git diff`, `git log`

Do not stage, commit, push, branch, reset, or otherwise mutate git state. The parent
skill owns all git operations — if you touch git state, you risk corrupting the worktree
in a way the pipeline cannot recover from without a full reset.

If the working tree appears stale, out-of-sync with master, or has unexpected files,
do NOT attempt to repair it. Instead return outcome=error with reason
"worktree out of sync — needs pipeline reset".

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

Collect the result JSON. If the subagent returned a non-null `error`: call ERROR REPORTING
PROCEDURE (stage: "implementation", error: subagent's error message) and EXIT.

---

## PHASE 4 — Commit, push, and write pipeline result

**Step 1 — Commit:**
```bash
git add -A
git commit -m "feat: AI implementation for #{ISSUE_NUMBER}"
```

On commit failure: call ERROR REPORTING PROCEDURE (stage: "commit") and EXIT.

**Step 2 — Contamination guard:**

Refuse to push if the branch has grown beyond the expected single-PR footprint. This is the
last line of defence — even if Phase 0b and the poller's `setup_worktree` both missed a
contaminated branch, a tainted push is still prevented here.

```bash
LOCAL_AHEAD=$(git rev-list --count "origin/master..HEAD" 2>/dev/null || echo 0)
LOCAL_FILES=$(git diff --name-only "origin/master...HEAD" 2>/dev/null | wc -l | tr -d ' ')
if [ "$LOCAL_AHEAD" -gt 3 ] || [ "$LOCAL_FILES" -gt 50 ]; then
  echo "ERROR: refusing to push — branch has $LOCAL_AHEAD commits / $LOCAL_FILES files vs origin/master (contamination guard)"
  # Call ERROR REPORTING PROCEDURE with this message, then EXIT
fi
```

**Step 3 — Descendant guard:**

If the remote branch already existed, verify HEAD is a descendant before pushing.
Fail fast instead of force-pushing (which is forbidden).

```bash
BRANCH="juanocampovgr/{ISSUE_NUMBER}"
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  if ! git merge-base --is-ancestor "origin/$BRANCH" HEAD; then
    # Call ERROR REPORTING PROCEDURE: "worktree HEAD is not a descendant of origin/{BRANCH} — needs pipeline reset"
    # EXIT
  fi
fi
```

**Step 4 — Push:**
```bash
git push origin "$BRANCH"
```

On push failure: call ERROR REPORTING PROCEDURE (stage: "push") and EXIT.

**Step 5 — Write pipeline result:**

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  cat > "$PIPELINE_RESULT_PATH" << RESULT_EOF
{
  "outcome": "done",
  "branch": "juanocampovgr/{ISSUE_NUMBER}",
  "files_changed": {FILES_CHANGED_JSON_ARRAY},
  "impl_summary": "{PR1_TITLE}",
  "commit_shas": {COMMIT_SHAS_JSON_ARRAY}
}
RESULT_EOF
fi
```

Build the compare URL:
```
compare_url = https://github.com/{ISSUE_REPO_FULL}/compare/master...juanocampovgr/{ISSUE_NUMBER}
```

---

## PHASE 5 — Post implementation marker comment

```bash
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "**Implementation complete — branch pushed**

- **Branch:** \`juanocampovgr/{ISSUE_NUMBER}\`
- **Files changed:** {N} files
- **Compare vs master:** {compare_url}

<!-- ai-impl:done -->"
```

On failure: log the error. The poller will not advance the ticket (marker absent), which is correct.

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

Before exiting for any reason, ensure the result file was written. This is a safety net for
unexpected exits — the procedures above should have already written it.

```bash
if [ -n "$PIPELINE_RESULT_PATH" ] && [ ! -s "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"error","error":"skill exited without writing result"}' > "$PIPELINE_RESULT_PATH"
fi
```

---

## ERROR HANDLING

| Scenario | Action |
|---|---|
| Ticket not found | Print message and EXIT (no comment — issue unknown) |
| No Implementation Plan comment | Call ERROR REPORTING PROCEDURE, EXIT |
| Subagent returns error | Call ERROR REPORTING PROCEDURE, EXIT |
| git commit fails | Call ERROR REPORTING PROCEDURE, EXIT |
| Contamination guard triggered | Call ERROR REPORTING PROCEDURE, EXIT |
| Descendant guard triggered | Call ERROR REPORTING PROCEDURE, EXIT |
| git push fails | Call ERROR REPORTING PROCEDURE, EXIT |
| Error comment post fails | Log and EXIT — poller staleness watchdog will flag it |
| gh 401 auth error | Print `gh auth refresh -s repo`. EXIT. |
