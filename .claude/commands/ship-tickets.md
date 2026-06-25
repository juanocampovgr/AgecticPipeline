Ship a ticket's verified implementation branch by creating a draft PR. The branch is already verified (quality-check passed before human approval); this skill only creates the PR.

Post `<!-- ai-ship:done -->` on success or `<!-- ai-ship:error -->` on unrecoverable failure.

## Arguments
`/ship-tickets --ticket <N>`

## Steps

### 1. Parse args
Extract `--ticket N` from `$ARGUMENTS`.

### 2. Ensure branch is pushed
```bash
git push -u origin HEAD
```
Idempotent — quality-check already pushed the branch, but this is a cheap safety net.

On failure: write error result and post `<!-- ai-ship:error -->`, EXIT.

### 3. Fetch issue context
```bash
gh issue view <N> --repo <ISSUE_REPO_FULL> --json title,body,comments
```

Extract:
- `title` — the issue title
- `plan_comment` — the most recent comment whose body contains `## Implementation Plan`

### 4. Generate PR description
Invoke `/grindr-pr-description` with the issue number, title, body, and plan comment as context.

### 5. Write Pipeline Result (before creating PR)

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"done","pr_number":0,"pr_url":""}' > "$PIPELINE_RESULT_PATH"
fi
```

This is written before the PR creation so a mid-creation failure still unblocks the graph.

On error (wherever you would post `<!-- ai-ship:error -->`):
```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"error","error":"%s"}' "<ERROR>" > "$PIPELINE_RESULT_PATH"
fi
```

### 6. Create draft PR
```bash
gh pr create --draft --base main \
  --title "[Ticket-<N>] <friendly description>" \
  --body "<output from /grindr-pr-description>"
```

**Title format:** `[Ticket-<N>] <friendly description>` — derive a short, human-readable description from the issue title (not a verbatim copy; make it clear and concise).

On failure: overwrite result file with `{"outcome":"error","error":"..."}`, post `<!-- ai-ship:error -->`, EXIT.

### 7. Update Pipeline Result with PR details

```bash
if [ -n "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  cat > "$PIPELINE_RESULT_PATH" << RESULT_EOF
{
  "outcome": "done",
  "pr_number": <PR_NUMBER>,
  "pr_url": "<PR_URL>"
}
RESULT_EOF
fi
```

### 8. Post done marker
```bash
gh issue comment <N> --repo <ISSUE_REPO_FULL> --body "$(cat <<'EOF'
✅ **Ship complete** — PR created: <pr_url>

<!-- ai-ship:done -->
EOF
)"
```

---

## EPILOGUE — Guaranteed result-file write

Before exiting for **any** reason, check that the result file was written:

```bash
if [ -n "$PIPELINE_RESULT_PATH" ] && [ ! -s "$PIPELINE_RESULT_PATH" ]; then
  mkdir -p "$(dirname "$PIPELINE_RESULT_PATH")"
  printf '{"outcome":"error","error":"skill exited without writing result"}' > "$PIPELINE_RESULT_PATH"
fi
```
