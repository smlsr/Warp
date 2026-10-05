---
name: warp-jira-check
description: "Explain, per ticket, which Jira transition and which Jira or pull-request comment should have happened, what the beam recorded, and what is missing. Use when a Jira status did not move."
---

# Jira check

```bash
python3 <plugin>/scripts/jira_sync.py verify --beam .warp/beam.json
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json
```

Add `--id <id>` to look at one ticket. `verify` only prints. `catchup` prints the moves still owed and writes `.warp/jira-todo.json`. `catchup --write` also stores a Jira key inferred from the id, summary, or branch. It does not call Jira.

Read the report to the user. For each ticket it shows `jiraKey`, the beam status, `startedAt`, `qaReadyAt`, `doneAt`, recorded comment ids, what should have happened, and what is missing.

A ticket merged before this upgrade often has no `jiraKey` and no `startedAt`. If the id, summary, or branch contains a key like `ABC-123`, catch-up can infer it. An already-merged auto-merge ticket is asked for Done and a merged comment, not a move back to In Progress.

If the user wants the gap closed, do every action in `.warp/jira-todo.json`:

1. `getAccessibleAtlassianResources` on the server named `jiraMcp` (default `atlassian`) for `cloudId`, unless `jiraSite` is set.
2. `getJiraIssue` and `getTransitionsForJiraIssue` (or `listJiraIssueTransitions`).
3. `python3 <plugin>/scripts/jira_sync.py pick`, then `transitionJiraIssue` with that id.
4. `addOrEditJiraIssueComment` (or `addCommentToJiraIssue`) with `commentBody`.
5. In connected mode only, comment on the pull request (`add_issue_comment` or `gh pr comment`). Local mode skips pull-request comments.
6. `jira_sync.py record` and `record-comment` so the next check shows nothing missing.

If a tool is missing, `record --result unavailable`. That writes `.warp/outbox.md` and a Herald message. The ticket is not stopped.
