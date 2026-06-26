# quality-check

Dispatches GitHub Actions workflows (detekt, lint, unit tests) against `grindrllc/grindr-android` on the current branch and watches results. The workflows handle their own scope detection. The poller has already committed and pushed the implementation; this skill runs in the same worktree.

On success (all checks pass, with or without auto-fixes): post `<!-- ai-quality:done -->`.
On unrecoverable code failure: post `<!-- ai-quality:error -->` — graph routes to `escalate_error`.
On infrastructure failure (credentials, network, runner quota): write `outcome: "needs_human"` — graph routes to `needs_human` for human recovery.

**Never create a PR. Never change ticket status. Never run `git checkout` or `git branch`.**

---

## ERROR REPORTING PROCEDURE

Call this on every unrecoverable code failure. It writes the result file and posts the error comment — do not double-write or double-post after calling it.

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"error","error":"%s"}' "{ERROR_DESCRIPTION}" > "$PIPELINE_RESULT_PATH"
fi
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "⚠️ **Quality check failed — {CHECK_NAME}**

**Error:** {ERROR_DESCRIPTION}
**Run:** https://github.com/grindrllc/grindr-android/actions/runs/{FAILED_RUN_ID}
**Time:** $(date '+%Y-%m-%d %H:%M:%S')

The ticket has been moved to **Error** for human review. Fix the issue and move it back to **AI Implementation** to retry.

<!-- ai-quality:error -->"
```

## INFRASTRUCTURE FAILURE PROCEDURE

Call this when the failure is environmental (expired credentials, network timeout, runner quota exceeded, missing secret). It writes the result file and posts the paused comment — do not double-write or double-post after calling it.

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"needs_human","error":"%s"}' "{INFRA_ERROR_DESCRIPTION}" > "$PIPELINE_RESULT_PATH"
fi
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "⚠️ **Quality check paused — infrastructure issue**

**Error:** {INFRA_ERROR_DESCRIPTION}

**To retry:** Fix the underlying issue (e.g. refresh AWS CodeArtifact credentials), then use \`python pipeline_poller.py reset-thread {ISSUE_NUMBER}\` to restart.

<!-- ai-quality:needs-human -->"
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

---

## PHASE 1 — Find the ticket

Scan all three repos for the issue:
```bash
gh issue view {N} --repo {org}/{repo} --json number,title,labels,comments 2>/dev/null
```
Use the first repo that returns a result.

Build:
```
ticket = { issue_number, issue_repo_full (org/repo), repo_name, title }
```

If no ticket found: print "Ticket #{N} not found in any configured repo." and EXIT.

---

## PHASE 2 — Push branch

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git push -u origin HEAD
```

If push fails: run INFRASTRUCTURE FAILURE PROCEDURE and EXIT. Runners need the remote ref — there is no fallback.

---

## PHASE 2.5 — Verify branch has implementation changes

After pushing, confirm the branch actually differs from `origin/master`. A worktree created when the implementation branch was missing will be based on master and have zero changes — this must be caught before dispatching CI.

```bash
CHANGED_FILES=$(git diff --name-only origin/master | wc -l | tr -d ' ')
if [ "$CHANGED_FILES" -eq 0 ]; then
  ERROR_DESCRIPTION="Branch '$BRANCH' has no changes vs origin/master — the implementation push likely failed or the worktree was built from master. Reset ticket to AI Implementation to re-run the code agent."
  call ERROR REPORTING PROCEDURE with ERROR_DESCRIPTION and EXIT
fi
```

If CHANGED_FILES is 0: run ERROR REPORTING PROCEDURE (with stage "verify-branch-has-changes") and EXIT.

---

## PHASE 3 — Dispatch and watch workflows

**Step 1 — Dispatch all three workflows:**

All three workflows use a `branch` input (not `--ref`) to select the code branch to test.
Dispatch from the default branch (master) and pass the implementation branch as input:

```bash
gh workflow run detekt.yml       --repo grindrllc/grindr-android -f branch="$BRANCH"
gh workflow run android-lint.yml --repo grindrllc/grindr-android -f branch="$BRANCH"
gh workflow run unit-tests.yml   --repo grindrllc/grindr-android -f branch="$BRANCH"
```

If any dispatch fails with a network/quota error: run INFRASTRUCTURE FAILURE PROCEDURE and EXIT.
If any dispatch fails with a 401/403: run INFRASTRUCTURE FAILURE PROCEDURE (describing the auth error) and EXIT.

**Step 2 — Resolve run IDs:**

Wait for GitHub to register the runs, then fetch each run ID. Because the workflows are
dispatched from master (not `$BRANCH`), list by `--branch master` and take the most-recently-created
run for each workflow. Retry up to 3 times with increasing delays (5s, 10s, 15s) if any ID
comes back empty:

```bash
sleep 5
DETEKT_ID=$(gh run list --workflow detekt.yml       --branch master --repo grindrllc/grindr-android --limit 1 --json databaseId --jq '.[0].databaseId')
LINT_ID=$(gh run list   --workflow android-lint.yml --branch master --repo grindrllc/grindr-android --limit 1 --json databaseId --jq '.[0].databaseId')
TESTS_ID=$(gh run list  --workflow unit-tests.yml   --branch master --repo grindrllc/grindr-android --limit 1 --json databaseId --jq '.[0].databaseId')
```

If any ID is still empty after 3 retries: run INFRASTRUCTURE FAILURE PROCEDURE and EXIT.

**Step 3 — Watch all three in parallel:**
```bash
gh run watch "$DETEKT_ID" --exit-status --repo grindrllc/grindr-android &
PID_DETEKT=$!
gh run watch "$LINT_ID"   --exit-status --repo grindrllc/grindr-android &
PID_LINT=$!
gh run watch "$TESTS_ID"  --exit-status --repo grindrllc/grindr-android &
PID_TESTS=$!

wait $PID_DETEKT; RC_DETEKT=$?
wait $PID_LINT;   RC_LINT=$?
wait $PID_TESTS;  RC_TESTS=$?
```

**Step 4 — Attempt auto-fixes for each failing check (up to 2 attempts per check):**

For each check with a non-zero exit code, attempt to fix and re-run it. Track attempts per check independently — a failure in one check does not affect the retry budget for another.

For each failing check:
1. Download the failure log:
   ```bash
   gh run view {FAILED_ID} --log-failed --repo grindrllc/grindr-android
   ```
2. Read and understand the failure.
   - If it is a tool/environment issue (missing secret, runner quota exceeded, network timeout, missing dependency): run INFRASTRUCTURE FAILURE PROCEDURE and EXIT.
   - Otherwise: apply code fixes via the Edit tool.
3. Commit and push the fix:
   ```bash
   git add -A
   git commit -m "fix: quality-check auto-fix for #${ISSUE_NUMBER} (${CHECK_NAME})"
   git push
   ```
4. Re-dispatch only this workflow and resolve its new run ID (repeat Steps 1–2 for this single workflow).
5. Watch the re-dispatched run. If it passes, mark this check as resolved and stop retrying it.

If a check still fails after 2 fix attempts, record it as a permanent failure and move on to the next check. Do not EXIT — collect results for all checks first, then Phase 4 and Phase 5 handle the outcome.

---

## PHASE 4 — Write pipeline result

Write the result file before posting the GitHub comment, so the pipeline graph node can read the outcome without polling.

**If all checks passed:**
```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  cat > "$PIPELINE_RESULT_PATH" << 'RESULT_EOF'
{
  "outcome": "done",
  "checks": [
    {"check": "detekt",     "passed": {DETEKT_PASSED},  "fixes_applied": {DETEKT_FIXES}},
    {"check": "lint",       "passed": {LINT_PASSED},    "fixes_applied": {LINT_FIXES}},
    {"check": "unit_tests", "passed": {TESTS_PASSED},   "fixes_applied": {TESTS_FIXES}}
  ]
}
RESULT_EOF
fi
```

**If any check failed permanently:**
```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"error","error":"%s"}' "{FAILING_CHECK}: {ERROR_SUMMARY}" > "$PIPELINE_RESULT_PATH"
fi
```

---

## PHASE 5 — Post marker comment

**If all checks passed**, post to the issue:
```markdown
✅ **Quality checks passed**

- detekt ✅ — https://github.com/grindrllc/grindr-android/actions/runs/{DETEKT_ID}
- lint ✅ — https://github.com/grindrllc/grindr-android/actions/runs/{LINT_ID}
- unit tests ✅ — https://github.com/grindrllc/grindr-android/actions/runs/{TESTS_ID}
- **Auto-fixes applied:** yes/no

<!-- ai-quality:done -->
```

**If any check failed permanently**, run ERROR REPORTING PROCEDURE. It posts the error comment and writes the result file — do not post separately.

---

## PHASE 6 — Summary

Print:
```
=== quality-check Complete ===
  #{ISSUE_NUMBER} — "{TITLE}"
  Checks: detekt ✅/❌  lint ✅/❌  unit tests ✅/❌
  Fixes applied: yes/no
  (Poller will clean up worktree and advance ticket on next poll cycle)
```

---

## ERROR HANDLING

| Scenario | Action |
|---|---|
| Ticket not found | Print message and EXIT (no comment — issue unknown) |
| Push fails | Run INFRASTRUCTURE FAILURE PROCEDURE, EXIT |
| Workflow dispatch fails (network/quota/auth) | Run INFRASTRUCTURE FAILURE PROCEDURE, EXIT |
| Run ID empty after 3 retries | Run INFRASTRUCTURE FAILURE PROCEDURE, EXIT |
| Failure is environment/tooling issue | Run INFRASTRUCTURE FAILURE PROCEDURE, EXIT |
| detekt fails after 2 attempts | Record failure, continue to Phase 4/5 |
| lint fails after 2 attempts | Record failure, continue to Phase 4/5 |
| unit tests fail after 2 attempts | Record failure, continue to Phase 4/5 |
| Error comment post fails | Log and EXIT — poller staleness watchdog will flag it |

---

## EPILOGUE — Guaranteed result-file write

Before exiting for any reason, ensure the result file was written. This is a safety net for unexpected exits — the procedures above should have already written it.

```bash
if [ -n "$PIPELINE_RESULT_PATH" ] && [ ! -s "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"error","error":"skill exited without writing result"}' > "$PIPELINE_RESULT_PATH"
fi
```
