---
name: shuttle-run
description: "Run one claimed ticket on its own VM, or in its own git worktree, through an open pull request. Bugbot runs on that pull request, not in this turn. Use when a Shuttle is started with the subagent prompt. Does not merge, and does not call Jira or Slack."
---

# Shuttle run

You were started as one subagent, for one step of one ticket. The prompt contains the claim: ticket id, Jira key, locks, acceptance, branch `warp/<id>-<jira>`, and your agent id on the line `agent: <agent>`. A `resume` prompt is this same Shuttle, not a second one.

## The contract

You do this step and return one line. You never start another agent of any kind. You never subscribe to anything, set a timer, loop, sleep, or wait on the pull request, CI, Bugbot, Slack, or an approval. The parent does all the waiting: it requests Bugbot, watches CI, and sends you a fix step if one is needed. If this conversation is woken later by anything that is not a new step from the parent (a CI result, a pull-request comment, a timer), run the reap check and return. Do not act on it.

## The reap check

Run it first, before any other step. The exact command is in your prompt.

```bash
python3 <plugin>/scripts/agents.py reap --beam <parent>/.warp/beam.json --id <agent> --ticket <id>
python3 <plugin>/scripts/agents.py reap --remote --id <agent> --ticket <id>
```

The first form is for a worktree. The second is for a dedicated VM: it reads the beam from origin's base branch and writes nothing. `reap: continue` means carry on. `reap: exit <reason>` means the run was paused or stopped, the ticket is merged or parked, or the parent replaced you. Stop now. Do not commit, push, or open anything more. Return `result: <id> stopped <reason>`. Do not start another step.

`ticket_state.py append` prints the same `reap:` line after every state you write. Read it every time. That is how a pause reaches you in the middle of a step.

When the prompt says "Run in your own cloud environment on a dedicated VM with its own clone and branch, not a git worktree on this machine", do that. Fetch the latest main, clone it, and create the branch yourself. First run `hostname` and `free -g`. Report that output with your branch name and working directory. Write `.warp/tickets/<id>/` on your branch and push it. The parent disk is not shared. Cloud MCP servers are the ones at cursor.com/agents, not the parent's session.

When the prompt names a `worktree:` path, work only inside that absolute path. Write `.warp/tickets/<id>/state.json` and `log.jsonl` at the parent absolute path (`--root` is that parent). Do not pass `--push`.

Either way: commit, push, and open or update the pull request. Never merge. Never touch the parent checkout. Never call Jira or Slack. Run `checkCommand` from config (often `make ci`) on this branch. Check every acceptance criterion and record each one as pass or fail on `pr.acResults`. Return one line: `result: <id> ok pr=<url> check=<green|red> ac=<name>:pass,<name>:fail hostname=<hostname> branch=<branch> cwd=<cwd> memory=<available-gib>` or `result: <id> failed <why> hostname=<hostname> cwd=<cwd>`. Opening the pull request is not done. The parent requests Bugbot, reads the findings, and may start this same Shuttle again on this branch for a fix. The orchestrator is the only merger. Do not implement from chat memory. Edit only this ticket's locks and `appendOnlyPaths` (add your own lines at the end). Do not force a `feature/` prefix. If you need work that is not on the base branch, record a blocker on the plan. Do not build another ticket. Do not ask. Before the pull request, `orchestrator.py record --plan --result` using the parent beam path when you are on a worktree. On a VM, commit that record into the ticket branch. Return the result line. Do not push to the base branch, and do not start a second ticket.

Optional `launch: agent` starts one new Agent from an IMPLEMENT prompt. Clone main so `.cursor` rules load, then create the branch. A beam file does not have to exist before that Agent starts. That mode is for a future Cloud Agents API. This plugin does not call that API. Jira and Slack calls below are for that mode only. The default subagent skips them. The parent does that work.

## Load the repo before editing

1. Read `.cursor/rules/`, including every rule with `alwaysApply: true`. Read rules whose globs match the lock paths.
2. Read `AGENTS.md` and `CLAUDE.md` at the repo root and under the ticket lock path, if they exist.
3. Read the preamble the plan names.
4. Read `.warp/config.yaml` and the claim in the IMPLEMENT prompt. Status must be `claimed` or `recovering`, and `agent` must be you. Otherwise stop. If status is `recovering`, you are the replacement for a dead Shuttle. Keep a branch that has work beyond the beam commit, the pull request, `jira.startedAt`, and the locks. Do not open a second pull request. A branch that is only the beam commit is not that work: start from latest main. After the heartbeat below, set status back to `recoveryPriorStatus` and continue that work.
5. Fetch the Jira issue if a key is set. Otherwise read the ticket in `CURSOR_PLAN.md`. Read every acceptance criterion.
6. Heartbeat. Write status into the parent checkout by absolute path. Do not push that directory. Repeat at each status change and at least every 5 minutes while you are working, always inside `staleMinutes` (default 15). This is not a timer: write it between steps of work you are already doing, and read the `reap:` line it prints. Do not commit `.warp/beam.json`. A failed turn returns `result: <id> failed <why>` so the parent can raise `worker-died`. The heartbeat watchdog still covers a parent that dies mid-turn.

```bash
python3 <plugin>/scripts/ticket_state.py append --id <id> --state started --agent <agent> --root <parent>
python3 <plugin>/scripts/ticket_state.py append --id <id> --state heartbeat --agent <agent> --root <parent>
```

## Then

1. Jira status and the claim comment. The plan id is not the issue key. If the claim prints `jira: PROJECT`, `jiraProject` is still empty. Call `getAccessibleAtlassianResources` and `getVisibleJiraProjects`, save the transcript, and run `jira_sync.py project --apply` before any transition. If several projects are listed, do not pick one and do not call `transitionJiraIssue`. If the claim prints `jira: RESOLVE`, search Jira before any transition. For each ticket, query the external-id field first (`jql`, then the other `queries` whose kind is `external`), then `labels = "warp:<id>"`, then `getJiraIssueRemoteIssueLinks` if you have a candidate issue. Stop when one issue matches. Two matches are ambiguous: do not fall through and do not pick one. Before any transition, record the hit: `python3 <plugin>/scripts/jira_sync.py resolve --ticket <id> --key <WAR-1> --issue-id <issue id> --cloud-id <cloudId> --status "<current status>"`. That writes `jiraKey`, `jira.id`, and `jira.cloudId` on the beam and in `.warp/jira-map.json`. A plan id is refused and nothing is stored. Do not call `transitionJiraIssue` until that command prints `jira: MUST DO` with the stored key. A saved transcript still works: `resolve --apply results.json` with `{"searches":[{"jql":"...","issues":[{"key":"WAR-1"}]}]}`. If the tool errors or Jira is not connected, record `unavailable` and carry on. That does not stop the run. If the search ran and returned no issue, apply `{"WV-01": []}` so the miss is recorded. Do the transition and comment with `jiraKey` from the todo only: `getAccessibleAtlassianResources` for `cloudId` (or `jiraSite` if set) on the server named `jiraMcp`, `getJiraIssue`, `getTransitionsForJiraIssue`, `jira_sync.py pick`, `transitionJiraIssue`, then `addOrEditJiraIssueComment` with the comment body. Record with `jira_sync.py record` and `record-comment`. If `getJiraIssue` says the issue was not found, record `--result not-found` (the error text can say not found). If `--apply` says `needs mapping` or `ambiguous`, do not call Jira and do not pass the plan id to `transitionJiraIssue`. When no ticket in this run has `jira.startedAt`, either of those results releases the claim and stops the run: the script prints `jira: STOP` and one sentence, `Run stopped because Jira issues are not linked (<id> / <key>)`. Post only that Herald payload. Do not implement, do not open a pull request, and do not also say the claim still stands. A later miss, after one ticket was moved to In Progress, is a per-ticket alarm: the claim stands and the run continues. `jiraTransition: false` does not stop the run. `/warp-jira-map` is only for a leftover after that later miss. If `jira.startedAt` is already set, the transition is not asked for again. No connector, no matching transition, or another failed call is not an error: record `unavailable`, `no-transition`, or `failed`. That writes `.warp/outbox.md` and a Herald message. Continue.
2. Set `planning` by appending it to your ticket directory and pushing that directory. Plan the diff inside the lock paths only. An escape is `alarm` / `lock-escape`. Name every file or directory outside the lock with `--escaped`, one flag per path, and push that too. Do not widen the lock yourself. Do not commit `.warp/beam.json`.

```bash
python3 <plugin>/scripts/ticket_state.py append --id <id> --state planning --root <parent>
python3 <plugin>/scripts/ticket_state.py append --id <id> --state lock-escape --escaped <path> --root <parent>
``` If `alarmRepair.state` is `naming`, you are only naming those paths: run `alarm_repair.py paths --id <id> --path <path>` and stop. Do not edit. A repair whose state is `working` already has the widened locks. Finish inside them. Clear the alarm only when the diff stays inside those locks and you set a normal status. If you still need a path outside them, set `alarm` / `lock-escape` with the new `--escaped` paths. If you error out, run `alarm_repair.py next --returned <id> --error "<why>"` and leave the alarm set.
3. Set `coding`. Branch `warp/<id>-<jiraKey>` off `baseBranch` from `python3 <plugin>/scripts/provider.py resolve`. Use `config.model` (default `claude-sonnet-5-5-high`, Claude Sonnet 5.5 High; the slug must match the Cursor model picker).
4. Implement. Prove each acceptance criterion. Checkpoint spend with `scripts/beam.py spend`. If the Cursor run reports token usage, also record it. Pass the totals for this ticket, which replace the previous report:

```bash
python3 <plugin>/scripts/beam.py usage --beam .warp/beam.json --id <id> \
  --tokens-in N --tokens-out N --tokens-cached N --cost USD
```

The same flags work on `beam.py set`. If the run does not report usage, do not invent numbers. The completion report shows n/a.
5. If resolve says connected, push and open or update the pull request with the first method in `methods` (the named MCP server, or `gh` for GitHub). If it says local, or every method fails, do not push and do not error: `python3 <plugin>/scripts/provider.py note --id <id> --reason "<why>"` and hand the branch name back. Set `review` and, in connected mode, `--pr <url>`. That records the pull request as opened. The slot stays occupied until the check rollup is green. Opening the pull request is not done. Run `checkCommand` and record `pr.check` as `green` or `red`, with the log on `pr.checkLog`. Record every acceptance criterion on `pr.acResults` as `pass` or `fail`. Do not run Bugbot in this VM. Bugbot runs on the pull request after the push. The parent requests it. Do the Jira comment and the pull-request comment, then `record-comment`. Local mode comments on Jira only. Return the result line with the pull request url, the check, and the acceptance results. A later `step=fix` run updates the same pull request, pushes, and returns again.
6. If Reed returns fixes and attempts remain, set `fix` and push only in connected mode. Past `maxFixAttempts`, the ticket is `alarm`. Manual tickets use this same loop before `awaiting_approval`.

If status is already `coding`, `fix`, or `recovering` and the branch exists, continue that branch. Do not open a second pull request. Heartbeat after every `beam.py set`.

Every acceptance-criterion comment names the id and the command that passed.

If Cursor asks you to press Allow or Run on a Jira or Slack tool, stop editing permission files. Tell the user to run `/warp-allow-notify --check`. `/warp-init` writes the project allowlist. A tool name that is not in `scripts/mcp_tools.py` belongs in `notifyAllow`.

Before an MCP tool that this skill does not name, run `python3 <plugin>/scripts/mcp_allow.py --server SERVER --tool TOOL --root .`. If it prints `ask`, do not call the tool. A missing hook is not permission to call it.

When this Shuttle finishes, run `python3 <plugin>/scripts/session_note.py --type subagent-stop --beam .warp/beam.json`. That records the stop. The parent reconciles the ticket from the beam on the next tick. Then return the result line and end. Do not wait for Bugbot, CI, a review, or a merge. Do not leave anything running: no background process, no timer, no subscription.
