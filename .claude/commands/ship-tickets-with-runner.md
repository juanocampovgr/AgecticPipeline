Ship a ticket's implementation branch using GitHub Actions runners for verification, then commit, push, and open a draft PR. Post `<!-- ai-ship:done -->` on success or `<!-- ai-ship:error -->` on unrecoverable failure.

## Arguments
`/ship-tickets-with-runner --ticket <N>`

## Steps

### 1. Parse args
Extract `--ticket N` from `$ARGUMENTS`.

### 2. Get current branch and push it
```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git push -u origin HEAD
```

The branch must exist on the remote before dispatching workflows.

### 3. Dispatch the three verification workflows against the branch
```bash
gh workflow run detekt.yml       --ref "$BRANCH" --repo Grindr/grindr-android
gh workflow run android-lint.yml --ref "$BRANCH" --repo Grindr/grindr-android
gh workflow run unit-tests.yml   --ref "$BRANCH" --repo Grindr/grindr-android
```

### 4. Resolve run IDs
`gh workflow run` does not return the run ID. Wait 5 seconds for GitHub to register the runs, then fetch the most recent run ID for each workflow on this branch:

```bash
sleep 5
DETEKT_ID=$(gh run list --workflow detekt.yml       --branch "$BRANCH" --repo Grindr/grindr-android --limit 1 --json databaseId --jq '.[0].databaseId')
LINT_ID=$(gh run list   --workflow android-lint.yml --branch "$BRANCH" --repo Grindr/grindr-android --limit 1 --json databaseId --jq '.[0].databaseId')
TESTS_ID=$(gh run list  --workflow unit-tests.yml   --branch "$BRANCH" --repo Grindr/grindr-android --limit 1 --json databaseId --jq '.[0].databaseId')
```

### 5. Watch all three runs in parallel
Spawn three background watchers and collect their exit codes:

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

### 6. Handle failures
For each run that failed (non-zero exit), perform up to **2 fix attempts**:

1. Download the failure log:
   ```bash
   gh run view <FAILED_ID> --log-failed --repo Grindr/grindr-android
   ```
2. Read and understand the failure. Edit the relevant files to fix the issue.
3. Commit the fix:
   ```bash
   git add -A
   git commit -m "Fix: address <check-name> failure"
   git push
   ```
4. Re-dispatch only the failing workflow and resolve its new run ID (same as steps 3–4 above, but for that single workflow).
5. Watch the re-dispatched run. If it passes, mark that check as resolved and continue.

If a check still fails after 2 fix attempts, post an error comment and stop:
```
❌ **Ship blocked — runner verification failed**

`<workflow-name>` failed after 2 fix attempts.

Run: https://github.com/Grindr/grindr-android/actions/runs/<RUN_ID>

<!-- ai-ship:error -->
```

### 7. Create draft PR
Fetch the issue title and body:
```bash
gh issue view <N> --repo Grindr/grindr-android --json title,body
```

Invoke `/grindr-pr-description` to generate the PR description. Pass the issue number, title, body, and plan comment as context.

Create the draft PR:
```bash
gh pr create --draft --base main \
  --repo Grindr/grindr-android \
  --title "[Ticket-<N>] <friendly description>" \
  --body "<output from /grindr-pr-description>"
```

### 8. Post done marker on issue #N
```bash
gh issue comment <N> --repo Grindr/grindr-android --body "$(cat <<'EOF'
✅ **Ship complete** — PR created: <pr_url>

<!-- ai-ship:done -->
EOF
)"
```
