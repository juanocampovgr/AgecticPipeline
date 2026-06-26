# fix-ci-failure

Fixes a failing CI check on a pull request. Fetches the CI failure logs, classifies the
failure, and if it's a small in-scope fix, implements it, commits, and pushes to the branch.
CI re-triggers automatically on push. Posts a summary comment on the issue with what was
fixed and a link to the commit.

On unrecoverable failure: post `<!-- ai-ci-fix:error -->` on the issue.
On out-of-scope failure: post `<!-- ai-ci-fix:needs-human -->` on the issue.

**Never create a PR. Never change ticket status. Never run `git checkout` or `git branch`.**

## Usage
`/fix-ci-failure --ticket <N> --pr <PR_NUMBER>`

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
  --body "⚠️ **CI fix failed — {STAGE}**

**Error:** {ERROR_DESCRIPTION}
**Time:** $(date '+%Y-%m-%d %H:%M:%S')

<!-- ai-ci-fix:error -->"
```

## OUT OF SCOPE PROCEDURE

Call this when the failure is not safe to auto-fix. It writes the result file and posts the
skip comment — do not double-write or double-post after calling it.

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"needs_human","reason":"%s"}' "{REASON}" > "$PIPELINE_RESULT_PATH"
fi
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "⚠️ **CI fix skipped**

The failing check (\`{CHECK_NAME}\`) is out of scope for automatic fixing:
{REASON}

Human review needed.

<!-- ai-ci-fix:needs-human -->"
```

---

## PHASE 0 — Bootstrap

Read `~/.claude/skills/plan-github-tickets/config.md` and parse every KEY=VALUE line:

```
owner        = GITHUB_OWNER
android_repo = ANDROID_REPO  (local path)
```

Parse `$ARGUMENTS`:
- `--ticket <N>` → required; identifies the GitHub issue for posting comments
- `--pr <P>`     → required; identifies the pull request

Resolve the PR's branch and full repo:
```bash
REPO="Grindr/grindr-android"
BRANCH=$(gh pr view {P} --repo "$REPO" --json headRefName --jq '.headRefName')
```

If the PR is not found or the branch is empty: print error and EXIT.

Build:
```
ticket = { issue_number: N, issue_repo_full: REPO, branch: BRANCH, pr_number: P }
```

---

## PHASE 1 — Fetch CI failure logs

Find the most recent failed run for this branch:
```bash
RUN_ID=$(gh run list --repo "$REPO" --branch "$BRANCH" --limit 5 \
  --json databaseId,conclusion \
  --jq '[.[] | select(.conclusion == "failure")] | first | .databaseId')
```

If no failed run found: print "No failed CI run found for branch $BRANCH." and EXIT.

Download the failure log:
```bash
gh run view "$RUN_ID" --log-failed --repo "$REPO"
```

Also capture the workflow name for use in comments:
```bash
CHECK_NAME=$(gh run view "$RUN_ID" --repo "$REPO" --json name --jq '.name')
```

If log fetch fails (network/auth error): call OUT OF SCOPE PROCEDURE with reason
"CI logs unavailable — infrastructure issue" and EXIT.

---

## PHASE 2 — Classify the failure

Determine if the failure is safe to auto-fix. When in doubt, treat as out of scope —
a failed fix attempt is worse than escalating to a human.

**In scope (proceed to Phase 3):**
- Compilation error from code on this branch
- Lint/formatting violation from code on this branch
- Unit test failure in a test touched by this PR
- Import error introduced by this PR

**Out of scope (call OUT OF SCOPE PROCEDURE and EXIT):**
- Pre-existing failure (failing on main before this PR)
- Infrastructure/flaky test (intermittent, unrelated to code changes)
- Failure in code not touched by this PR
- Complex logic bug requiring redesign
- Fix would require changing more than 3 files

---

## PHASE 3 — Apply fix

The worktree for this ticket lives at `~/.pipeline/worktrees/{ISSUE_NUMBER}`. Navigate there
and confirm the branch matches:
```bash
cd ~/.pipeline/worktrees/{ISSUE_NUMBER}
CURRENT=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT" != "$BRANCH" ]; then
  # Worktree is on the wrong branch — something is wrong with the pipeline state
  # Call ERROR REPORTING PROCEDURE: "worktree is on $CURRENT, expected $BRANCH"
fi
```

**Safety constraints — check before editing:**
```bash
git diff origin/master --name-only   # only edit files in this list
```
- Only edit files already modified by this PR
- Maximum 3 files changed — if more are needed, call OUT OF SCOPE PROCEDURE and EXIT
- Do not change test assertions to make tests pass — fix the implementation instead

Implement the fix using the Edit tool.

**Commit and push:**
```bash
git add -A
git commit -m "fix: resolve CI failure — {BRIEF_DESCRIPTION}"
git push
```

On commit failure: call ERROR REPORTING PROCEDURE (stage: "commit") and EXIT.
On push failure: call ERROR REPORTING PROCEDURE (stage: "push") and EXIT.

Capture the commit details:
```bash
COMMIT_SHA=$(git rev-parse HEAD)
COMMIT_SHA_SHORT=$(git rev-parse --short HEAD)
COMMIT_URL="https://github.com/$REPO/commit/$COMMIT_SHA"
```

CI re-triggers automatically on push — no manual dispatch needed.

---

## PHASE 4 — Write result and post success comment

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  echo '{"outcome":"done"}' > "$PIPELINE_RESULT_PATH"
fi
```

Post to the issue:
```bash
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "🔧 **CI fix applied**

- **Fixed:** {BRIEF_DESCRIPTION}
- **Check:** $CHECK_NAME
- **Commit:** [$COMMIT_SHA_SHORT]($COMMIT_URL)

CI has been re-triggered on the updated commit.

<!-- ai-ci-fix:done -->"
```

On post failure: log the error. The poller will not advance (marker absent), which is correct.

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
| PR not found / branch empty | Print message and EXIT |
| No failed CI run found | Print message and EXIT |
| CI logs unavailable (infra/auth) | Call OUT OF SCOPE PROCEDURE, EXIT |
| Failure is out of scope | Call OUT OF SCOPE PROCEDURE, EXIT |
| Fix requires > 3 files | Call OUT OF SCOPE PROCEDURE, EXIT |
| Worktree on wrong branch | Call ERROR REPORTING PROCEDURE, EXIT |
| git commit fails | Call ERROR REPORTING PROCEDURE, EXIT |
| git push fails | Call ERROR REPORTING PROCEDURE, EXIT |
| Error comment post fails | Log and EXIT — poller staleness watchdog will flag it |
