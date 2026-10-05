---
name: warp-scan
description: "Scan any repo, or one folder of it, for CURSOR_PLAN.md, schedule.json, a build map, or a Jira ticket export and build the Warp plan. Use as the first step on a project, or to rebuild after the spec changes."
---

# Warp scan

First step on every repo. Do not assume HumanifyOS paths.

## Run

```bash
python3 <plugin>/scripts/scan.py scan --root . --out .warp/beam.json
```

`scan.py ?` prints every subcommand and flag (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it.

## Scope to a folder

`/warp-scan` takes an optional folder, as a path or a name. Without it the whole repo is scanned, as before.

```bash
python3 <plugin>/scripts/scan.py scan --root . --folder HOS/spec --out .warp/beam.json
```

Only that folder is searched for plans, schedules, and exports. `.warp/` stays at the repo root, and `scan.json` and the beam record the folder.

| Argument | Result |
|---|---|
| An existing path under the repo, such as `HOS/spec` | Used as given. |
| A name or trailing path, such as `spec`, matching one folder | That folder. |
| A name matching several folders, only one with plan files | That folder, with a note listing the others. |
| A name matching several folders, none or more than one with plan files | Nothing is scanned. The script lists the matches (exit 3). Ask the user which one, then run again with the full path. |
| No such folder, or a path outside the repo | Error. Nothing is written. |
| A folder with no plan files | "no plan found in <folder>" (exit 2). |

With no argument, if plans sit in more than one folder, the scan prints which folders and still uses the richest plan. Suggest `/warp-scan <folder>` if that is not the one the user wants.

The scan writes `.warp/scan.json` (what it found) and `.warp/beam.json` (the plan). `runState` is `stopped`. Nothing dispatches until `/warp-start`.

## What it looks for

| File | How it is read |
|---|---|
| `WARP_PLAN.json` | Warp exchange format. Wins if present. |
| `schedule.json` with a `tickets` array | Deps, locks, gates, size. Preferred when there is no Warp plan. |
| `CURSOR_PLAN.md`, `CURSOR_PLAN_FAST.md` | Wave headings, ticket ids, `after` blockers, `Gates before` checks. |
| `*tickets*.json`, `jira/*.json` | Jira export: `blockedBy`, `blockedByTempIds`, or Blocks links. |
| Markdown table | Columns `id` and `deps` or `blockers`. |

A connected Jira plugin is the live source after the scan. If the repo has no plan file, ask the user to export the Jira filter to JSON or to connect the Atlassian MCP, then scan again. Do not invent tickets.

## Message to Slack or Teams

A successful scan writes `.warp/notify-post.json`, headed `Warp | <repo> / <project>` (the channel is shared across repos), with format, ticket count, gate count, estimate, and links to the plan used and the other plan, schedule, and export files found. Links use the `origin` remote and the current branch when it is GitHub, GitLab, or bitbucket.org, and work once the branch is pushed. Otherwise the relative path is used. Follow the `herald:` line the scan prints and post as `agents/herald.md` describes. `notify: quiet` posts nothing. With no channel set, or no connected server, the text goes to `.warp/outbox.md`. Tell the user and carry on. A failed scan (no plan found) posts nothing.

## Jira project

If the scan prints `jira: PROJECT`, or `jiraProject` in `.warp/config.yaml` is still empty, resolve the project before the external-id lookup:

1. Call `getAccessibleAtlassianResources` and `getVisibleJiraProjects` on `jiraMcp`.
2. Save the transcript and run `python3 <plugin>/scripts/jira_sync.py project --apply results.json`.
3. If several projects come back, run the JQL in `.warp/jira-project-probe.json` (`project --probe` reprints it). Save the searches and run `python3 <plugin>/scripts/jira_sync.py project --record probe.json`. Exactly one project with a hit is stored. If several projects match, or none do, the command lists them and the `project --set` lines. Do not guess. Do not pass a plan id to Jira.

A `jiraProject` that already has a value is left alone.

## Jira keys

The scan stores a key that is already on the plan, in a Jira export, or in `.warp/jira-map.json`. It prints `N tickets: K keyed, U need mapping`.

If `.warp/jira-resolve.json` lists tickets, and the Atlassian MCP is connected, resolve them before you tell the user the summary and before you post Herald. Order for each ticket: external-id field, then label `warp:<id>`, then a remote link. Stop at the first single match. Two matches are ambiguous: do not try the next step and do not pick one.

1. Call `getJiraProjectIssueTypesMetadata` once. Keep each field `name`, `id`, and `clauseNames`.
2. Call `searchJiraIssuesUsingJql` on `jiraMcp` for each `queries` entry whose `kind` is `external` or `label`. Prefer a field the catalog actually has. `jql` on the ticket is the configured `jiraExternalIdField` (a name or `cf[NNNNN]`).
3. For remote links, call `getJiraIssueRemoteIssueLinks` only when you already have a candidate issue. Record `{key, ids}`.
4. Save one transcript, not one file per ticket:

```json
{"fields":[{"name":"External ID","id":"customfield_10050","clauseNames":["cf[10050]"]}],"searches":[{"jql":"project = WAR AND cf[10050] = \"WV-01\"","issues":[{"key":"WAR-1","fields":{"customfield_10050":"WV-01"}}]},{"jql":"project = WAR AND labels = \"warp:WV-01\"","issues":[]}],"remoteLinks":[]}
```

A search that fails because the field does not exist is `{"jql":"...","error":"field not found","field":"External ID"}`. One issue and no echoed field value is still a hit, because the JQL was exact. Two issue keys are ambiguous.

5. Record each hit before any transition: `python3 <plugin>/scripts/jira_sync.py resolve --ticket <id> --key <WAR-1> --issue-id <issue id> --cloud-id <cloudId>`. That writes the beam and `.warp/jira-map.json` together. `resolve --apply results.json` does the same from a saved transcript. An exact single match is stored with its source (`external`, `label`, or `link`) and confidence. A summary match is not stored here. Nothing asks the user to confirm an external-id, label, or link hit. Do not pass the plan id to `transitionJiraIssue`.
6. If Jira is not connected, save `{"WV-01": []}` and run the same `--apply`. Do not pass the plan id to `transitionJiraIssue`.
7. If tickets stay unmapped and have summaries, run `/warp-jira-match`. `jira_match.py` writes the JQL. Search with `searchJiraIssuesUsingJql` (50 issues, at most 2 pages) and match locally. `--apply` stores one exact or 60-character prefix hit. A fuzzy proposal is not stored unless the user passed `--yes`. Two matches, or one issue claimed by two tickets, are listed and not stored. If `jiraWriteExternalId` is true, after `resolve --ticket` also run `jira_sync.py external-id` so `editJiraIssue` writes the plan id. Do not post a comment for that edit. If the key is false, do not edit the External ID field during scan.

Read the `N tickets: K keyed, U need mapping` line from that command to the user. Mention `/warp-jira-match` when a ticket is still unmapped and a summary candidate exists, and `/warp-jira-map` only to set one by hand. Do not post the scan Herald payload until this step has finished, so the message includes the resolved counts. `notify: quiet` posts nothing.

## After the scan

Tell the user the format, ticket count, gate count, the Jira line `N tickets: K keyed, U need mapping`, and that the plan is stopped. Offer `/warp-export` if they want another model to critique the order before `/warp-start`.
