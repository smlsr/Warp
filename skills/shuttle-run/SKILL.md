---
name: shuttle-run
description: "Run one claimed ticket from Jira through an open pull request. Bugbot runs on that pull request, not in this VM. Use when a Shuttle is started with IMPLEMENT <id>. Does not merge."
---

# Shuttle run

You were started with `IMPLEMENT <id>` in your own checkout. Cloud is your own VM. Local is your own git worktree. Do not share a working copy. The orchestrator is the only merger. Do not implement from chat memory. Edit only this ticket's locks and `appendOnlyPaths` (add your own lines at the end). Branch from the head of `baseBranch` using Warp's `warp/<id>` name. Do not force a `feature/` prefix. If you need work that is not on the base branch, record a blocker on the plan. Do not build another ticket. Do not ask. Before the pull request, `orchestrator.py record --plan --result`. Then stop. Do not merge, do not push to the base branch, and do not start a second ticket.

## Load the repo before editing

1. Read `.cursor/rules/`, including every rule with `alwaysApply: true`. Read rules whose globs match the lock paths.
2. Read `AGENTS.md` and `CLAUDE.md` at the repo root and under the ticket lock path, if they exist.
3. Read the preamble the plan names.
4. Read `.warp/config.yaml` and your claim in `.warp/beam.json`. Status must be `claimed` or `recovering`, and `agent` must be you. Otherwise stop. If status is `recovering`, you are the replacement for a dead Shuttle. Keep the branch, the pull request, `jira.startedAt`, and the locks. Do not open a second pull request. After the heartbeat below, set status back to `recoveryPriorStatus` and continue that work.
5. Fetch the Jira issue if a key is set. Otherwise read the ticket in `CURSOR_PLAN.md`. Read every acceptance criterion.
6. Heartbeat. Your agent id is the ticket's `agent` field. A dead turn does not notify Warp. Cursor does not restart you. Plugin hooks do not run on cloud runners. There is no process table. Repeat this at each status change and at least every 5 minutes while you are working, always inside `staleMinutes` (default 15).

```bash
python3 <plugin>/scripts/beam.py heartbeat --beam .warp/beam.json --id <id> --agent <agent>
```

## Then

1. Jira status and the claim comment. The plan id is not the issue key. If the claim prints `jira: PROJECT`, `jiraProject` is still empty. Call `getAccessibleAtlassianResources` and `getVisibleJiraProjects`, save the transcript, and run `jira_sync.py project --apply` before any transition. If several projects are listed, do not pick one and do not call `transitionJiraIssue`. If the claim prints `jira: RESOLVE`, search Jira before any transition. For each ticket, query the external-id field first (`jql`, then the other `queries` whose kind is `external`), then `labels = "warp:<id>"`, then `getJiraIssueRemoteIssueLinks` if you have a candidate issue. Stop when one issue matches. Two matches are ambiguous: do not fall through and do not pick one. Before any transition, record the hit: `python3 <plugin>/scripts/jira_sync.py resolve --ticket <id> --key <WAR-1> --issue-id <issue id> --cloud-id <cloudId> --status "<current status>"`. That writes `jiraKey`, `jira.id`, and `jira.cloudId` on the beam and in `.warp/jira-map.json`. A plan id is refused and nothing is stored. Do not call `transitionJiraIssue` until that command prints `jira: MUST DO` with the stored key. A saved transcript still works: `resolve --apply results.json` with `{"searches":[{"jql":"...","issues":[{"key":"WAR-1"}]}]}`. If the tool errors or Jira is not connected, record `unavailable` and carry on. That does not stop the run. If the search ran and returned no issue, apply `{"WV-01": []}` so the miss is recorded. Do the transition and comment with `jiraKey` from the todo only: `getAccessibleAtlassianResources` for `cloudId` (or `jiraSite` if set) on the server named `jiraMcp`, `getJiraIssue`, `getTransitionsForJiraIssue`, `jira_sync.py pick`, `transitionJiraIssue`, then `addOrEditJiraIssueComment` with the comment body. Record with `jira_sync.py record` and `record-comment`. If `getJiraIssue` says the issue was not found, record `--result not-found` (the error text can say not found). If `--apply` says `needs mapping` or `ambiguous`, do not call Jira and do not pass the plan id to `transitionJiraIssue`. When no ticket in this run has `jira.startedAt`, either of those results releases the claim and stops the run: the script prints `jira: STOP` and one sentence, `Run stopped because Jira issues are not linked (<id> / <key>)`. Post only that Herald payload. Do not implement, do not open a pull request, and do not also say the claim still stands. A later miss, after one ticket was moved to In Progress, is a per-ticket alarm: the claim stands and the run continues. `jiraTransition: false` does not stop the run. `/warp-jira-map` is only for a leftover after that later miss. If `jira.startedAt` is already set, the transition is not asked for again. No connector, no matching transition, or another failed call is not an error: record `unavailable`, `no-transition`, or `failed`. That writes `.warp/outbox.md` and a Herald message. Continue.
2. Set `planning`. Plan the diff inside the lock paths only. An escape is `alarm` / `lock-escape`. Store every file or directory outside the lock with `--escaped`, one flag per path. Do not widen the lock yourself. If `alarmRepair.state` is `naming`, you are only naming those paths: run `alarm_repair.py paths --id <id> --path <path>` and stop. Do not edit. A repair whose state is `working` already has the widened locks. Finish inside them. Clear the alarm only when the diff stays inside those locks and you set a normal status. If you still need a path outside them, set `alarm` / `lock-escape` with the new `--escaped` paths. If you error out, run `alarm_repair.py next --returned <id> --error "<why>"` and leave the alarm set.
3. Set `coding`. Branch `warp/<id>-<jiraKey>` off `baseBranch` from `python3 <plugin>/scripts/provider.py resolve`. Use `config.model` (default `claude-sonnet-5-5-high`, Claude Sonnet 5.5 High; the slug must match the Cursor model picker).
4. Implement. Prove each acceptance criterion. Checkpoint spend with `scripts/beam.py spend`. If the Cursor run reports token usage, also record it. Pass the totals for this ticket, which replace the previous report:

```bash
python3 <plugin>/scripts/beam.py usage --beam .warp/beam.json --id <id> \
  --tokens-in N --tokens-out N --tokens-cached N --cost USD
```

The same flags work on `beam.py set`. If the run does not report usage, do not invent numbers. The completion report shows n/a.
5. If resolve says connected, push and open the pull request with the first method in `methods` (the named MCP server, or `gh` for GitHub). If it says local, or every method fails, do not push and do not error: `python3 <plugin>/scripts/provider.py note --id <id> --reason "<why>"` and hand the branch name back. Set `review` and, in connected mode, `--pr <url>`. That records the pull request as opened. The slot stays occupied until the check rollup is green. Do not run Bugbot in this VM. Bugbot runs on the pull request after the push. Do the Jira comment and the pull-request comment, then `record-comment`. Local mode comments on Jira only. Then stop.
6. If Reed returns fixes and attempts remain, set `fix` and push only in connected mode. Past `maxFixAttempts`, the ticket is `alarm`. Manual tickets use this same loop before `awaiting_approval`.

If status is already `coding`, `fix`, or `recovering` and the branch exists, continue that branch. Do not open a second pull request. Heartbeat after every `beam.py set`.

Every acceptance-criterion comment names the id and the command that passed.

If Cursor asks you to press Allow or Run on a Jira or Slack tool, stop editing permission files. Tell the user to run `/warp-allow-notify --check`. `/warp-init` writes the project allowlist. A tool name that is not in `scripts/mcp_tools.py` belongs in `notifyAllow`.

Before an MCP tool that this skill does not name, run `python3 <plugin>/scripts/mcp_allow.py --server SERVER --tool TOOL --root .`. If it prints `ask`, do not call the tool. A missing hook is not permission to call it.

When this Shuttle finishes, run `python3 <plugin>/scripts/session_note.py --type subagent-stop --beam .warp/beam.json`. That records the stop. The parent reconciles the ticket from the beam on the next tick.
