# quality-check

Runs quality checks scoped to only the modules changed by the implementation branch.
The poller has already committed and pushed the implementation; this skill runs in the same worktree.

On success (all checks pass, with or without auto-fixes): post `<!-- ai-quality:done -->`.
On unrecoverable failure: post `<!-- ai-quality:error -->` — the poller detects this and moves the ticket to **Error**.

**NEVER create a PR. NEVER change ticket status. NEVER run `git checkout` or `git branch`.**

---

## ERROR REPORTING PROCEDURE

Call this on **every** unrecoverable failure before EXIT:

```bash
gh issue comment {ISSUE_NUMBER} \
  --repo {ISSUE_REPO_FULL} \
  --body "⚠️ **Quality check failed — {CHECK_NAME}**

**Error:** {ERROR_DESCRIPTION}
**Time:** $(date '+%Y-%m-%d %H:%M:%S')

The ticket has been moved to **Error** for human review. Fix the issue and move it back to **AI Implementation** to retry.

<!-- ai-quality:error -->"
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

If `--dry-run`: print the detected modules and what checks would run, then EXIT.

---

## PHASE 3 — Scoped quality checks

Build per-check Gradle task lists from `affected_modules`:
- Detekt tasks:  `{module}:detekt` for each module
- Lint tasks:    `{module}:lint` for each module
- Test tasks:    `{module}:testDebugUnitTest` for each module

Launch **3 parallel subagents** — one per check type. Each gets up to **3 auto-fix attempts**.

### Subagent A — detekt
```bash
./gradlew {module1:detekt} {module2:detekt} ... 2>&1
```
Repeat up to 3 times:
1. If the command fails, read the error output and apply auto-fixes via Edit tool
2. Re-run the command
3. If it passes, stop and return success

After 3 failed attempts:
- Return `{ "check": "detekt", "passed": false, "fixes_applied": true, "error": "<summary>" }`

On success at any attempt:
- Return `{ "check": "detekt", "passed": true, "fixes_applied": true/false }`

### Subagent B — lint
```bash
./gradlew {module1:lint} {module2:lint} ... 2>&1
```
Same retry logic as detekt (up to 3 fix attempts).
Return `{ "check": "lint", "passed": true/false, "fixes_applied": true/false, "error": "<summary if failed>" }`.

### Subagent C — unit tests
```bash
./gradlew {module1:testDebugUnitTest} {module2:testDebugUnitTest} ... 2>&1
```
Repeat up to 3 times:
1. If tests fail, read the output and attempt to fix failing test code via Edit tool
2. Re-run the command
3. If it passes, stop and return success

After 3 failed attempts:
- Return `{ "check": "unit_tests", "passed": false, "fixes_applied": true, "error": "<summary>" }`

On success at any attempt:
- Return `{ "check": "unit_tests", "passed": true, "fixes_applied": true/false }`

---

**After collecting all three subagent results: ALWAYS continue to Phase 4 and Phase 5, regardless of whether any check passed or failed. Do NOT exit here. The marker must always be posted.**

---

## PHASE 4 — Commit and push fixes (if any)

Check if any subagent reported `fixes_applied: true`.

If fixes were applied:
```bash
git add -A
git commit -m "fix: quality check auto-fixes for #{ISSUE_NUMBER}"
git push origin juanocampovgr/{ISSUE_NUMBER}
```

On commit failure: run ERROR REPORTING PROCEDURE and EXIT.
On push failure: run ERROR REPORTING PROCEDURE and EXIT.

If no fixes were applied: skip this phase.

---

## PHASE 5 — Post marker comment

**If all checks passed:**

Post to the issue:
```markdown
**Quality checks passed**

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

## PHASE 6 — Summary

Print:
```
=== quality-check Complete ===
  #{ISSUE_NUMBER} — "{TITLE}"
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
| detekt fails after 3 attempts | Run error procedure (include detekt output), EXIT |
| lint fails after 3 attempts | Run error procedure (include lint output), EXIT |
| unit tests fail after 3 attempts | Run error procedure (include test failure summary), EXIT |
| git commit fails (fixes) | Run error procedure, EXIT |
| git push fails (fixes) | Run error procedure, EXIT |
| Error comment post fails | Log and EXIT — poller staleness watchdog will flag it |
| gh 401 auth error | Print `gh auth refresh -s repo`. EXIT. |
