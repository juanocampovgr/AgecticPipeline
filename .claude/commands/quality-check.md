# quality-check

Runs verification checks scoped to only the modules changed by the implementation branch.
The poller has already committed and pushed the implementation; this skill runs in the same worktree.

Two modes:
- `--mode runner` **(default)**: dispatch GitHub Actions workflows and watch results. Matches CI exactly; robust to local environment issues.
- `--mode local`: run `./gradlew` locally. Faster (~2–5 min) but requires a healthy local env (credentials, Gradle daemon, etc.).

On success (all checks pass, with or without auto-fixes): post `<!-- ai-quality:done -->`.
On unrecoverable code failure: post `<!-- ai-quality:error -->` — graph routes to `escalate_error`.
On infrastructure failure (credentials, network, Gradle daemon): write `outcome: "needs_human"` — graph routes to `needs_human` for human recovery.

**NEVER create a PR. NEVER change ticket status. NEVER run `git checkout` or `git branch`.**

---

## ERROR REPORTING PROCEDURE

Call this on **every** unrecoverable code failure before EXIT:

```bash
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "⚠️ **Quality check failed — {CHECK_NAME}**

**Error:** {ERROR_DESCRIPTION}
**Time:** $(date '+%Y-%m-%d %H:%M:%S')

The ticket has been moved to **Error** for human review. Fix the issue and move it back to **AI Implementation** to retry.

<!-- ai-quality:error -->"
```

## INFRASTRUCTURE FAILURE PROCEDURE

Call this when the failure is environmental (expired credentials, network timeout, Gradle daemon OOM, missing tool):

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"needs_human","error":"%s"}' "{INFRA_ERROR_DESCRIPTION}" > "$PIPELINE_RESULT_PATH"
fi
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "⚠️ **Quality check paused — infrastructure issue**

**Error:** {INFRA_ERROR_DESCRIPTION}

**To retry:** Fix the underlying issue (e.g. refresh AWS CodeArtifact credentials), then use \`python pipeline_poller.py reset-thread {ISSUE_NUMBER}\` to restart. Alternatively, add the \`quality-mode:runner\` label (or remove \`quality-mode:local\`) to switch to GitHub Actions runners.

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

Derive GitHub org/owner from git remotes:
- `android_github_org` = from `git -C {android_repo} remote get-url origin`
- Same for ios and backend

Parse `$ARGUMENTS`:
- `--ticket <N>` → required; identifies the GitHub issue
- `--mode local|runner` → optional; defaults to `runner`
- `--dry-run`    → print detected modules and planned checks, skip execution, post nothing

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

If no ticket found: print "Ticket #{N} not found in any configured repo." EXIT.

---

## PHASE 2 — Detect affected modules

**Step 1 — List changed files:**
```bash
git diff origin/master --name-only
```
If the output is empty (no changes), post `<!-- ai-quality:done -->` immediately and EXIT.

**Step 2 — Check if this is an Android/Gradle project:**
```bash
test -f ./gradlew && echo "gradle" || echo "no-gradle"
```
If `./gradlew` is not present: post `<!-- ai-quality:done -->` immediately and EXIT (non-Android repo).

**Step 3 — Map changed files to Gradle modules:**

For each changed file path, find the nearest ancestor directory that contains `build.gradle.kts` or `build.gradle`:

```bash
# Example: given "features/chat/src/main/java/com/example/Foo.kt"
# Check: features/chat/src/main/java → no build.gradle
# Check: features/chat/src/main     → no build.gradle
# Check: features/chat/src          → no build.gradle
# Check: features/chat              → has build.gradle.kts → module root
# Gradle notation: :features:chat   (replace / with :, prepend :)
```

Convert each module root path to Gradle notation:
- `features/chat` → `:features:chat`
- `app` → `:app`
- `libs/common` → `:libs:common`

Deduplicate the resulting list. Store as `affected_modules`.

If `affected_modules` is empty after processing: post `<!-- ai-quality:done -->` immediately and EXIT.

If `--dry-run`: print the detected modules, the mode, and what checks would run, then EXIT.

---

## PHASE 3 — Push branch (required before runner mode; safe for local too)

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git push -u origin HEAD
```

If push fails:
- For `--mode runner`: this is fatal — run INFRASTRUCTURE FAILURE PROCEDURE and EXIT (runners need the remote ref).
- For `--mode local`: log warning and continue (checks run locally on working tree).

---

## PHASE 4 — Verify

### Mode: `--mode local`

Build per-check Gradle task lists from `affected_modules`:
- Detekt tasks:  `{module}:detekt` for each module
- Lint tasks:    `{module}:lint` for each module
- Test tasks:    `{module}:testDebugUnitTest` for each module

Launch **3 parallel subagents** — one per check type. Each gets up to **3 auto-fix attempts**.

**Subagent A — detekt**
```bash
./gradlew {module1:detekt} {module2:detekt} ... 2>&1
```
Repeat up to 3 times:
1. If the command fails, read the error output and apply auto-fixes via Edit tool
2. Re-run the command
3. If it passes, stop and return success

After 3 failed attempts:
- If the error is a Gradle/toolchain infrastructure issue (OOM, credentials, missing plugin) → return infra failure signal
- Otherwise → return `{ "check": "detekt", "passed": false, "fixes_applied": true, "error": "<summary>" }`

On success:
- Return `{ "check": "detekt", "passed": true, "fixes_applied": true/false }`

**Subagent B — lint** — same retry logic as detekt (up to 3 fix attempts).

**Subagent C — unit tests** — same retry logic (up to 3 fix attempts).

If **any subagent signals an infrastructure failure**, run the INFRASTRUCTURE FAILURE PROCEDURE and EXIT.

---

### Mode: `--mode runner` (default)

**Step 1 — Dispatch workflows:**
```bash
gh workflow run detekt.yml       --ref "$BRANCH" --repo Grindr/grindr-android
gh workflow run android-lint.yml --ref "$BRANCH" --repo Grindr/grindr-android
gh workflow run unit-tests.yml   --ref "$BRANCH" --repo Grindr/grindr-android
```

If any dispatch fails with a non-auth error (network, quota): run INFRASTRUCTURE FAILURE PROCEDURE and EXIT.
If any dispatch fails with a 401/403: print `gh auth refresh -s repo`. EXIT.

**Step 2 — Resolve run IDs** (wait 5s for GitHub to register):
```bash
sleep 5
DETEKT_ID=$(gh run list --workflow detekt.yml       --branch "$BRANCH" --repo Grindr/grindr-android --limit 1 --json databaseId --jq '.[0].databaseId')
LINT_ID=$(gh run list   --workflow android-lint.yml --branch "$BRANCH" --repo Grindr/grindr-android --limit 1 --json databaseId --jq '.[0].databaseId')
TESTS_ID=$(gh run list  --workflow unit-tests.yml   --branch "$BRANCH" --repo Grindr/grindr-android --limit 1 --json databaseId --jq '.[0].databaseId')
```

**Step 3 — Watch all three in parallel:**
```bash
gh run watch "$DETEKT_ID" --exit-status --repo Grindr/grindr-android &
PID_DETEKT=$!
gh run watch "$LINT_ID"   --exit-status --repo Grindr/grindr-android &
PID_LINT=$!
gh run watch "$TESTS_ID"  --exit-status --repo Grindr/grindr-android &
PID_TESTS=$!

wait $PID_DETEKT; RC_DETEKT=$?
wait $PID_LINT;   RC_LINT=$?
wait $PID_TESTS;  RC_TESTS=$?
```

**Step 4 — Handle failures** (up to 2 fix attempts per failing check):

For each run that failed (non-zero exit):
1. Download the failure log:
   ```bash
   gh run view <FAILED_ID> --log-failed --repo Grindr/grindr-android
   ```
2. Read and understand the failure.
   - If the failure is a tool/environment issue (missing secret, runner quota exceeded, network timeout): run INFRASTRUCTURE FAILURE PROCEDURE and EXIT.
   - Otherwise: apply auto-fixes via Edit tool.
3. Commit and push the fix:
   ```bash
   git add -A
   git commit -m "fix: quality-check auto-fix for #{ISSUE_NUMBER} ({check_name})"
   git push
   ```
4. Re-dispatch only the failing workflow and resolve its new run ID (same as Steps 1–2, for that single workflow).
5. Watch the re-dispatched run. If it passes, mark that check as resolved.

If a check still fails after 2 fix attempts:
- Return `{ "check": "<name>", "passed": false, "fixes_applied": true, "error": "<summary>" }`

---

**After collecting all check results: ALWAYS continue to Phase 5 and Phase 6, regardless of pass/fail. Do NOT exit here. The marker must always be posted.**

---

## PHASE 5 — Commit and push fixes (local mode only)

If `--mode local` and any subagent reported `fixes_applied: true`:
```bash
git add -A
git commit -m "fix: quality-check auto-fixes for #{ISSUE_NUMBER}"
git push origin {BRANCH}
```

On commit failure: run ERROR REPORTING PROCEDURE and EXIT.
On push failure: run ERROR REPORTING PROCEDURE and EXIT.

If `--mode runner`: fixes are already committed and pushed during Phase 4 retry loops; skip this phase.

If no fixes were applied: skip this phase.

---

## PHASE 5b — Write Pipeline Result

Before posting the marker, write the structured result file so the pipeline graph node reads the
outcome without waiting for a GitHub comment:

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  cat > "$PIPELINE_RESULT_PATH" << RESULT_EOF
{
  "outcome": "done",
  "quality_mode": "{MODE}",
  "checks": [
    {"check": "detekt",     "passed": true, "fixes_applied": false},
    {"check": "lint",       "passed": true, "fixes_applied": false},
    {"check": "unit_tests", "passed": true, "fixes_applied": false}
  ],
  "modules": {AFFECTED_MODULES_JSON_ARRAY}
}
RESULT_EOF
fi
```

On any code check failure (before running the ERROR REPORTING PROCEDURE):
```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"error","error":"%s"}' "{ERROR_DESCRIPTION}" > "$PIPELINE_RESULT_PATH"
fi
```

On infrastructure failure: write `outcome: "needs_human"` via the INFRASTRUCTURE FAILURE PROCEDURE (already handled above — do not double-write).

---

## PHASE 6 — Post marker comment

**If all checks passed:**

Post to the issue:
```markdown
✅ **Quality checks passed**

- **Mode:** {local|runner}
- **Modules checked:** {module list}
- **Checks:** detekt ✅  lint ✅  unit tests ✅
- **Auto-fixes applied:** yes/no

<!-- ai-quality:done -->
```

**If any check failed:**

Run the ERROR REPORTING PROCEDURE with the failing check name and error summary.

```bash
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "{comment_body}"
```

---

## PHASE 7 — Summary

Print:
```
=== quality-check Complete ===
  #{ISSUE_NUMBER} — "{TITLE}"
  Mode: {local|runner}
  Modules checked: {module list}
  Checks: detekt ✅/❌  lint ✅/❌  unit tests ✅/❌
  Fixes applied: yes/no
  (Poller will clean up worktree and advance ticket on next poll cycle)
```

---

## ERROR HANDLING

| Scenario | Action |
|---|---|
| Ticket not found | EXIT with message (no comment — issue unknown) |
| `./gradlew` absent | Post done marker, EXIT |
| No changed files | Post done marker, EXIT |
| No modules detected | Post done marker, EXIT |
| Push fails (runner mode) | Run INFRASTRUCTURE FAILURE PROCEDURE, EXIT |
| Workflow dispatch fails (network/quota) | Run INFRASTRUCTURE FAILURE PROCEDURE, EXIT |
| Workflow dispatch fails (401/403) | Print `gh auth refresh -s repo`. EXIT. |
| AWS CodeArtifact 401 (local) | Run INFRASTRUCTURE FAILURE PROCEDURE, EXIT |
| Gradle daemon OOM (local) | Run INFRASTRUCTURE FAILURE PROCEDURE, EXIT |
| detekt fails after 3 attempts (local) / 2 attempts (runner) | Run ERROR REPORTING PROCEDURE, EXIT |
| lint fails after 3/2 attempts | Run ERROR REPORTING PROCEDURE, EXIT |
| unit tests fail after 3/2 attempts | Run ERROR REPORTING PROCEDURE, EXIT |
| git commit fails (fixes) | Run ERROR REPORTING PROCEDURE, EXIT |
| git push fails (local fixes) | Run ERROR REPORTING PROCEDURE, EXIT |
| Error comment post fails | Log and EXIT — poller staleness watchdog will flag it |
| gh 401 auth error | Print `gh auth refresh -s repo`. EXIT. |
