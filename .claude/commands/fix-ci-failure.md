Fix a failing CI check on a pull request. Fetches CI logs, classifies the failure, and if it's a small in-scope fix, implements it and pushes to the branch. Posts a summary comment with what was fixed and links to the commit.

## Usage
`/fix-ci-failure --ticket <N> --pr <PR_NUMBER>`

## Arguments
- `--ticket <N>` — the GitHub issue number
- `--pr <PR_NUMBER>` — the pull request number

## Workflow

1. **Parse args** — extract `--ticket N` and `--pr P` from `$ARGUMENTS`

2. **Fetch CI failure details**:
   ```bash
   gh run list --repo <repo> --branch <branch> --limit 5 --json databaseId,status,conclusion,name
   # Get the latest failed run
   gh run view <run_id> --log-failed
   ```

3. **Classify the failure** — determine if it's in scope for auto-fix:

   **In scope (proceed):**
   - Compilation error from code on this branch
   - Lint/formatting violation from code on this branch
   - Unit test failure in a test touched by this PR
   - Import error introduced by this PR

   **Out of scope (post comment + needs_human):**
   - Pre-existing failure (failing on main before this PR)
   - Infrastructure/flaky test (intermittent, unrelated to code)
   - Failure in code not touched by this PR
   - Complex logic bug requiring re-design

4. **If out of scope** — post comment explaining why fix was skipped, then post needs-human marker:
   ```
   ⚠️ **CI fix skipped**

   The failing check (`<check name>`) is out of scope for automatic fixing:
   <reason>

   Human review needed.

   <!-- ai-ci-fix:needs-human -->
   ```

5. **If in scope** — fix and push:
   - Navigate to the worktree (the branch is already checked out)
   - Implement the fix (edit the specific file(s) causing the failure)
   - Run the check locally to verify the fix:
     ```bash
     # e.g., for lint: ./gradlew detekt
     # for compilation: ./gradlew compileDebugKotlin
     ```
   - Commit and push:
     ```bash
     git add <changed files>
     git commit -m "fix: resolve CI failure — <brief description>"
     git push
     ```

6. **Post success comment** on the issue:
   ```
   🔧 **CI fix applied**

   **Fixed:** <brief description of what was wrong>
   **Commit:** <commit SHA with link>
   **Check:** <check name that was failing>

   CI has been re-triggered on the updated commit.

   <!-- ai-ci-fix:done -->
   ```

## Safety constraints
- Only edit files that were already modified by this PR (check `git diff origin/master --name-only`)
- Maximum 3 files changed per CI fix
- If the fix requires changing more than 3 files, treat as out-of-scope
- Do NOT change test assertions to make tests pass — fix the implementation instead
