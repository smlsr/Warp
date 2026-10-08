# Runbook

## Orchestrator

The orchestrator is the only merger. It dispatches, merges, and tracks. It writes no ticket product code. Shuttles do not merge, do not push to the base branch, and do not ask mid-ticket. Reed reviews and does not merge.

Ready work is a ticket whose dependency and `after` lists are merged on the base branch, and whose full lock list does not overlap a lock an in-flight ticket holds. Overlap includes a parent folder. An open or green pull request is not merged. The dispatch loop runs at start and after every merge, failure, and freed slot. Starred and priority tickets start first, then the lowest rank, until `maxAgents` are in flight. A project may set 18. A slot stays occupied after the Shuttle opens the pull request, until the provider check rollup is green. Locks stay until that pull request is merged or the ticket is parked. Bugbot runs on the pull request after the push. `mergeQueue: true`, or a GitHub merge queue the provider reports, enqueues the pull request. A direct merge that branch protection rejects is not marked merged.

The merge queue is serial and automatic for sizes in `autoMergeSizes`: rebase onto the current base, run `checkCommand` on that head, merge, delete the branch, dispatch. When `checkCommand` is empty, the check is Bugbot and CI as configured. Several green auto-merge pull requests may be stacked in rank order and checked once. A red stack falls back to one pull request at a time. Sizes not in `autoMergeSizes` wait for `/warp-proceed` or `warp:proceed` before they enter the queue.

`appendOnlyPaths` is empty by default. A conflict in one of those files keeps both sides, every added line. Any other conflict is sent back to that ticket's Agent on the same branch. Do not hand-merge it. A red pull request, or red after rebase, goes back the same way with the failing output. It takes a slot at that ticket's rank. `orchestrator.py fail --output` records it. Past `maxFixAttempts` (default 5) the ticket parks: locks release, the branch and pull request stay open, and the blocker is written on the plan record. Tickets that do not depend on it keep going. Parked tickets and what they block are in `.warp/warp-complete.html`.

`make ci` is kept as the full check command. The space is part of the command. A red result is stored on `pr.check` and the ticket is sent back with the log (`send-back <id> fix` and a `start` line), the same way as `orchestrator.py fail`. Past `maxFixAttempts` (default 5) the ticket parks. The run does not sit idle. A Shuttle already on the ticket is not sent again. The gate stays while that check is red. It still clears when every member is merged or done and no current check is red. The name alone is not a red check.

A red base branch stops merging. Sub-agents keep working. Dispatch a fix ahead of every rank. Resume the queue when the base is green. On start or resume, rebuild from git (`merged`, `in-flight`, `pending`) and run the dispatch loop. Done means every ticket is merged or explicitly parked, and the base branch is green. `state_commit.py commit` commits the beam, journal, STATUS, and BOARD and strips tokens, cost, API keys, and webhook URLs. `.warp/config.yaml` stays gitignored.

Start one subagent per ticket in its own worktree, in parallel up to `maxAgents`. `checkout.py launch` fetches origin and runs `git worktree add <worktreeRoot>/<id> -b warp/<id>-<jira> origin/main`. The subagent works only inside that absolute path, commits, pushes, and opens the pull request. It never merges, never touches the parent checkout, and never calls Jira or Slack. It writes `.warp/tickets/<id>/state.json` and `log.jsonl` into the parent checkout by absolute path. `checkout.py verify` runs after it and raises `lock-escape` when the parent is dirty outside `.warp/` or the diff leaves the locks. Merge or park removes the worktree and prunes. A failed subagent result raises `worker-died`. The heartbeat watchdog still covers a parent that dies mid-turn. `/warp-resume` reuses the surviving worktrees and branches. Optional `launch: agent` prints an IMPLEMENT prompt for one new Agent. This plugin does not call a Cloud Agents API. Clone main so `.cursor` rules load, then create the branch. A beam file does not have to exist on the branch before that Agent starts. Never two workers in one worktree.

One agent is long-lived: the parent. Only the parent starts agents, and only the parent waits. A Shuttle does one step of one ticket and returns one line. The listener is one background subagent on the parent's checkout. `checkout.py launch` is the only source of a Shuttle prompt. It records the Shuttle in `.warp/agents.json` first. Its output is split by the line `prompt: pass every line below this one to the subagent, and nothing above it`. Every worker prompt carries `agent: <id>`, the worker contract, and the reap command (`agents.py reap --beam .warp/beam.json --id <agent> --ticket <id>`, or `--remote` on a dedicated VM). The parent ends every pass with the one wait, `orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>`. `<session>` is the id `scan.py start` and `scan.py resume` print as `parent: session <id>`. The whole rule is in [LIFECYCLE.md](LIFECYCLE.md).

## Pause overnight but keep the board

`/warp-pause` with a reason. Pause is the same teardown as `/warp-stop`. The one difference is that stop also writes the completion report and records `stoppedAt`. Every agent in `.warp/agents.json` is ended (`stop: <id>`) and marked halted, every Shuttle slot is freed, the listener's poll is closed (`halt: paused agents=<n>`), and the state is pushed to main. With `CURSOR_API_KEY` in the environment, every cloud agent in the registry, and every agent in this repo whose name carries this run's `[warp:<instance>]` tag, has its run cancelled and is archived. The agent running the command and this run's parent are skipped. Without the key the command prints `cleanup: link https://cursor.com/agents/<id>` for each one. In-flight Shuttles are not left running. Each one reads `reap: exit paused` at its next check and returns `result: <id> stopped paused`. Branches, worktrees, pull requests, locks, and `jira.startedAt` stay. The HTML board stays valid. In the morning run `/warp-start` or `/warp-resume`. They are the same command. It prints `run: was paused (<reason>). Continuing.` and the `unfinished:` line, reconciles, launches one new Shuttle with a new agent id on the same branch for each ticket that had one out (`fix: <id> resume after halt`), then ticks. Unfinished work starts before any new ticket. Nothing from before the pause is resumed. That is not a recovery, and no worker-died message is posted.

## Pipeline

`orchestrator.py supervise` and the dispatch tick are the pipeline. There are two caps. `maxAgents` (default 18) is how many agents run at once: every Shuttle, whatever its step (implement, fix, rebase, rerun, restart, repair), in a worktree on this machine or on its own VM. `maxInProgress` (default 20) caps started tickets that are not merged or parked. At that cap, nothing new is taken from the ready queue. A fix, a rebase, or a relaunch still starts, because its ticket is already open. It takes a `maxAgents` slot like any other agent. `/warp-status` prints `in progress N/maxInProgress`, or `holding new launches (N/maxInProgress), N fix workers running` when that cap is full, and `live shuttles N/cap` with the dead tickets waiting for a replacement. `N fix workers running` is a count. It is not a cap. A dead Shuttle does not hold a slot. Parked tickets do not count. Being over the cap does not park anyone. With no `--provider` file, supervise loads each open pull request from GitHub. A CONFLICTING or DIRTY pull request rebases in that pass, and route registration files keep both sides. Zero check runs older than `ciStartGraceMinutes` (default 5) start CI in that pass. `review: wait` is not printed for either case. A heartbeat, or a start that does not change the commit, the phase, or a check result, does not reset the stall clock. Phases are `implementing`, `pr-open`, `reviewing`, `fixing`, `ready`, `merging`, then `merged` or `parked`. Opening a pull request is not done. Launch a Shuttle when the pass prints `start <id> ... step=implement`, `step=fix`, or `step=restart`: run `checkout.py launch --id <id>` and start one subagent with the lines below the `prompt:` mark. `spawn: closed <id>` means no prompt was printed: start nothing. The first line of the pass is the listener: `listener: poll`, `listener: hold`, `listener: idle`, or `listener: none`. Request Bugbot when it prints `bugbot: request <id> <url>`. Post `herald:`, do `jira: MUST DO`, post `slack:`, and push the beam when a ticket merges. `/warp-start` and `/warp-resume` adopt an existing open pull request into `reviewing` or `fixing`. A `lock-escape` from `checkout.py verify` is repaired in that same pass. `/warp-status`, start, and resume list every ticket that is not merged, alarms and stalls first. A ticket with no progress inside its stall limit is stalled, and that supervise pass tries one fix: ask Bugbot again, rerun CI, rebase, restart a silent Shuttle, widen a lock-escape, clear a stale gate, start a base-branch fix, or return an approved pull request to the merge queue. Past `maxStallFixes` the ticket parks and Slack gets one alarm. The digest posts every `statusDigestMinutes`, and a new stall or alarm posts once. Every `repairSweepMinutes` (default 15) that pass also sweeps the whole run: what is broken, and what can be fixed that is not already queued or running. Broken covers tickets, pull requests, CI on main, locks, gates, the listener, and the beam, including red or stuck CI, a merge conflict, unresolved Bugbot findings, a stale or orphaned lock, a red base, a stuck gate, a listener with no holder, and a beam that is out of sync with main. The sweep reuses the fixer paths, starts work up to the slot cap, and logs findings and actions. `/warp-status` shows the last sweep. Slack is posted only when the sweep starts something new or escalates.

## A PR will not go green

`orchestrator.py supervise` starts a fix Shuttle for a Bugbot finding, red CI, or a failed acceptance criterion, then asks Bugbot to review again. After `maxFixAttempts` (default 5) the ticket is `parked`. Warp will not retry it. Reply `warp:retry <id>` after adding a note on the Jira issue. There is no `warp:hold` command. `/warp-pause` ends every agent and stops new claims.

Manual tickets (`autoMerge` false) use that loop before anyone is asked to review. `beam.py set --status awaiting_approval` is refused until `pr.bugbot` is `pass` and `pr.ci` is `green`, unless `bugbotManual` is false (Bugbot off for that path only) or `bugbotRequired` is false (Bugbot off for both). QA Ready is the Jira move for that status, not for pull-request open.

New commits after QA Ready (`--sha` while status is `awaiting_approval`) set status `bugbot_running`, clear Bugbot and CI, and comment "new commits, re-running Bugbot" on Jira and, in connected mode, the pull request. Jira stays at QA Ready. A later Bugbot failure goes to `fix` or `alarm` and does not move the Jira issue backwards. A later pass returns to `awaiting_approval` and comments again, without a second transition.

## Sizes not in autoMergeSizes waiting on you

Herald posts the pull request, or the local branch when `pushMerge` is false, and `warp:proceed <id>`. An approval on the provider (GitHub review or Bitbucket APPROVED) is enough. The chat command is the override, and it is the only signal in local-only mode besides the listener. Jira is at QA Ready while this waits. After the merge it moves to Done (`jiraDoneOnManualMerge`, default true).

The listener is a Subagent of the parent. Start one subagent per ticket in its own worktree, in parallel up to maxAgents. The channel reader is one `warp-listen` Subagent for the beam, not one per awaiting_approval ticket. It is not a separate Agent. It shares the parent's session and checkout. It is one background subagent. `subagentVm` does not apply. `orchestrator.py supervise` prints `listener: poll`, then the prompt (first line `LISTEN`), when no holder is live and the return stamp is in. The parent starts exactly one in the background and does not wait. Each cycle runs `inbound.py poll --lease` before any read, reads each channel once from the `since` mark, runs `inbound.py accept` and `inbound.py enqueue` for each `warp:` command (both take `--message-id`), posts the ack, then `inbound.py wait`, which sleeps in Python. A duplicate Slack ts prints `inbound: duplicate` and writes no second ack. `.warp/listener.json` gets `lastPollAt`, `reason`, `generation`, and `holder`. The parent applies queued commands with `inbound.py apply-pending` when the pending file changes (`pass: command`). `listener: hold reason=live` and `listener: hold reason=await-return` mean do not start a second. `listener: idle` means the run is paused, stopped, or finished. `listener: none` means no `slackChannel` or `teamsChannel` is set, so no listener is started at all. A stale heartbeat does not start another listener. The return stamp does. There is no restart cap: the next generation starts from that stamp. `/warp-start` and `/warp-resume` do not claim a listener. `inbound.py claim` and `inbound.py heartbeat` are for a listener someone starts by hand. A run does not use them. A running flag from the previous turn does not block the next tick. `/warp-pause` and `/warp-stop` close the poll and set `listener.state` to `stopped` through `inbound.py release`. The listener must not keep reading while paused or stopped. Cursor cannot start it from Slack. There is no webhook, and plugin hooks do not run on cloud runners. Close the window, open a new one, `/warp-start`. State comes from main plus ticket folders. Pause and stop do not start a listener.

`/warp-proceed WV-01` also accepts the Jira key (`WAR-1`) or a `#` number (`#01`). The listener runs `inbound.py`, which acks in the channel before `proceed.py`. The ack is `Received warp:proceed XV-01. Merging and moving Jira to Done.` A bad id acks and merges nothing else. If the ticket is not `awaiting_approval`, the ack says so and does not merge. When it accepts, merge in that same turn and run the post-merge MUST DO before you reply. The channel gets the ack first, then the merge sha and the Jira status.

Do not proceed a red PR. Reed will refuse.

A ticket that already merged without leaving QA Ready shows up on `/warp-jira-check` as `merged-but-not-done`. `catchup` prints the Done transition, the merged comment, and the exact command:

```bash
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json --id <id>
```

Record the result with `record --event done`. If Jira has no transition from QA Ready to Done, `pick --kind done` tries the name and then one done-category transition. `record --result no-transition` writes `.warp/outbox.md` and Herald posts Jira not updated. Set `jiraDoneOnManualMerge: false` only when QA should keep Done.

## Run finished or stopped

When nothing is queued or active, or you run `/warp-stop`, Warp writes `.warp/warp-complete.html` and Herald posts the totals and the path. Both run the same teardown as `/warp-pause` first: every agent in `.warp/agents.json` is ended and marked halted, and with `CURSOR_API_KEY` the run's cloud agents are cancelled and archived. The line is `halt: stopped agents=<n>` after `/warp-stop`, and `halt: done agents=<n>` once when every ticket is merged or parked. Then `parent-exit` prints `parent: exit` and the parent ends its turn. A stopped run starts nothing until `/warp-start` or `/warp-resume`, which are the same command. The file is local and gitignored. `reportOnComplete: false` skips that automatic write. While the run is still going, `/warp-report --partial` writes a snapshot and does not wait for the end.

## Gate red

Fix on the member branch (G0 is L-01, M-01, D-01). Dependents stay out of `ready()` until `beam.py gate --status green --evidence "..."`. A red critical-path gate is the one case to pause the program: later agents will only pile up lock-free work that cannot ship. A red gate stays red when a member check is red or the evidence names a red check. A stale red, with every member merged or done and no red check, is cleared on the same pass as a pending gate.

## Gate still pending

Nothing in flight, nothing awaiting approval, no alarms, and an empty ready set can still mean a pending or stale red gate whose members are already merged or done and no check is actually red. The same is true when every ticket on the beam is merged and a gate is still pending. The parent's repair pass (`alarmRepairMinutes`, default 15) recomputes those gates. The listener does not run it. A gate turns green when every member is merged or done, or when every ticket is, the evidence is `members merged:` and those ids, and the board is rewritten. Then that pass runs the tick. G1 with members P-002, P-003, P-004, and P-018 stores `members merged: P-002, P-003, P-004, P-018`. Herald posts `G1 pending cleared. Members merged. Tick ran.` and that pass runs the dispatch tick so work blocked only by the stale gate starts. Every tick recomputes before `ready()`, so a merge does not wait for the pass. The pass is the backstop when no tick is running. A member that is not merged, a ticket that is still alarmed or parked, or a check that is actually red leaves the gate as it is. A recovered ticket that is merged counts. A second pass does not dispatch again. A green gate stays green. Pause and stop do not recompute.

## First ticket is not linked to Jira

The first claimed ticket of a run is the claim made while no ticket has `jira.startedAt`. No earlier ticket was linked and moved to In Progress. If Jira says that issue was not found (`record --result not-found`), or the lookup cannot resolve a real issue key, Warp does not leave the claim standing. It sets the ticket back to `queued`, clears `agent`, `branch`, `jira.startedAt`, and `jira.previousStatus`, writes a session note, and stops the run the same way as `/warp-stop`. Beam, `.warp/STATUS.md`, and `.warp/BOARD.md` all show that ticket as queued and the run stopped. Nothing is implemented and no pull request is opened.

Herald posts one message:

```text
Run stopped because Jira issues are not linked (WV-01 / WAR-1). Fix jiraProject, /warp-jira-match, or /warp-jira-external-id, then /warp-resume.
```

It does not also post that the claim still stands. The reason on the beam is `tickets are not linked to Jira (WV-01 / WAR-1)`. Fix `jiraProject`, run `/warp-jira-match`, or run `/warp-jira-external-id`, then `/warp-resume`.

A later miss, after at least one ticket has `jira.startedAt`, stays a per-ticket alarm. The claim stands, Herald posts Jira not updated, and the run continues. `jiraTransition: false` does not stop the run. A missing transition or a disconnected Jira server is still that per-ticket note, not this stop, unless the result is not-found or the key cannot be resolved on the first ticket.

## Jira status did not move

`/warp-jira-check` runs `jira_sync.py verify`, then `catchup`. `verify` with no flags only prints. It does not call Jira. For each ticket: `status: keyed` or `status: unmapped`, `source`, beam status, `startedAt`, `qaReadyAt`, `doneAt`, comment ids, `jira.id`, and `lastAttempt` when a move failed. An unmapped ticket also has `reason` and one `fix` command. When every ticket is unmapped the report ends with `why nothing linked`.

Walk the first line that matches. Stop when the ticket has a stored key and `catchup` prints the move that is still owed.

1. `jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC)`. If every stored key is already in one project, scan wrote it (`jira: set jiraProject to WAR (every stored key is in project WAR)`) even when `getVisibleJiraProjects` is missing. Do not run `project --set` for that line. Jira moves use the key that was written. Mixed keys stay empty and the line names the projects. A different existing value is not overwritten. Otherwise `project --list` prints candidates and writes nothing. One project you recognize: `project --set WAR`. That also fills an empty `jiraKeyPrefixes`. Jira is connected and more than one project is visible: `project --probe` writes the search, then `project --record` stores the single project that contains the plan id. Several hits are listed and nothing is guessed. One Atlassian site from `project --apply` is stored in `jiraSite`.
2. `why nothing linked: jiraProject is empty`. Same as step 1. No transition runs until a project is set.
3. `why nothing linked: the map file has keys the beam never stored`. `verify --link` copies `.warp/jira-map.json` or `jiraKeyMap` onto the beam and writes `.warp/jira-resolve.json`. Then `catchup`.
4. `status: unmapped` and `fix` is `verify --link`. Run it. If tickets are still unmapped and Jira is connected, run that file's queries: external-id field, then label `warp:<id>`, then a remote link. `verify --apply results.json` stores one exact match. Two matches are named and neither is stored. A summary match is not stored on this path. A manual key is not overwritten.
5. The check prints `summary candidate: WAR-1 (exact 1.00)`. `/warp-jira-match --apply --id WV-01` stores an exact or 60-character prefix hit. A fuzzy proposal stays unstored until `--apply --yes`. Ambiguous rows are listed and not stored. Done issues and issues already mapped to another ticket are skipped. The full flags are `--chars`, `--min-score`, `--include-done`, and `--all`.
6. Still unmapped, and you know the key. `map --set WV-01=WAR-1`, or `beam.py set --jira`. `--force` is only for a prefix that is not configured. Then `catchup`.
7. `key missing: claim lookup result was not recorded`. The search may already have found `WAR-1`. Record it before any transition: `resolve --ticket WV-01 --key WAR-1 --issue-id <id> --cloud-id <cloudId>`. That writes the beam and `.warp/jira-map.json` together. A plan id is refused. `transitionJiraIssue` uses only the stored key.
8. `key stored` and `lastAttempt: claim failed — ...`. That line is `record --result failed --error "..."`. `jira.startedAt` was not set. Fix the error (workflow name, missing transition, connector), then `record` the success. The same text is in `.warp/outbox.md`.
9. `key stored` and catch-up still lists a move. The agent calls `getTransitionsForJiraIssue`, `jira_sync.py pick`, then `transitionJiraIssue`, then `addOrEditJiraIssueComment`, then `record` and `record-comment`. An already-merged auto-merge ticket is asked for Done, not a move back to In Progress.
10. `key stored` and nothing is missing, but Jira still did not move. `jiraTransition` is not false. `jiraMcp` is the server name Cursor shows. `jiraInProgressStatus`, `jiraQaReadyStatus`, and `jiraDoneStatus` match the workflow. Re-run `/warp-init` if the config file is missing keys. The script cannot see whether the server is connected.

`/warp-jira-view WAR-1` (or a plan id) prints the live fields when you need to see why a transition is absent. It does not write the beam.

**External ID not written.** `jiraWriteExternalId: true` makes `/warp-scan` and `/warp-jira-check` print `MUST DO write External ID` for mappings that already exist. The flag is the consent. `--yes` is not required. The agent calls `editJiraIssue` for each line, then `jira_sync.py record-external-id`. To do it without a scan: `/warp-jira-external-id`, then `/warp-jira-external-id --apply --yes`. A key whose source is `external` is skipped. A different non-empty value needs `--force-external-id`. No comment is added. `jira.externalId` on the beam is not proof the Jira field was written. `jira.externalIdWritten` is, and its `method` says `field`, `label`, or `remote-link`.

**External ID field missing.** One line, not one skip per ticket. `.warp/jira-field.json` stores the miss. The default fallback adds label `warp:<id>`. Create the field in Jira admin (Short text, name External ID, add it to the project screens), then `/warp-jira-external-id --recheck`. Or `/warp-jira-external-id --create-field --yes` to probe for a create tool. The Rovo catalog does not have one. `jira mapping:` on `/warp-jira-check` and `stored in Jira:` on `/warp-jira-view` say which method landed.

## Nothing linked

`needs mapping` on `/warp-jira-check` does not mean the external-id search already ran. Read `why nothing linked` before running another search.

| Line | What to do |
|---|---|
| `jiraProject is empty` | `project --list`, then `--set`, or `--probe` and `--record` |
| `the map file has keys the beam never stored` | `verify --link` |
| `no Jira key in the plan, export, or map file` | Connect Jira and let scan or `verify --link` search, or `/warp-jira-match` when the summaries correspond, or `map --set` |
| `ambiguous matches` | Do not pick one. `map --set WV-01=WAR-1` |
| `stored WV-01 is the plan id` | The lookup was never recorded. `resolve --ticket` with the issue key |

A Jira JSON import stores `externalId` as the plan id. `h2. Size`, `h2. Locks`, `h2. Blocked by`, `h2. Acceptance`, and labels `size:S`, `auto-merge`, and `area:*` describe the ticket. None of those fields are the issue key. The issue `key` in an export is the only export field that becomes `jiraKey`.

## Stuck agent

Reconcile marks `stuck` when `updatedAt` is older than `stuckAfterMinutes` (default 90) and the ticket is not `awaiting_approval`. Reattach by spawning a Shuttle on the same id; it must reuse the branch. That window is not worker death. A dead background worker is `staleMinutes` below.

## Dead worker

Warp's background worker is the Shuttle on a claimed ticket (`claimed`, `planning`, `coding`, `fix`). The one `warp-listen` listener (`listener` on the beam) is one background subagent on the parent's checkout. `subagentVm` does not apply. A dead turn does not notify Warp. Cursor does not restart it. Plugin hooks do not run on cloud runners. There is no process table.

Each Shuttle writes a heartbeat the parent can see. A remote Agent writes `.warp/tickets/<id>/state.json` (`heartbeatAt`) and a `heartbeat` line in `log.jsonl`, and pushes only that directory, at claim, at each status change, and at least every 5 minutes. The tick copies that onto `lastSeenAt`. It does not replace the live beam with the branch snapshot. `beam.py heartbeat` writes `lastSeenAt` directly when the worker is already on the live beam, and prints the `reap:` line. A heartbeat is not a timer. A Shuttle writes it between steps of work it is already doing. The parent writes `listener.parentSeenAt` from `parent-exit --wait`. Each read writes `listener.lastSeenAt` and `.warp/listener.json`. `inbound.py heartbeat` is for a listener someone starts by hand. A run does not use it. Shuttles stay inside `staleMinutes` (default 15). Channel reads use `listenerFastSeconds` and `listenerSlowSeconds`. `pollSeconds` is only the parent-wait cap. A stale heartbeat does not start another listener.

`beam.py watchdog` runs on every Warp tick and from `/warp-start` and `/warp-resume` (`scan.py start` and `scan.py resume`). A Shuttle is dead when `lastSeenAt` is older than `staleMinutes`, or it never heartbeated and the claim is older than `staleMinutes`. A fresh heartbeat is alive. Do not start a second worker. `watchdog: skipped (paused)` or `watchdog: skipped (stopped)` means do not recover and do not start a replacement. The watchdog prints no listener line and never starts a listener.

The listener is a Subagent of the parent turn, not a worker the watchdog replaces. The parent starts each poll from `orchestrator.py supervise` (`listener: poll`). A `running` flag left when the window closed does not block the next tick. Close the window, open a new one, `/warp-start`. State comes from main plus ticket folders. `/warp-start` and `/warp-resume` fetch origin/main, load that beam when the checkout is empty, then patch each in-flight ticket from `.warp/tickets/<id>/`. On every merge, and at the end of each parent tick, the live beam is pushed to main. `/warp-update-state` is the insurance sync: load main, patch `.warp/tickets/<id>/` only, keep pause, stop, claims, and runState the parent has that main does not, and push. `/warp-pause` and `/warp-stop` run that sync before they return. Each ticket is a subagent in its own git worktree, or on its own VM when `subagentVm` is true.

Dead Shuttle: the ticket stays on the same work. Branch, pull request, `jira.startedAt`, and locks stay. The launch slot is released immediately, so a dead claim does not count against `maxAgents`. Status becomes `recovering` when the replacement actually starts (not a silent stuck claim, and not `queued`). On start or resume, a stale heartbeat is dead and a fresh heartbeat stays. Warp dispatches open-pull-request fixes first, then `shuttle: replace <id>` lines, up to the live caps. Dispatch exactly one new Shuttle for each of those lines. Do not queue a duplicate. Do not launch a `shuttle: release` line. `ready()` will not give those locks to another ticket. `recoveredAt` and `recoveries` record the replacement. The new Shuttle heartbeats, sets status back to `recoveryPriorStatus`, and continues that branch. It does not open a second pull request. Herald posts one line: `<id> worker died. A new Shuttle started.` A Shuttle that `/warp-pause` or `/warp-stop` ended is not a dead worker. `/warp-start` or `/warp-resume` launches a new Shuttle on the same branch (`fix: <id> resume after halt`), counts no recovery, and posts no worker-died message.

Two ticks close together reserve the replacement on the first write. The second tick sees the fresh start and does not launch another.

`maxRecoveries` (default 5) caps Shuttle replacements. Past the cap the ticket is `alarm` with reason `worker-died` and no new Shuttle starts. Herald posts `<id> worker died. Recovery cap reached.` Branch, pull request, and `jira.startedAt` stay. `/warp-retry` is the way back onto the queue. `/warp-init` appends `staleMinutes` and `maxRecoveries` when `.warp/config.yaml` does not have them yet.

## Agents pile up or keep waking

Agents pile up when four things are true together. A worker can start a worker. A worker waits for something instead of returning. Nothing ends an agent that is no longer wanted. And nobody can list what is out. Five mechanisms remove those. [LIFECYCLE.md](LIFECYCLE.md) is the rule.

| Mechanism | What it does |
|---|---|
| The gate | `checkout.py launch` is the only source of a Shuttle prompt. It prints `spawn: closed <id>` and a `refuse:` line, and exits 2, while the run is paused, stopped, or finished, and for a merged or parked ticket. Only the parent starts agents. |
| The contract | Every worker prompt carries `agent: <id>` and the worker contract: one step, one line. A worker never starts another agent, never subscribes, sets a timer, loops, sleeps, or waits on a pull request, CI, Bugbot, Slack, or an approval. Only the parent waits. |
| The reap check | `agents.py reap --beam .warp/beam.json --id <agent> --ticket <id>` (`--remote` on a dedicated VM) prints `reap: continue`, or `reap: exit <reason>` and exits 3. `ticket_state.py append`, `beam.py heartbeat`, and `inbound.py poll` print the same line. |
| The halt | `/warp-pause` and `/warp-stop` are the same teardown. Every row in `.warp/agents.json` is ended and marked halted. With `CURSOR_API_KEY`, the run's cloud agents are cancelled and archived. The same teardown runs once at the end of a run (`halt: done agents=<n>`). |
| The list and the clear | `/warp-list` shows what is still out and changes nothing. `/warp-cleanup` cancels and archives it. You run it yourself. It never runs on its own. |

Use `/warp-list` to see what is out, and `/warp-cleanup` to cancel and archive it. They take the same selection options.

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
```

`/warp-list` runs the first line. It changes nothing. It prints `instance: warp:<instance> host=<host> machine=<machine id>`, then one line per row in `.warp/agents.json` (`agent: <id> role= ticket= state= started= ended=`), then `registry: live=<n> ended=<n>`. With `CURSOR_API_KEY` in the environment it prints `cloud: id= name= repo= status= updated=` for each cloud agent in this repo that is in the registry or whose name starts with this run's `[warp:<instance>]` tag. `status=ACTIVE` is still running. `status=IDLE` is not. An agent it would leave alone is `list: kept <id> reason=parent` (this run's parent), `reason=self` (the agent running the command), or `reason=running-untagged`. The last line is `out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>`. Without the key it prints `cloud: not read.` and one `cloud: link https://cursor.com/agents/<id>` for each recorded cloud agent.

`/warp-cleanup` runs the second line. Run `/warp-pause` or `/warp-stop` first. It cancels the run of each RUNNING or ACTIVE match (`cleanup: cancelled <id>`) and archives every match (`cleanup: archived <id>`). The agent running the command stays (`cleanup: skip <id> reason=self`), and so does this run's parent (`reason=parent`). Leave `--apply` off for a dry run: it prints `cleanup: tag [warp:<instance>]`, each match, `cleanup: would cancel <id>` for the running ones, and `cleanup: count`, and changes nothing. Archive is reversible in the Cursor UI. Warp never deletes an agent.

| What is piled up | List it | Clear it |
|---|---|---|
| This run's agents | `/warp-list` | `/warp-cleanup` |
| Agents from before 1.5.0, which carry no tag | `/warp-list --untagged` | `/warp-cleanup --untagged` |
| What another Warp run left behind | `/warp-list --tag <instance>` | `/warp-cleanup --tag <instance>` |
| What any Warp run left behind | `/warp-list --tag all` | `/warp-cleanup --tag all` |
| Agents in any repo whose name, summary, or prompt contains a word | `/warp-list --scan="Jira","Fix","Bugbot"` | `/warp-cleanup --scan="Jira","Fix","Bugbot"` |

`<instance>` is the six characters in that run's header and names, for example `a1b2c3`. `warp:a1b2c3` and `[warp:a1b2c3]` are the same. `--untagged` matches loosely, on Warp role words in the name or prompt, so it can show someone else's agent. Read the list before you clear it. An idle one is archived. One that only matched loosely and is still running is left alone (`reason=running-untagged`), because it may be someone else's. `--force` cancels it. `--all-idle` and `--any-repo` widen the match further. Leave them off for a pile of Warp agents in this repo. `--scan` is the word match, and it crosses repos: it ignores this repo and the tag and registry rules, and pages through every agent the key can see. `--scan="Jira","Fix","Bugbot"`, `--scan Jira,Fix,Bugbot`, and `--scan Jira --scan Fix` are the same. Matching is case-insensitive, on the name, the summary or description, and the first prompt. Read `/warp-list --scan` before `--apply`. A dry run prints `cleanup: would archive` and `cleanup: would cancel`. An idle match is archived. A running one is left (`reason=running-scan`) unless `--force`. The agent running the command and this run's parent are never matched. The refusal while the run is running still applies.

An agent that keeps waking after it returned may be woken by Cursor, not by Warp. Depending on the Cloud Agents settings for the account, Cursor can wake a cloud agent that opened a pull request when CI fails on it or a comment arrives. That wake-up is not a step from the parent. The worker contract tells the worker to run the reap check and return without acting, and the parent handles red CI on its next pass. To stop the wake-ups at the source, turn off automatic CI fixes for Cloud Agents in the Cursor dashboard for the repos Warp runs in.

## Pause or stop printed `cleanup: link`

`cleanup: link https://cursor.com/agents/<id>` means `CURSOR_API_KEY` is not in the environment, so the pause or stop could not cancel or archive that cloud agent. The local half still ran: every row in `.warp/agents.json` is ended and marked halted (`halt: paused agents=<n>` or `halt: stopped agents=<n>`), the state is pushed, and each worker reads `reap: exit` at its next check. Open each link and archive the agent in the Cursor UI. Or export `CURSOR_API_KEY`, run `/warp-list` to see what is out, and run `/warp-cleanup` to cancel and archive it:

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
```

The key is read from the environment and is never written to the repo.

## `cleanup: cut short`

`cleanup: cut short; run /warp-cleanup for the rest` means the cloud half of a pause or stop gave up after 45 seconds, before every cloud agent was cancelled and archived. The run is still paused or stopped, and every row in `.warp/agents.json` is still ended. Run `/warp-list` to see what is still out, then `/warp-cleanup` for the rest:

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
```

Agents it did not reach still exit at their next reap check.

## A Shuttle returned `result: <id> stopped <reason> agent=<agent>`

That line is a clean stop, not a failure. `<agent>` is the registry id or the cloud id of the Shuttle that read `reap: exit`. The parent stamps `reaped` on that row only. A late line from a halted or replaced VM does not end the Shuttle that replaced it; it is journaled as `reap-stale`. The Shuttle read `reap: exit <reason>` from its reap check (`agents.py reap`, or the same line from `ticket_state.py append` or `beam.py heartbeat`), did not commit, push, or open anything more, and returned. Do not relaunch the ticket because of it. It is not a dead worker: no recovery is counted and `worker-died` is not raised.

| Reason | Meaning | What to do |
|---|---|---|
| `paused`, `stopped` | `/warp-pause` or `/warp-stop` ran | Nothing. `/warp-resume` or `/warp-start` launches a new Shuttle on the same branch (`fix: <id> resume after halt`). |
| `done` | Every ticket is merged or parked | Nothing. The run is finished. |
| `settled` | This ticket is merged or parked | Nothing. |
| `replaced` | The parent started another Shuttle for this ticket, or another parent took the run | Nothing. The other Shuttle has the ticket. |
| `halted` | The Shuttle was started before a pause or a stop, and the run moved on without it | Nothing. `/warp-start` or `/warp-resume` already launched a new one for that ticket. |
| `archived` | Cleanup archived this agent | Nothing. Work it had not pushed stays in the archived agent. Unarchive it in the Cursor UI to get it back. |
| `not-resumed` | Its turn returned more than `staleMinutes` ago and the parent did not resume it | Nothing. Something other than the parent woke it. The parent handles the ticket on its next pass. |
| `not-launched` | No launch was recorded for this id | Find what started it. Only the parent starts a Shuttle, from `checkout.py launch`. |

## `pass: aborted` or `cleanup: unreachable`

`orchestrator.py supervise` prints `pass: aborted. The run was paused during this pass.` (or `stopped`) when `/warp-pause` or `/warp-stop` landed from another window or from the channel while the pass was working. The halt wins. The pass does not write over it, anything it recorded in `.warp/agents.json` is ended, and no `start` line from it counts. Start nothing and run `parent-exit`, which prints `parent: exit`.

`cleanup: unreachable <id>`, `cleanup: cancel failed <id>`, and `cleanup: archive failed <id>` mean a Cloud Agents API call failed, with the link to that agent. Nothing is aborted: the pause, stop, merge, or pass carries on. The agent is left as it is, never archived while it might still be running, and tried again on the next pass or the next `/warp-cleanup`. The agent itself still exits at its next reap check.

## `spawn: closed <id>` from `checkout.py launch`

`checkout.py launch --id <id>` printed `spawn: closed <id>` and a `refuse:` line, printed no prompt, and exited 2. That is the gate. Start nothing.

| Line | What to do |
|---|---|
| `refuse: Warp is paused. No agent starts. /warp-resume or /warp-start opens the run.` (also `stopped` and `finished`) | `/warp-resume` or `/warp-start`. Launch from the `start <id>` lines that command prints. A finished run has nothing left to launch. |
| `refuse: ticket <id> is merged. Nothing starts for it.` (also `parked`) | Nothing. `warp:retry <id>` is the way to start a parked ticket again. |

## `parent: exit superseded`

`orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>` prints `parent: exit superseded` when a later `/warp-start` or `/warp-resume`, in this window or another one, took the run over. One parent per run. End that turn at once and start nothing. The later parent owns the run. Its id is the `parent: session <id>` line that its `scan.py start` or `scan.py resume` printed. To take the run back, run `/warp-start` or `/warp-resume` in the window you want. It prints `run: was already running. This session takes it over.` The other parent exits on its next `parent-exit`.

## `listener: none`

`orchestrator.py supervise` prints `listener: none` when no `slackChannel` or `teamsChannel` is set for the `messenger` in use. No channel is configured, so no listener is started at all and `warp:` messages are not read. Use the slash commands (`/warp-proceed`, `/warp-pause`, `/warp-stop`), or set a channel in `.warp/config.yaml`. `listener: hold` means a poll is out or the next one is not due. `listener: idle` means the run is paused, stopped, or finished. Neither starts a listener.

## The parent's turn died

A dead turn does not notify Warp, and Cursor does not restart it. Nothing restarts the parent. No worker does, and no hook does. Workers that were out finish their step, write their ticket folder, and return. They start nothing. Run `/warp-resume` or `/warp-start`. They are the same command. It prints a new `parent: session <id>`, loads state from main plus the ticket folders, releases Shuttles whose heartbeat is stale, and runs passes again. An earlier parent that is still alive reads `parent: exit superseded` on its next `parent-exit` and ends its turn.

## `cleanup: --running refused while the run is running`

`agents.py cleanup --cloud --apply --running` prints `cleanup: --running refused while the run is running. /warp-pause or /warp-stop first.` when the run is still running. `--running` would cancel Shuttles that are doing work. The command carries on without it: idle matches are archived and RUNNING or ACTIVE matches are skipped (`reason=running`). Run `/warp-pause` or `/warp-stop`, then run `/warp-cleanup` again. `/warp-list` shows what is still out at any time, because it changes nothing. `--force` overrides the refusal and cancels live work.

## Should I pause or stop?

`/warp-pause` and `/warp-stop` are the same teardown. Stop also writes the completion report. That is the one difference. Either way, carry on with `/warp-start` or `/warp-resume`, which are the same command.

| | `/warp-pause` | `/warp-stop` |
|---|---|---|
| `runState` | `paused` | `stopped` |
| Every agent in `.warp/agents.json` ended, every Shuttle slot freed | yes | yes |
| This run's cloud agents cancelled and archived (with `CURSOR_API_KEY`) | yes | yes |
| Branches, worktrees, pull requests, locks, `jira.startedAt` | kept | kept |
| Completion report `.warp/warp-complete.html` and the Herald totals | no | yes |
| `stoppedAt` recorded as the end of the run | no | yes |
| Use it when | you will carry on: overnight, a meeting, a bad base branch | the run is over, or you want the report |
| To carry on | `/warp-start` or `/warp-resume` | `/warp-start` or `/warp-resume` |

## `/warp-start` on a paused run

It continues. `/warp-start` is not refused on a paused run, and there is nothing to choose between it and `/warp-resume`. Either one sets the run running from a stopped run, a paused run, or a run that is already running, and prints which:

| Line | The run was |
|---|---|
| `run: was stopped. Starting.` | stopped, or never started |
| `run: was paused (<reason>). Continuing.` | paused |
| `run: was already running. This session takes it over.` | running. The earlier parent reads `parent: exit superseded` |

Both run the same prompt gate, and `--force` means the same on both.

## New tickets are not starting after a start

Read the `unfinished:` line that `/warp-start` or `/warp-resume` printed:

```text
unfinished: 6 open. 1 need a fix, 3 need a Shuttle, 0 have a Shuttle out, 2 wait on review or checks. These start first, up to maxAgents 18. New tickets start after, while fewer than maxInProgress 20 are open.
```

Unfinished work takes the `maxAgents` slots first: open pull requests that need a fix, then tickets whose Shuttle was halted or died, then new tickets from the ready queue. A new ticket starts only in a slot unfinished work did not need. `unfinished: none. New tickets start from the ready queue.` means nothing was left over.

`holding new launches (N/maxInProgress), N fix workers running` in the open-work list means `maxInProgress` is full: that many tickets are started and not merged or parked. A ticket waiting on review or CI is open and uses no agent. New tickets start again when one merges or parks. Fixes are not held.

The `effective:` line shows both caps: `effective: runner=<r> subagentVm=<b> memoryCheck=<b> maxAgents=<n> maxInProgress=<n> launch=<l>`. On a shared machine `checkout.py launch` prints `local-cap: <n>`, and `refuse: local subagent cap <n> (live ...)` when the slots are full. That number is `maxAgents`, or lower when `memoryCheck: true` and memory is tight.

## `config: <key> is retired and ignored`

`/warp-start`, `/warp-resume`, and `/warp-version` print `config: <key>: <value> is retired and ignored.` for a key in `.warp/config.yaml` that no longer does anything. The line goes on to say what replaced the key, and ends `Remove it from .warp/config.yaml.` The run is not blocked. Remove the key.

| Key | What replaced it |
|---|---|
| `maxLocalSubagents` | `maxAgents`. It is the one cap on agents that run at once, on this machine or on their own VMs. `memoryCheck: true` lowers it on a shared machine when memory is tight |
| `maxFixWorkers` | `maxAgents`. A fix is an agent like any other and takes a slot. Unfinished work takes the slots first |

## Two Warp runs in one repo or one channel

Each run has its own tag, `warp:<instance>`. The instance is six characters that `/warp-start` sets once on the beam, and a resume from another window or VM keeps it. Every agent the run starts is named `[warp:<instance>] <ticket> <step>`, and the listener is `[warp:<instance>] listener`. To tell two runs apart, read the header on any Slack or Teams message, `Warp | <repo> / <project> | warp:<instance>`, or the `instance: warp:<instance> host=<host> machine=<machine id>` line that `/warp-list`, `/warp-status`, `/warp-version`, start, and resume print.

Pause, stop, and `/warp-cleanup` touch only agents in their own registry or carrying their own tag. Another run's agents are not matched, and neither is any other agent in the repo, whatever words are in its name. To act on another run's leftovers on purpose, name its tag: `/warp-list --tag <instance>`, then `/warp-cleanup --tag <instance>`. Nothing checks whether that run is still going.

## Lock-escape repair

`lock-escape` means the diff needed a path outside the ticket's locks. The Shuttle stores each of those paths with `--escaped` when it sets the alarm. It does not widen the lock itself.

The parent's pass, while the run is running, calls `alarm_repair.py next` about every `alarmRepairMinutes` (default 15). The listener does not start the repair. The repair widens that ticket's locks to the paths that escaped, records the added paths on `addedLocks`, and starts one Shuttle inside the widened lock. Herald's start line names the paths added.

Do not start that repair when an in-flight ticket holds one of those paths, or holds one of the ticket's current locks. `alarm-repair: locked <id> by <holder>` means wait. When the holder finishes (it is no longer in flight), the next pass widens and starts. A queued ticket's overlapping lock can be widened. Nothing is stolen from work that is running.

One repair at a time, one ticket at a time, in plan order. If the repair alarms `lock-escape` again, or the Shuttle errors, leave the alarm, record the attempt, and start the next lock-escape ticket in that same call. Do not retry the one that just failed in this pass. If the repair puts the ticket back on the normal path and clears the alarm, or the ticket merges, wait for that completion, then start the next. A second tick while a repair Shuttle is working does not start another.

`maxAlarmRepairs` (default 5) is the cap. After that the alarm stays and later passes skip the ticket. Herald posts `<id> lock-escape repair given up.` An older alarm with no path list prints `alarm-repair: name <id>`. That Shuttle names the paths with `alarm_repair.py paths` before it edits. Pause and stop do not repair, because the run is not running. `bugbot-failed`, `stuck`, `worker-died`, `ci-red`, and `gate-red` are not repaired this way. `/warp-init` appends `alarmRepairMinutes` and `maxAlarmRepairs` when the config does not have them yet.

## Cap change

Edit `.warp/config.yaml` `maxAgents`. Next tick picks it up if the skill re-reads yaml. Also set the copy inside the beam if a tick reads only the beam: re-ingest is wrong for a live run; edit `beam.config.maxAgents` with a journal note, or set it in both places.

## Reinstall or reset

`/warp-init` is idempotent. It never overwrites `.warp/config.yaml` values or existing plugin files, so it will not upgrade an installed copy. It does append config keys the file is missing, and it records the installed version in `.warp/version`. When that copy is older than the plugin you ran, it prints `plugin is vOLD, repo copy is vNEW: run /warp-upgrade`. `/warp-upgrade` replaces `.cursor/plugins/warp`. Run it in the repo that has Warp installed. When the source is a git checkout, it fetches the default branch. If the fetch fails, it prints `fetch failed` and `keeping the installed copy`. The installed copy stays. It does not overwrite `.warp/config.yaml` or the beam. It prints `Warp vX.Y.Z`. Reload Cursor, then confirm with `/warp-version`. `/warp-init` does not upgrade an existing copy. This command does. `--force` is not an upgrade flag. The flags are `--root` and `--source`. If `/warp-upgrade` is not in the command list, `/warp-version` prints `python3 .cursor/plugins/warp/scripts/upgrade.py` (`?` prints the flags). The script does not need the slash command. A 1.4.6 tree includes that file. See the README Update section. On a cloud runner, `/warp-start` refuses when `transitionJiraIssue`, `addOrEditJiraIssueComment`, or `slack_send_message` is missing from the MCP allow list unless `--force`. A local runner does not block. Init and upgrade write every `commands/*.md` file to `.cursor/commands/`, including `.cursor/commands/warp-upgrade.md`, so a reload lists `/warp-upgrade`, `/warp-update-state`, `/warp-status`, and the other commands when the plugin command index still omits that path.

## agents stop and ask to Run/Allow

`slack_send_message` and `addOrEditJiraIssueComment` prompt when the allowlist entry does not match the server id Cursor shows, or when the entry was never written.

Before 1.3.13, `/warp-allow-notify` wrote `slack:<tool>` and `atlassian:<tool>` only, Jira tools required `--with-jira`, and `/warp-init` did not run it. The Run dialog often names `user-slack`, `plugin-slack-slack`, `project-0-<folder>-slack`, `atlassian-rovo`, or `claude_ai_Atlassian`. `project-0-<folder>` changes when you open another workspace or worktree. A literal `slack:slack_send_message` does not match those.

`/warp-init` now writes the project list (specific tools, plus `user-`, `plugin-`, and `*<name>*:<tool>`). `autoAllowTools: false` or `/warp-init --no-allow` skips it. User-level `~/.cursor` files still need `/warp-allow-notify --user --yes`.

Then:

1. Set Run Mode to Auto-review, Allowlist, or Run Everything. Ask Every Time ignores `permissions.json`. A team admin override ignores it too.
2. `/warp-allow-notify --check` prints detected servers, what project and user files cover, and the entries that would fix a gap. `--list` only prints servers.
3. If the dialog's tool name is not in `scripts/mcp_tools.py`, add `server:tool` to `notifyAllow` and run `/warp-allow-notify` again.
4. Reload Cursor (Developer: Reload Window) or start a new chat.
5. Undo with `/warp-allow-notify --revoke`. That removes only the entries Warp recorded.

IDE agents prompt until that matches. Cloud agents and automations do not prompt, and plugin hooks do not run on cloud runners. The Cursor CLI uses `.cursor/cli.json` (`Mcp(server:tool)`), not the IDE button; `agent -f` approves tools for that process. A `beforeMCPExecution` allow does not skip the IDE prompt. On a cloud runner, `scripts/mcp_allow.py --server SERVER --tool TOOL` prints `allow` or `ask`. Call the tool only when it prints `allow`. That is the same Slack, Teams, Jira, and optional GitHub list, not every MCP tool, and it does not approve shell commands. `--allow-server-tools` writes `server:*`, which allows every tool on that server, including destructive ones. Leave it off unless you mean that.

## Scan one folder

`/warp-scan <folder>` limits the search to that folder. If the name matches several folders the scan stops and lists them; re-run with the full path. The beam and `scan.json` record the folder.

## Rebuild the graph

Only when `schedule.json` is regenerated. Copy `.warp/` aside first. Ingest overwrites live status.
