Ship a ticket's implementation branch: run verification scoped to changed Gradle modules only, then commit, push, and open a draft PR. Post `<!-- ai-ship:done -->` on success or `<!-- ai-ship:error -->` on unrecoverable failure.

## Arguments
`/ship-tickets --ticket <N>`

## Steps

### 1. Parse args
Extract `--ticket N` from `$ARGUMENTS`.

### 2. Find changed Gradle modules
```bash
git diff --name-only origin/main...HEAD
```
For each changed file, walk up the directory tree until you find a `build.gradle` or `build.gradle.kts`. The Gradle module path is the directory path relative to the repo root with `/` replaced by `:` (e.g. `feature/permissions` → `:feature:permissions`). Deduplicate the list and ignore root-level build files (the root module itself).

### 3. Run verification in parallel — scoped to changed modules only
For each module from step 2, run in parallel sub-agents:
- `./gradlew <module>:testDebugUnitTest --no-configuration-cache`
- `./gradlew <module>:lint`
- `./gradlew <module>:detekt` (skip silently if detekt is not configured for this module)

If any check fails:
- Attempt to auto-fix the failure (edit the relevant file, re-run the check). Max 2 fix attempts per check.
- If still failing after 2 attempts, post an error comment and stop:
  ```
  ❌ **Ship blocked — verification failed**

  `<module>:<task>` failed after 2 fix attempts:
  <failure summary>

  <!-- ai-ship:error -->
  ```
  Then exit.

### 4. Commit auto-fix changes (if any)
If verification passed and there are uncommitted changes:
```bash
git add -A
git commit -m "Fix: address lint/test failures before ship"
```

### 5. Push the branch
```bash
git push -u origin HEAD
```

### 6. Create draft PR
Fetch the issue title and body using `gh issue view <N> --json title,body`.

```bash
gh pr create --draft --base main \
  --title "[AI] <issue title>" \
  --body "$(cat <<'EOF'
## Summary
Closes #<N>

<brief summary of what was changed and why, derived from the issue body and plan comment>

## Test plan
- [ ] Verified with module-scoped unit tests
- [ ] Lint passed
- [ ] Detekt passed

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Title must be prefixed with `[AI] ` followed by the verbatim issue title.

### 7. Post done marker on issue #N
```bash
gh issue comment <N> --body "$(cat <<'EOF'
✅ **Ship complete** — PR created: <pr_url>

<!-- ai-ship:done -->
EOF
)"
```
