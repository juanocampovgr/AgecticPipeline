Review your own implementation of a GitHub ticket before sending it for human review. Checks plan alignment, obvious bugs, test coverage, and unresolved TODOs. Then runs the appropriate code review skill. Posts a done or failed marker as a GitHub comment.

## Usage
`/self-review-ticket --ticket <N>`

## Arguments
- `--ticket <N>` — the GitHub issue number

## Workflow

1. **Parse args** — extract `--ticket N` from `$ARGUMENTS`

2. **Fetch the plan** — get the issue's implementation plan comment from GitHub:
   ```bash
   gh issue view $TICKET --json comments --jq '.comments[].body' | grep -A 9999 "## Implementation Plan" | head -100
   ```

3. **Fetch recent commits** on the current branch (compare to origin/master):
   ```bash
   git log origin/master..HEAD --oneline
   git diff origin/master..HEAD --stat
   ```

4. **Self-review checklist** — read changed files and check each item:
   - [ ] All plan requirements implemented (cross-reference plan vs diff)
   - [ ] No obvious logic bugs or unhandled edge cases
   - [ ] Tests added or updated for the changes
   - [ ] No unresolved TODOs introduced by this PR
   - [ ] Code follows existing patterns in the repo

5. **Run code review skill** — invoke the appropriate skill based on repo:
   - Android repo → `/grindr-code-review`
   - All other repos → `/code-review`

6. **Determine outcome**:
   - If all checklist items pass AND code review has no blocking issues → **passed**
   - If any critical item fails → **failed** (will trigger a re-implementation)

7. **Post result comment** on the GitHub issue:

   On pass:
   ```
   ✅ **Self-review passed**

   All plan requirements implemented. No blocking issues found.

   <!-- ai-self-review:done -->
   ```

   On fail:
   ```
   ❌ **Self-review failed — re-implementing**

   Issues found:
   - <bullet list of problems>

   <!-- ai-self-review:failed -->
   ```

## Notes
- Be honest and critical. The goal is to catch real bugs before human review.
- Only mark as failed if there are genuine blockers; style nits alone don't fail.
- Do NOT push any code changes — this is a read-only review.
