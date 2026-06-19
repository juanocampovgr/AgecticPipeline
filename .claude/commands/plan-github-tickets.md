# plan-github-tickets

Automates implementation planning for GitHub Project board tickets. Reads TODO tickets
(identified by the "TODO" label), moves them to AI-Plan-InProgress while planning, posts
a full implementation plan as a comment, then moves them to Juan's Turn for review.

Status columns are tracked via GitHub Issue labels — no GitHub Projects API or special
OAuth scopes required (only `repo` scope needed).

## TOOL RESTRICTIONS

**Allowed**: Read, Grep, Glob, Bash (gh commands and git read-only), Agent
**NEVER**: Edit or Write source files. Never run gradle, xcodebuild, npm, make, or any build tool.
**git**: Read-only only — git log, git diff, git show. No commits or pushes.

---

## PHASE 0 — Bootstrap

### 0a: Read config

Read `~/.claude/skills/plan-github-tickets/config.md` and parse every KEY=VALUE line into a config map.

Resulting config object:
```
owner                    = GITHUB_OWNER
todo_col                 = TODO_COLUMN_NAME          (used as label name, e.g. "TODO")
ai_inprogress_col        = AI_PLAN_INPROGRESS_COLUMN_NAME  (label name, e.g. "AI-Plan-InProgress")
juans_turn_col           = JUANS_TURN_COLUMN_NAME    (label name, e.g. "Juan's Turn")
android_repo             = ANDROID_REPO             (local path — basename = GitHub repo name)
ios_repo                 = IOS_REPO                 (local path — basename = GitHub repo name)
backend_repo             = BACKEND_REPO             (local path — basename = GitHub repo name)
max_parallel             = MAX_PARALLEL_AGENTS (integer, default 3)
dry_run                  = DRY_RUN (boolean, default false)
pipeline_project_number  = PIPELINE_PROJECT_NUMBER  (integer, default 2)
status_ready_to_pick_up  = STATUS_READY_TO_PICK_UP  (option ID for "Ready To Pick Up")
```

Derive GitHub repo names from the basenames of the local paths:
- `android_github_repo` = basename(android_repo)   e.g. "grindr-android"
- `ios_github_repo`     = basename(ios_repo)        e.g. "grindr-3.0-ios"
- `backend_github_repo` = basename(backend_repo)    e.g. "backend"

Build a list of all repos to search:
```
repos_to_search = [
  { github_repo: android_github_repo,  local_path: android_repo  },
  { github_repo: ios_github_repo,      local_path: ios_repo      },
  { github_repo: backend_github_repo,  local_path: backend_repo  },
]
```

### 0b: Parse arguments

Parse $ARGUMENTS (space-separated). Override config values:

| Flag | Overrides |
|------|-----------|
| `--owner <name>`  | owner |
| `--dry-run`       | dry_run = true |
| `--ticket <N>`    | single_ticket = N (skip Phase 1 fetch, process only this issue) |
| `--repo <name>`   | single_repo = name (only search this GitHub repo, e.g. "grindr-android") |

---

## PHASE 1 — Fetch tickets

If `--ticket N` was passed:
- You still need to know which repo the issue belongs to. Scan all repos_to_search with:
  ```bash
  gh issue view {N} --repo {owner}/{repo.github_repo} --json number,title,labels 2>/dev/null
  ```
  Use the first repo that returns a result. Build a single-item list.

Otherwise, fetch from **two sources** and merge:

### Source A — label-based (TODO)

For each repo in repos_to_search (run in parallel, skip if `--repo` filter set):
```bash
gh issue list \
  --repo "{owner}/{repo.github_repo}" \
  --label "{todo_col}" \
  --state open \
  --json number,title,labels \
  --limit 100
```

### Source B — project board "Ready To Pick Up"

Query Project #`{pipeline_project_number}` for items in "Ready To Pick Up" status:
```bash
gh api graphql -f query='
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100) {
        nodes {
          content {
            __typename
            ... on Issue {
              number
              title
              repository { name }
              labels(first: 10) { nodes { name } }
            }
          }
          fieldValues(first: 10) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}' -f owner="{owner}" -F number={pipeline_project_number}
```

Filter to nodes where the Status field value == "Ready To Pick Up". Extract
`issue_number` (content.number), `issue_repo` (content.repository.name), `title`.

If `--repo <name>` was passed, filter Source B results to that repo as well.

### Merge and deduplicate

Combine Source A and Source B into a flat list, deduplicate by `issue_number` (Source B
takes precedence if same issue appears in both). Tag each ticket with its source for logging.

```
ticket = {
  issue_number: <integer>,
  issue_repo:   <github repo name, e.g. "grindr-android">,
  title:        <string>,
  source:       "label:TODO" | "board:Ready To Pick Up"
}
```

If 0 qualifying tickets: print "No tickets found in TODO or Ready To Pick Up. Nothing to do." and EXIT cleanly.

Print summary:
```
Found N ticket(s):
  #123 — grindr-android — "Feature title"  [label:TODO]
  #456 — grindr-android — "Bug title"      [board:Ready To Pick Up]
```

---

## PHASE 2 — Parallel planning

Read the full content of `.claude/references/plan-template.md`.
Store it as PLAN_TEMPLATE_CONTENT.

Divide the ticket list into batches of config.max_parallel.
For each batch: launch ALL agents in the batch in a SINGLE message (parallel tool calls).
Wait for the batch to complete before launching the next batch.

### Subagent prompt template

For each ticket, launch an Agent with `model: "opus"` and this exact prompt (replace all {PLACEHOLDERS}):

---
```
You are a planning subagent. Explore code and produce an implementation plan.
Do NOT modify any files. Do NOT post comments. Do NOT change labels or ticket status.
Return only the JSON object described in Step 4.

## Target ticket
- Issue number: {ISSUE_NUMBER}
- Repo: {OWNER}/{ISSUE_REPO}

## Repo paths
- Android: {ANDROID_REPO}
- iOS: {IOS_REPO}
- Backend: {BACKEND_REPO}

## Plan template
{PLAN_TEMPLATE_CONTENT}

---

## Step 1 — Fetch issue content and comment history

```bash
gh issue view {ISSUE_NUMBER} --repo {OWNER}/{ISSUE_REPO} --json title,body,labels,assignees,comments
```

Record: title, body (may be empty — proceed with title only), labels as a list of name strings, and comments.

### Detect re-plan mode

Scan the comments for any that contain **"Plan feedback from Juan:"** — this is the marker posted by the `/reset-ticket` skill.

If one or more such comments exist:
- This is a **RE-PLAN**. Set `is_replan = true`.
- Extract all feedback comment bodies (there may be multiple rounds). Store as `prior_feedback`.
- Also find the most recent comment that starts with `## Implementation Plan` — store as `prior_plan`. This is the plan that was rejected.
- Log: "Re-plan detected — found N feedback comment(s). Revising prior plan."

If no such comments exist:
- This is a **FIRST-TIME PLAN**. Set `is_replan = false`.

## Step 3 — Route by label

Check labels for: android, ios, backend (case-insensitive).
- Label "android" present → explore {ANDROID_REPO}
- Label "ios" present → explore {IOS_REPO}
- Label "backend" present → explore {BACKEND_REPO}
- Multiple labels → explore all matching repos
- No routing labels → skip code exploration, note in Open Questions

## Step 4 — Explore relevant code (≤5 actions per codebase)

**For Android ({ANDROID_REPO}):**
1. Glob: `{ANDROID_REPO}/feature/**` — look for modules whose name matches key words from the issue title
2. Grep: search `{ANDROID_REPO}` for class names or domain terms from the issue title (limit 20 results)
3. Read: the most relevant ViewModel, UseCase, or Repository file found
4. (optional) Read: corresponding module's build.gradle.kts for dependency context
5. (optional) Glob: `{ANDROID_REPO}/platform/**` for any platform module that looks relevant

**For iOS ({IOS_REPO}):**
1. Glob: `{IOS_REPO}/**/*.swift` filtered by issue title keywords
2. Grep: search for relevant protocol, ViewModel, or DataProvider type names
3. Read: the most relevant file found
4. (optional) Glob additional Swift files

**For Backend ({BACKEND_REPO}):**
1. Glob: `{BACKEND_REPO}/**` for handlers, controllers, or services matching keywords
2. Grep: search for relevant endpoint paths or domain type names
3. Read: the most relevant file found

## Step 5 — Produce the plan

Fill in the plan template with FULL implementation detail:
- Exact file paths from exploration (not generic placeholders)
- Specific function/class signatures for new code
- Named test cases (not just "add tests")
- Complexity estimate with honest rationale

Set the current date as {DATE} in the plan header.

### If is_replan == true

You MUST address all points raised in `prior_feedback`. Add a **"### Revision Notes"** section
directly below the Summary block. In it:
- List each piece of feedback and explain specifically how this plan addresses it
- If a feedback point is not addressed, explain why (e.g. out of scope, not actionable)

Do NOT simply repeat the prior plan. The revision must differ from `prior_plan` in the areas
the feedback called out. If the prior plan had gaps in a specific file or layer, explore
deeper before writing the plan.

Return this exact JSON (plan_markdown as a single escaped string, with `<!-- ai-plan:done -->` appended at the very end on its own line):
{
  "issue_number": {ISSUE_NUMBER},
  "issue_repo": "{ISSUE_REPO}",
  "plan_markdown": "<full plan as markdown string>\n\n<!-- ai-plan:done -->",
  "complexity": "M",
  "error": null
}

On any unrecoverable failure:
{
  "issue_number": {ISSUE_NUMBER},
  "issue_repo": "{ISSUE_REPO}",
  "plan_markdown": null,
  "complexity": null,
  "error": "<description>"
}
```
---

Collect all agent results. For results with non-null error: log and skip post+move.

---

## PHASE 3 — Post comments and move tickets (sequential)

Process each successful result one at a time to avoid GitHub API rate limits.

### 3a: Validate done marker

Before posting, check that `plan_markdown` contains the string `<!-- ai-plan:done -->`.

If the marker is **missing**:
- Log: `"WARNING: plan_markdown for #{issue_number} is missing <!-- ai-plan:done --> — appending it"`
- Append `\n\n<!-- ai-plan:done -->` to `plan_markdown`

This guard prevents tickets from getting stuck when a subagent posts partial work as
separate comments and omits the terminal marker.

### 3b: Post plan comment

If dry_run == true:
  Print the plan markdown to terminal.
  Log: "[DRY RUN] Would post plan to #{issue_number} in {owner}/{repo}"
  Skip the gh command.

If dry_run == false:
```bash
gh issue comment {issue_number} \
  --repo {owner}/{issue_repo} \
  --body "{plan_markdown}"
```

On failure: log error and continue to next ticket. The poller will detect the missing marker and not advance the ticket.

---

## PHASE 4 — Summary

Print:
```
=== plan-github-tickets Complete ===

Plans posted (poller will advance to Ready to Review then Plan): N
Errors:                                                          N

Tickets:
  #123 — "Feature title" [M] → plan posted
  #456 — "Bug title"     [S] → plan posted

{If dry_run}: DRY RUN — no comments posted.
```

---

## ERROR HANDLING REFERENCE

| Scenario | Action |
|---|---|
| No repos reachable | Print fix instructions. EXIT. |
| Draft item (no linked issue) | Skip with logged warning. |
| Issue body empty | Plan from title only. Note in Open Questions. |
| gh 401 auth error | Print `gh auth refresh -s repo`. EXIT. |
| gh 429 rate limit | Wait 60s, retry once. Then EXIT. |
| Comment post fails | Log error. Continue. Poller will retry next cycle (marker absent). |
| Zero tickets found | Print "Nothing to do." EXIT cleanly. |
