---
name: warp-jira-check
description: "Explain, per ticket, which Jira transition and which Jira or pull-request comment should have happened, what the beam recorded, and what is missing. Use when a Jira status did not move."
---

# Jira check

```bash
python3 <plugin>/scripts/jira_sync.py verify --beam .warp/beam.json
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json
```

`jira_sync.py ?` prints every subcommand and flag (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it.

Add `--id <id>` to look at one ticket. `verify` only prints. It lists every ticket that `needs mapping`. `catchup` prints the moves still owed and writes `.warp/jira-todo.json` for tickets that have a confirmed key. It does not call Jira. A missing or invalid key is refused: the message goes to `.warp/outbox.md` and Herald, and `.warp/jira-todo.json` does not ask for a transition.

Read the report to the user. For each ticket it shows `jiraKey` or `unmapped`, the beam status, `startedAt`, `qaReadyAt`, `doneAt`, recorded comment ids, what should have happened, and what is missing.

The plan id is not the Jira issue key. Scan and claim store `WAR-1` when the plan, the map file, or exactly one Jira issue's external id says so. `WV-01` stays unmapped only when that search is missing or ambiguous. `/warp-jira-map` is for that leftover, not a required step. After a key is mapped, `catchup` asks for the moves the current status still owes. An already-merged auto-merge ticket is asked for Done and a merged comment, not a move back to In Progress. Never pass the plan id to `transitionJiraIssue`.

If the user wants the gap closed, do every action in `.warp/jira-todo.json`:

1. `getAccessibleAtlassianResources` on the server named `jiraMcp` (default `atlassian`) for `cloudId`, unless `jiraSite` is set.
2. `getJiraIssue` and `getTransitionsForJiraIssue` (or `listJiraIssueTransitions`).
3. `python3 <plugin>/scripts/jira_sync.py pick`, then `transitionJiraIssue` with that id.
4. `addOrEditJiraIssueComment` (or `addCommentToJiraIssue`) with `commentBody`.
5. In connected mode only, comment on the pull request (`add_issue_comment` or `gh pr comment`). Local mode skips pull-request comments.
6. `jira_sync.py record` and `record-comment` so the next check shows nothing missing.

If a tool is missing, `record --result unavailable`. That writes `.warp/outbox.md` and a Herald message. The ticket is not stopped.
