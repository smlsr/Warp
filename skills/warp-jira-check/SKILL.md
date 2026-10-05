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

Add `--id <id>` to look at one ticket. `verify` with no flags only prints. It does not call Jira, and a `needs mapping` line does not mean the external-id search already ran. For each ticket read `status: keyed` or `status: unmapped`, `source`, and when unmapped the `reason` and the single `fix` command. When every ticket is unmapped, read `why nothing linked`. `catchup` prints the moves still owed and writes `.warp/jira-todo.json` for tickets that have a confirmed key. It does not call Jira. A missing or invalid key is refused: the message goes to `.warp/outbox.md` and Herald, and `.warp/jira-todo.json` does not ask for a transition.

Read the report to the user, including `why nothing linked` when it is present. Also show the beam status, `startedAt`, `qaReadyAt`, `doneAt`, recorded comment ids, what should have happened, and what is missing. If the first line is `jiraProject not set: Jira moves are disabled until you set it (candidates: ...)`, Jira moves stay off until the user picks one: `python3 <plugin>/scripts/jira_sync.py project --set WAR`. `project --list` prints the candidates and writes nothing. `jiraMcp` in the report (default `atlassian`) must be the Atlassian server name Cursor shows. This script cannot see whether that server is connected.

The plan id is not the Jira issue key. Scan and the first claim store `WAR-1` only when the agent runs the search they print and then `resolve --apply`. `verify` itself does not do that search. Never pass the plan id to `transitionJiraIssue`.

If any ticket is `status: unmapped`, link it before catch-up. Do this even when the user only asked what the check does:

1. `python3 <plugin>/scripts/jira_sync.py verify --link --beam .warp/beam.json`. This copies a key already in `.warp/jira-map.json` or `jiraKeyMap`. It writes `.warp/jira-resolve.json` and does not call Jira. `--dry-run` writes nothing.
2. If tickets are still unmapped and the Atlassian server named `jiraMcp` is connected, run that file's queries. Call `getJiraProjectIssueTypesMetadata` once, then `searchJiraIssuesUsingJql` for each external-id field (`jql`, then the other `queries` whose kind is `external`), then `labels = "warp:<id>"`, then `getJiraIssueRemoteIssueLinks` for a candidate issue. Stop when one issue matches. Two matches are ambiguous: do not pick one. Save `{"fields":[...],"searches":[{"jql":"...","issues":[{"key":"WAR-1"}]}],"remoteLinks":[{"key":"WAR-1","ids":["WV-01"]}]}`.
3. `python3 <plugin>/scripts/jira_sync.py verify --apply results.json --beam .warp/beam.json`. One exact match is stored with its source. A summary match is not stored. A manual key is not overwritten. If the server is not connected, say so and stop. Do not invent a key.

Then run `catchup`. After a key is mapped, `catchup` asks for the moves the current status still owes. An already-merged auto-merge ticket is asked for Done and a merged comment, not a move back to In Progress.

If the user wants the gap closed, do every action in `.warp/jira-todo.json`:

1. `getAccessibleAtlassianResources` on the server named `jiraMcp` (default `atlassian`) for `cloudId`, unless `jiraSite` is set.
2. `getJiraIssue` and `getTransitionsForJiraIssue` (or `listJiraIssueTransitions`).
3. `python3 <plugin>/scripts/jira_sync.py pick`, then `transitionJiraIssue` with that id.
4. `addOrEditJiraIssueComment` (or `addCommentToJiraIssue`) with `commentBody`.
5. In connected mode only, comment on the pull request (`add_issue_comment` or `gh pr comment`). Local mode skips pull-request comments.
6. `jira_sync.py record` and `record-comment` so the next check shows nothing missing.

If a tool is missing, `record --result unavailable`. That writes `.warp/outbox.md` and a Herald message. The ticket is not stopped.
