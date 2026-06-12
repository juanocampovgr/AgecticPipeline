Implement requested changes from unresolved PR review comments. For each comment successfully addressed, replies "Done" on that thread. Does not respond to comments it did not implement.

## Usage
`/respond-to-review --ticket <N> --pr <PR_NUMBER>`

## Arguments
- `--ticket <N>` — the GitHub issue number
- `--pr <PR_NUMBER>` — the pull request number

## Workflow

1. **Parse args** — extract `--ticket N` and `--pr P` from `$ARGUMENTS`

2. **Fetch all unresolved review threads**:
   ```bash
   gh api repos/<owner>/<repo>/pulls/<pr>/reviews --jq '.[].body'
   gh api repos/<owner>/<repo>/pulls/<pr>/comments --jq '.[] | {id, body, path, line, user}'
   ```

3. **Read and understand each comment** — for each unresolved thread:
   - Read the file at the relevant path/line
   - Understand what change is being requested
   - Determine if the request is:
     - **Implementable**: a concrete code change request → implement
     - **Discussion/question**: asking for clarification → skip (do not reply)
     - **Out of scope**: requires architectural change → skip

4. **Implement changes** — for each implementable comment:
   - Make the requested code change
   - Keep changes minimal and focused on what was requested
   - Don't refactor surrounding code unless explicitly requested

5. **Verify changes compile** (if applicable):
   ```bash
   # Android: ./gradlew compileDebugKotlin
   # iOS: xcodebuild -scheme <scheme> build
   ```

6. **Commit and push all changes as a single commit**:
   ```bash
   git add <changed files>
   git commit -m "review: address PR review comments"
   git push
   ```

7. **Reply "Done" on each addressed thread**:
   ```bash
   gh api repos/<owner>/<repo>/pulls/<pr>/comments/<id>/replies \
     --method POST --field body="Done"
   ```
   Only reply on threads where you made a change. Do NOT reply on threads you skipped.

8. **Post completion marker** on the issue:
   ```
   ✅ **Review comments addressed**

   Implemented changes for N comment(s). See commit: <SHA with link>

   <!-- ai-review-response:done -->
   ```

   If you could not implement any comments (all were discussion/out-of-scope):
   ```
   ℹ️ **Review comment response**

   No implementable code changes found in the review comments.
   The comments appear to be questions or discussion points that require human response.

   <!-- ai-review-response:needs-human -->
   ```

## Rules
- One commit for all changes — do not make separate commits per comment
- Only change lines/files referenced by review comments
- If a comment is ambiguous, skip it rather than guessing wrong
- Do NOT resolve the review threads yourself — the human reviewer does that
- Do NOT approve the PR or request re-review — just push the commit and reply
