---
name: shuttle-run
description: "Run one claimed ticket from Jira through PR and Bugbot. Use when a Shuttle is started with IMPLEMENT <id>. Does not merge."
---

# Shuttle run

You were started with `IMPLEMENT <id>` in this repo. Local and cloud are the same workspace: the clone. Do not implement from chat memory.

## Load the repo before editing

1. Read `.cursor/rules/`, including every rule with `alwaysApply: true`. Read rules whose globs match the lock paths.
2. Read `AGENTS.md` and `CLAUDE.md` at the repo root and under the ticket lock path, if they exist.
3. Read the preamble the plan names.
4. Read `.warp/config.yaml` and your claim in `.warp/beam.json`. Status must be `claimed` and `agent` must be you. Otherwise stop.
5. Fetch the Jira issue if a key is set. Otherwise read the ticket in `CURSOR_PLAN.md`. Read every acceptance criterion.

## Then

1. Jira status and the claim comment. The plan id is not the issue key. If the claim prints `jira: PROJECT`, `jiraProject` is still empty. Call `getAccessibleAtlassianResources` and `getVisibleJiraProjects`, save the transcript, and run `jira_sync.py project --apply` before any transition. If several projects are listed, do not pick one and do not call `transitionJiraIssue`. If the claim prints `jira: RESOLVE`, search Jira before any transition. For each ticket, query the external-id field first (`jql`, then the other `queries` whose kind is `external`), then `labels = "warp:<id>"`, then `getJiraIssueRemoteIssueLinks` if you have a candidate issue. Stop when one issue matches. Two matches are ambiguous: do not fall through and do not pick one. Save `{"fields":[...],"searches":[{"jql":"...","issues":[{"key":"WAR-1"}]}],"remoteLinks":[{"key":"WAR-1","ids":["WV-01"]}]}` and run `jira_sync.py resolve --apply results.json`. The older `{"WV-01": [{"key": "WAR-1", "externalId": "WV-01"}]}` shape still stores an external-id hit. If the tool errors or Jira is not connected, apply `{"WV-01": []}` so the miss is recorded. A single exact match stores the key, the source, and the confidence, and prints `jira: MUST DO`. Do those calls with `jiraKey` from the todo: `getAccessibleAtlassianResources` for `cloudId` (or `jiraSite` if set) on the server named `jiraMcp`, `getJiraIssue`, `getTransitionsForJiraIssue`, `jira_sync.py pick`, `transitionJiraIssue`, then `addOrEditJiraIssueComment` with the comment body. Record with `jira_sync.py record` and `record-comment`. If `--apply` says `needs mapping` or `ambiguous`, do not call Jira and do not pass the plan id to `transitionJiraIssue`. `/warp-jira-map` is only for that leftover. If `jira.startedAt` is already set, the transition is not asked for again. No connector, no matching transition, or a failed call is not an error: record `unavailable`, `no-transition`, or `failed`. That writes `.warp/outbox.md` and a Herald message. Continue.
2. Set `planning`. Plan the diff inside the lock paths only. An escape is `alarm` / `lock-escape`.
3. Set `coding`. Branch `warp/<id>-<jiraKey>` off `baseBranch` from `python3 <plugin>/scripts/provider.py resolve`. Use `config.model` (default `claude-sonnet-5-5-high`, Claude Sonnet 5.5 High; the slug must match the Cursor model picker).
4. Implement. Prove each acceptance criterion. Checkpoint spend with `scripts/beam.py spend`.
5. If resolve says connected, push and open the pull request with the first method in `methods` (the named MCP server, or `gh` for GitHub). If it says local, or every method fails, do not push and do not error: `python3 <plugin>/scripts/provider.py note --id <id> --reason "<why>"` and hand Reed the branch name. Set `review` and, in connected mode, `--pr <url>`. That prints the Jira comment and the pull-request comment. Do both, then `record-comment`. Local mode comments on Jira only.
6. If Reed returns fixes and attempts remain, set `fix` and push only in connected mode. Past `maxFixAttempts`, set `alarm`.

If status is already `coding` or `fix` and the branch exists, continue that branch. Do not open a second pull request.

Every acceptance-criterion comment names the id and the command that passed.
