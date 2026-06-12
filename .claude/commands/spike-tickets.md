Research a spike ticket and optionally create actionable follow-up tickets from the findings.

## Usage
`/spike-tickets --ticket <N> [--create-followups]`

## Arguments
- `--ticket <N>` — the GitHub issue number
- `--create-followups` — if present, parse research findings and create follow-up GitHub tickets

## Workflow

### Phase 1: Research (always runs)

1. **Parse args** — extract `--ticket N` and detect `--create-followups` flag in `$ARGUMENTS`

2. **Read the spike issue** to understand the research question:
   ```bash
   gh issue view $TICKET --json title,body,labels,comments
   ```

3. **Explore the relevant codebase** based on the spike topic:
   - Read existing code, architecture docs, configuration files
   - Search for relevant patterns, existing implementations, or prior art
   - Check for related issues or PRs

4. **Produce a comprehensive research document** as a GitHub issue comment:
   ```markdown
   ## Spike Research: <title>

   ### Summary
   <2-3 sentence executive summary>

   ### Findings
   <detailed findings with code examples, diagrams, or references>

   ### Recommendation
   <recommended approach or decision>

   ### Follow-up Tasks
   (Only if actionable next steps are clear)
   - **Task 1**: <title> — <brief description> [label: android|ios|backend]
   - **Task 2**: <title> — <brief description> [label: android|ios|backend]

   <!-- ai-impl:done -->
   ```

### Phase 2: Create follow-up tickets (only when --create-followups is set)

5. **Parse "Follow-up Tasks"** from the research comment — extract each task item

6. **For each follow-up task**, create a GitHub issue:
   ```bash
   gh issue create \
     --repo <repo> \
     --title "<task title>" \
     --body "## Background\nSpawned from spike #$TICKET\n\n## Description\n<task description>" \
     --label "<android|ios|backend>"
   ```

7. **Add created issues to the project board** at "Backlog" status:
   ```bash
   gh project item-add <project_number> --owner <owner> --url <issue_url>
   ```

8. **Post a summary comment** on the original spike issue:
   ```
   📋 **Follow-up tickets created**

   Created N tickets from spike findings:
   - #<number>: <title>
   - #<number>: <title>
   ...

   <!-- ai-followups:done -->
   ```

## Notes
- The research document is the primary deliverable — be thorough
- Follow-up tasks should be concrete and independently actionable
- Each follow-up task should map to a single repo (android/ios/backend)
- If no clear actionable tasks emerge from the research, skip the Follow-up Tasks section
