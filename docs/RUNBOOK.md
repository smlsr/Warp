# Runbook

## Pause overnight but keep the board

`/warp-pause` with a reason. In-flight Shuttles finish the current step and checkpoint. The HTML board stays valid. `/warp-resume` reconciles, then ticks.

## A PR will not go green

After `maxFixAttempts` (default 3) Reed sets `alarm` / `bugbot-failed` and Herald posts. The same counter covers auto-merge and manual tickets. Warp will not retry it. Reply `warp:retry <id>` after adding a note on the Jira issue. There is no `warp:hold` command. `/warp-pause` stops new claims.

Manual tickets (`autoMerge` false) use that loop before anyone is asked to review. `beam.py set --status awaiting_approval` is refused until `pr.bugbot` is `pass` and `pr.ci` is `green`, unless `bugbotManual` is false (Bugbot off for that path only) or `bugbotRequired` is false (Bugbot off for both). QA Ready is the Jira move for that status, not for pull-request open.

New commits after QA Ready (`--sha` while status is `awaiting_approval`) set status `bugbot_running`, clear Bugbot and CI, and comment "new commits, re-running Bugbot" on Jira and, in connected mode, the pull request. Jira stays at QA Ready. A later Bugbot failure goes to `fix` or `alarm` and does not move the Jira issue backwards. A later pass returns to `awaiting_approval` and comments again, without a second transition.

## Sizes not in autoMergeSizes waiting on you

Herald posts the pull request, or the local branch when `pushMerge` is false, and `warp:proceed <id>`. An approval on the provider (GitHub review or Bitbucket APPROVED) is enough. The chat command is the override, and it is the only signal in local-only mode besides the listener. Jira is at QA Ready while this waits. After the merge it moves to Done (`jiraDoneOnManualMerge`, default true).

The channel reader is one `warp-listen` sub-agent for the beam, not one per awaiting_approval ticket. `/warp-start` and `/warp-resume` launch it when `listener.state` is not `running`, and `beam.py watchdog` replaces it when `lastSeenAt` is older than `staleMinutes`. `listener: already running` with a fresh heartbeat means do not launch a second. `/warp-pause` and `/warp-stop` set `listener.state` to `stopped` through `inbound.py release`. The listener must not keep reading while paused or stopped. Cursor cannot start it from Slack. There is no webhook, and plugin hooks do not run on cloud runners. If its turn has just ended and the heartbeat is still fresh, send `/warp-proceed <id>` in the agent chat. After `staleMinutes`, the next tick starts one replacement. Pause and stop do not.

`/warp-proceed WV-01` also accepts the Jira key (`WAR-1`) or a `#` number (`#01`). The listener runs `inbound.py`, which acks in the channel before `proceed.py`. The ack is `Received warp:proceed XV-01. Merging and moving Jira to Done.` A bad id acks and merges nothing else. If the ticket is not `awaiting_approval`, the ack says so and does not merge. When it accepts, merge in that same turn and run the post-merge MUST DO before you reply. The channel gets the ack first, then the merge sha and the Jira status.

Do not proceed a red PR. Reed will refuse.

A ticket that already merged without leaving QA Ready shows up on `/warp-jira-check` as `merged-but-not-done`. `catchup` prints the Done transition, the merged comment, and the exact command:

```bash
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json --id <id>
```

Record the result with `record --event done`. If Jira has no transition from QA Ready to Done, `pick --kind done` tries the name and then one done-category transition. `record --result no-transition` writes `.warp/outbox.md` and Herald posts Jira not updated. Set `jiraDoneOnManualMerge: false` only when QA should keep Done.

## Run finished or stopped

When nothing is queued or active, or you run `/warp-stop`, Warp writes `.warp/warp-complete.html` and Herald posts the totals and the path. The file is local and gitignored. `reportOnComplete: false` skips that automatic write. While the run is still going, `/warp-report --partial` writes a snapshot and does not wait for the end.

## Gate red

Fix on the member branch (G0 is L-01, M-01, D-01). Dependents stay out of `ready()` until `beam.py gate --status green --evidence "..."`. A red critical-path gate is the one case to pause the program: later agents will only pile up lock-free work that cannot ship.

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

Warp's background workers are the Shuttle on a claimed ticket (`claimed`, `planning`, `coding`, `fix`) and the one `warp-listen` listener (`listener` on the beam). A dead turn does not notify Warp. Cursor does not restart it. Plugin hooks do not run on cloud runners. There is no process table.

Each worker writes a heartbeat on the beam: `lastSeenAt` and its agent id. A Shuttle runs `beam.py heartbeat` at claim, at each status change, and at least every 5 minutes while the turn is alive. The listener runs `inbound.py heartbeat` at claim and on every pass of its read loop. Both stay inside `staleMinutes` (default 15).

`beam.py watchdog` runs on every Warp tick and from `/warp-start` and `/warp-resume` (`scan.py start` and `scan.py resume`). A worker is dead when `lastSeenAt` is older than `staleMinutes`, or it never heartbeated and the claim or listener start is older than `staleMinutes`. A fresh heartbeat is alive. Do not start a second worker. `watchdog: skipped (paused)` or `watchdog: skipped (stopped)` means do not recover and do not start a replacement.

Dead listener while the run is running: the stale `running` flag is cleared in the same write that reserves one new id. Launch exactly one `warp-listen` agent for `listener: replace <id>`. Do not claim a different id. A second tick prints `listener: fresh` and must not launch another. Herald posts one line: `Listener died. A new one started.` The same text is in `.warp/recovery.json`.

Dead Shuttle: the ticket stays on the same work. Branch, pull request, `jira.startedAt`, and locks stay. Status becomes `recovering` (not a silent stuck claim, and not `queued`). Dispatch exactly one new Shuttle for `shuttle: replace <id>`. Do not queue a duplicate. `ready()` will not give those locks to another ticket. `recoveredAt` and `recoveries` record the replacement. The new Shuttle heartbeats, sets status back to `recoveryPriorStatus`, and continues that branch. It does not open a second pull request. Herald posts one line: `<id> worker died. A new Shuttle started.`

Two ticks close together reserve the replacement on the first write. The second tick sees the fresh start and does not launch another.

`maxRecoveries` (default 3) caps Shuttle replacements. Past the cap the ticket is `alarm` with reason `worker-died` and no new Shuttle starts. Herald posts `<id> worker died. Recovery cap reached.` Branch, pull request, and `jira.startedAt` stay. `/warp-retry` is the way back onto the queue. `/warp-init` appends `staleMinutes` and `maxRecoveries` when `.warp/config.yaml` does not have them yet.

## Lock-escape repair

`lock-escape` means the diff needed a path outside the ticket's locks. The Shuttle stores each of those paths with `--escaped` when it sets the alarm. It does not widen the lock itself.

The one listener, while the run is running, calls `alarm_repair.py next` about every `alarmRepairMinutes` (default 15). The repair widens that ticket's locks to the paths that escaped, records the added paths on `addedLocks`, and starts one Shuttle inside the widened lock. Herald's start line names the paths added.

Do not start that repair when an in-flight ticket holds one of those paths, or holds one of the ticket's current locks. `alarm-repair: locked <id> by <holder>` means wait. When the holder finishes (it is no longer in flight), the next listener pass widens and starts. A queued ticket's overlapping lock can be widened. Nothing is stolen from work that is running.

One repair at a time, in plan order. If the repair alarms `lock-escape` again, or the Shuttle errors, leave the alarm, record the attempt, and start the next lock-escape ticket in that same call. Do not retry the one that just failed in this pass. If the repair puts the ticket back on the normal path and clears the alarm, or the ticket merges, wait for that completion, then start the next. A second tick while a repair Shuttle is working does not start another.

`maxAlarmRepairs` (default 3) is the cap. After that the alarm stays and later passes skip the ticket. Herald posts `<id> lock-escape repair given up.` An older alarm with no path list prints `alarm-repair: name <id>`. That Shuttle names the paths with `alarm_repair.py paths` before it edits. Pause and stop do not repair, because the listener is not running. `bugbot-failed`, `stuck`, `worker-died`, `ci-red`, and `gate-red` are not repaired this way. `/warp-init` appends `alarmRepairMinutes` and `maxAlarmRepairs` when the config does not have them yet.

## Cap change

Edit `.warp/config.yaml` `maxAgents`. Next tick picks it up if the skill re-reads yaml. Also set the copy inside the beam if a tick reads only the beam: re-ingest is wrong for a live run; edit `beam.config.maxAgents` with a journal note, or set it in both places.

## Reinstall or reset

`/warp-init` is idempotent. It never overwrites `.warp/config.yaml` values or existing plugin files, so it will not upgrade an installed copy. It does append config keys the file is missing, and it records the installed version in `.warp/version`. When that copy is older than the plugin you ran, it prints `plugin is vOLD, repo copy is vNEW: run /warp-uninstall then /warp-init`. To start clean, copy `.warp/` aside if you want the journal, run `/warp-stop`, then `/warp-uninstall` and confirm, reload Cursor, and run `/warp-init`. See the README upgrade section.

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
