# Commands

One reference for the chat commands and the scripts they run. Config keys and defaults are in [CONFIG.md](CONFIG.md). How agents start, how they end, and how a run is torn down is in [LIFECYCLE.md](LIFECYCLE.md). Every script below accepts `?`, `help`, `-h`, and `--help`. `?` is always help, including where a value would go. A bare `help` is help unless it is the value of an option (`--folder help` is a folder name). Quote `?` if the shell expands it.

`<plugin>` is the plugin Cursor loaded (often `.cursor/plugins/warp` after init).

## Quick map

| Chat | What it does | Script and common flags |
|---|---|---|
| `/warp-init` | Copy the plugin, backfill config, and allow Warp's MCP tools | `install.py init` `--channel` `--dry-run` `--no-allow` |
| `/warp-scan` | Read a plan into the beam and resolve Jira keys | `scan.py scan` `--folder` |
| `/warp-start` | Set the run running, from a stopped run, a paused run, or a run that is already running. The same command as `/warp-resume`. Loads main's beam on a fresh checkout, prints `run: was ...`, `unfinished: ...`, and `parent: session <id>`, and starts unfinished work before any new ticket. This turn is the parent of the run | `scan.py start` `--reason` `--force` (runs `beam.py watchdog`), then passes of `orchestrator.py supervise` `--beam` and `orchestrator.py parent-exit` `--beam` `--wait` `--session` |
| `/warp-pause` | End every agent, keep the work, then push that state. The same teardown as `/warp-stop`, without the completion report. Carry on with `/warp-start` or `/warp-resume` | `scan.py pause` or `beam.py pause` `--reason`, then `inbound.py release`, then `update_state.py` `--beam`, then `orchestrator.py parent-exit` `--beam` |
| `/warp-resume` | The same command as `/warp-start`: the same prompt gate, the same output, and the same `--force`, from a paused run, a stopped run, or a run that is already running. A ticket that had a Shuttle out gets a new one | `scan.py resume` `--reason` `--force` (runs `beam.py watchdog`), then passes of `orchestrator.py supervise` `--beam` and `orchestrator.py parent-exit` `--beam` `--wait` `--session` |
| `/warp-stop` | The same teardown as `/warp-pause`: end every agent, keep the work, push that state. Then write the completion report and record `stoppedAt`. Carry on with `/warp-start` or `/warp-resume` | `scan.py stop` `--reason`, then `inbound.py release`, then `update_state.py` `--beam`, then `orchestrator.py parent-exit` `--beam` |
| `/warp-list` | Show what this run still has out, running or idle. Changes nothing. `--scan` lists every agent the key can see that matches the words, in any repo | `agents.py list` `--beam` `--tag` `--untagged` `--all-idle` `--any-repo` `--scan` |
| `/warp-cleanup` | Cancel the run of each agent `/warp-list` shows as running, and archive all of them. Without `--apply` it is a dry run. `--scan` archives idle word matches and leaves running ones unless `--force` | `agents.py cleanup` `--beam` `--cloud` `--apply` `--running` `--force` `--tag` `--untagged` `--all-idle` `--any-repo` `--scan` |
| `/warp-update-state` | Load main and `.warp/tickets/<id>/`, keep parent pause, stop, and claims, push the beam | `update_state.py` `--beam` `--root` |
| `/warp-sync` | Read Jira into the beam. Dry-run unless `--apply`. Does not dispatch | `jira_sync.py sync` `--beam` `--jira` `--prs` `--apply` `--force` `--id` |
| `/warp` | One tick of the master loop, including the watchdog | the `warp` skill, `beam.py watchdog`. Not a script flag. |
| `/warp-status` | List every ticket that is not merged, alarms and stalls first, and rewrite status and the board | `scan.py status`, `beam.py board` |
| `/warp-status-post` | Post that digest | `status_post.py` `--beam` `--out` |
| `/warp-export` | Write the plan files for an outside edit | `scan.py export` `--beam` `--out` |
| `/warp-import` | Replace the plan. The run stays stopped | `scan.py import` `--plan` `--keep-status` |
| `/warp-ingest` | Build the beam from a schedule you name | `beam.py ingest` |
| `/warp-jira-check` | Keyed or unmapped, what is missing, one fix | `jira_sync.py verify`, `catchup` |
| `/warp-jira-view <key-or-id>` | Every field on one issue | `jira_view.py` `--comments` `--links` `--all` `--full` `--verbose` `--json` |
| `/warp-jira-match` | Match summaries. Optionally write External ID | `jira_match.py` `--apply` `--yes` `--write-external-id` |
| `/warp-jira-external-id` | Write mappings into External ID, or a label if that field is missing | `jira_external_id.py` `--apply` `--yes` `--create-field` `--recheck` `--force` `--ticket` |
| `/warp-jira-map` | Review or set plan id to issue key | `jira_sync.py map` `--set` `--import` `--from-jira` |
| `/warp-allow-notify` | Allow specific MCP tools | `allow_notify.py` `--dry-run` `--list` `--check` `--server` `--allow-server-tools` `--with-jira` `--with-git` |
| `/warp-proceed <id>` | Merge one green manual ticket and move Jira to Done | `proceed.py` `--beam` `--by` |
| `/warp-report` | Completion report, or a partial snapshot | `report.py` `--beam` `--out` `--open` `--partial` |
| `/warp-version` | Installed copy, source copy, `.warp/version` | `version.py` |
| `/warp-upgrade` | Replace the project plugin copy. Leaves config and the beam | `upgrade.py` `--root` `--source` |
| `/warp-uninstall` | Delete `.cursor/plugins/warp` and `.warp/` after you confirm | `install.py uninstall` `--yes` `--remove-gitignore` |

`jira_sync.py` subcommands, each printed by `?`: `verify`, `catchup`, `sync`, `map`, `resolve`, `project`, `external-id`, `record-external-id`, `plan`, `pick`, `record`, `record-comment`.

The orchestrator is the only merger. Shuttles and Reed do not merge. `orchestrator.py` is the policy the master loop runs. It does not edit ticket product code. Ready means every dependency and `after` id is merged on the base branch, and no lock overlaps an in-flight ticket, including a parent folder. The slot frees when the provider check rollup is green. Locks stay until merge or park. The merge queue is serial: rebase, check, merge, delete the branch, dispatch. Sizes outside `autoMergeSizes` still wait for `/warp-proceed` or `warp:proceed`. A red base branch stops merging. `queue --beam` prints the next merge candidate. `fail --beam --id --output` records a red check and parks once attempts reach `maxFixAttempts` (default 5). `rebuild --beam --facts` classifies `merged`, `in-flight`, and `pending` from git. `resolve --path --append-only --base --ours --theirs` keeps both sides for `appendOnlyPaths` and prints `send-back` for any other path. `dispatch --beam` recomputes pending gates, then prints starts after a freed slot, a merge, or a failure. A pending or stale-red gate turns green when every member is merged or done and no check is actually red, then the tick runs. G1 with members P-002, P-003, P-004, and P-018 stores `members merged: P-002, P-003, P-004, P-018`. The line is `G1 pending cleared. Members merged. Tick ran.` `record --beam --id --plan --result` stores the plan record and the result record. `checkCommand` empty means Bugbot and CI as configured. `make ci` is kept as the full check command (the space stays) and the result is stored on `pr.check`. A red `make ci` prints `send-back <id> fix` and a `start` line with the log. A green result does not. Past `maxFixAttempts` (default 5) the ticket parks. The name alone is not a red check. The cap is `maxAgents`. A project may set 18. `mergeQueue` defaults to false.

## Run control

Four commands, two behaviors.

`/warp-start` and `/warp-resume` are the same command (`scan.py start`, `scan.py resume`). Either one sets the run running, from a stopped run, a paused run, or a run that is already running. It prints which: `run: was stopped. Starting.`, `run: was paused (<reason>). Continuing.`, or `run: was already running. This session takes it over.` Both run the same prompt gate, and `--force` means the same on both. `/warp-start` on a paused run is not refused. There is nothing to choose between them.

`/warp-pause` and `/warp-stop` are the same teardown. One thing differs: stop also writes the completion report and records `stoppedAt`.

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

Start and resume check for unfinished work before anything new, and print it:

```text
unfinished: 6 open. 1 need a fix, 3 need a Shuttle, 0 have a Shuttle out, 2 wait on review or checks. These start first, up to maxAgents 18. New tickets start after, while fewer than maxInProgress 20 are open.
```

The order is fixed: open pull requests that need a fix (a conflict, CI that never started, red CI, Bugbot findings), then tickets whose Shuttle was halted or died, then new tickets from the ready queue. `unfinished: none. New tickets start from the ready queue.` means nothing was left open.

There are two caps. `maxAgents` (default 18) is how many agents run at once: every Shuttle, whatever its step (implement, fix, rebase, rerun, restart, repair), in a worktree on this machine or on its own VM. `maxInProgress` (default 20) is how many tickets are open at once: started, and not merged or parked. A ticket waiting on review or CI is open and uses no agent. A fix, a rebase, or a relaunch takes a `maxAgents` slot, and it starts even when `maxInProgress` is full, because its ticket is already open. `maxInProgress` holds only new tickets. Unfinished work draws on the slots first, so a new ticket starts only in a slot unfinished work did not need. `memoryCheck: true` lowers `maxAgents` on a shared machine when memory is tight. Start, status, and `/warp-version` print both caps on one line: `effective: runner=<r> subagentVm=<b> memoryCheck=<b> maxAgents=<n> maxInProgress=<n> launch=<l>`. A config that still has a retired key prints `config: <key>: <value> is retired and ignored.` and what replaced it, at start and in `/warp-version`. The retired keys are listed in [CONFIG.md](CONFIG.md).

Plugin hooks do not run on cloud runners. `/warp-start`, `/warp-resume`, and `/warp-status` print the resume hint from `scripts/resume_hint.py` (`--root`, `--beam`). Follow it. It does not dispatch. `/warp-stop` (`scan.py stop`) and `/warp-pause` (`scan.py pause` or `beam.py pause`) append a `session-stop` line through `scripts/session_note.py --type session-stop`. A Shuttle finishes with `scripts/session_note.py --type subagent-stop`. A second note of the same type is skipped while it is still the last journal line. Local hooks in `hooks/hooks.json` only repeat those scripts. Nothing depends on a hook. `hooks/subagent-start.sh` is a local IDE duplicate of the gate in `checkout.py launch`, and `WARP_SPAWN_GATE=off` turns it off.

`/warp-list` and `/warp-cleanup` are the list and the clear, and they take the same selection. `/warp-list` (`agents.py list --beam .warp/beam.json`) shows what this run still has out, running or idle, and changes nothing. `/warp-cleanup` (`agents.py cleanup --beam .warp/beam.json --cloud --apply --running`) cancels the run of each one that is running and archives all of them. It is the clear you run yourself. It never runs on its own. Set `CURSOR_API_KEY` in the environment and never commit it. Leave `--apply` off for a dry run: `agents.py cleanup --beam .warp/beam.json --cloud` prints `cleanup: tag [warp:<instance>]`, then each match with id, name, repo, status, last update, and link, `cleanup: would cancel <id>` for the running ones, and `cleanup: count`. It changes nothing. `--cloud` is `GET /v1/agents`. `--apply` is `POST /v1/agents/{id}/archive`. Archive is reversible in the Cursor UI. Warp never deletes an agent. A match is in this checkout's origin repository, and it is in `.warp/agents.json` or its name or prompt starts with this run's `[warp:<instance>]` tag. Other agents in the repo are not matched, whatever words are in their names, and neither are another Warp run's. An IDLE match is archived (`cleanup: archived <id>`). The agent running the command stays (`cleanup: skip <id> reason=self`). This run's parent stays (`reason=parent`). A RUNNING or ACTIVE match has its latest run cancelled with `POST /v1/agents/{id}/runs/{runId}/cancel` (`cleanup: cancelled <id>`) and is archived. Without `--running` it stays (`reason=running`). Any other status stays (`reason=status`). While this run is running, the command archives the idle ones, leaves the running ones, and prints `cleanup: --running refused while the run is running. /warp-pause or /warp-stop first.` `--force` overrides that refusal and cancels live work. `--tag <instance>` names another run's tag in place of this one: `a1b2c3`, `warp:a1b2c3`, and `[warp:a1b2c3]` are the same. `--tag all` is the tag of any Warp run, and `--tag '*'` is the same. `--untagged` also matches agents with no Warp tag, from before 1.5.0, loosely, on Warp role words in the name or prompt. One of those that is still running is left alone (`reason=running-untagged`) unless `--force`, because it may be someone else's. `--all-idle` includes every IDLE agent in this repo. `--any-repo` does not limit to this repo. `--scan` ignores the repo filter and the tag and registry rules. It matches one or more words, case-insensitive, in the name, the summary or description, and the first prompt, and it pages through every agent the key can see. `--scan="Jira","Fix","Bugbot"`, `--scan Jira,Fix,Bugbot`, and `--scan Jira --scan Fix` are the same. Each list match prints id, name, repo, status, updated time, `word=` and `field=`, and a link, then `scan: count`. `--scan` crosses repos, so check `/warp-list --scan` before `--apply`. A dry run prints `cleanup: would archive <id>` for an idle match and `cleanup: would cancel <id>` for a running match that `--force` would cancel. With `--apply`, idle matches are archived. A RUNNING or ACTIVE match is left (`reason=running-scan`) unless `--force`, which cancels it and then archives it. The refusal while the run is running still applies. Archive only. Warp never deletes an agent. The agent running the command (`reason=self`) and this run's parent (`reason=parent`) are never matched. Without the key, the command prints `cleanup: link https://cursor.com/agents/<id>` for each recorded cloud agent. A pile of about 199 unarchived Shuttles, Bugbot runs, and listeners from a run before 1.5.0 carries no tag: `/warp-stop`, then `/warp-list --untagged` to read it, then `/warp-cleanup --untagged`. If `/warp-list` or `/warp-cleanup` is not in the command list, run `python3 .cursor/plugins/warp/scripts/agents.py list --beam .warp/beam.json` or `python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running`. `start <id>` creates one Shuttle. `resume <id>` continues that Shuttle and does not create one. `shuttle: hold` means one is already out. A Shuttle is replaced only after its turn returned or its heartbeat is older than `staleMinutes`. `bugbot: request` is once per head commit. `bugbot: hold` means that commit was already requested. `listener: poll` means start one background listener and do not wait. `listener: hold reason=live` and `listener: hold reason=await-return` mean do not start a second. `listener: idle` and `listener: none` mean do not start a listener. `spawn: closed` and `spawn: cap` mean do not spawn. `/warp-pause` and `/warp-stop` end every registered agent (`stop: <id>`) and mark it halted, and with `CURSOR_API_KEY` they cancel and archive this run's cloud agents. Merge and park archive every cloud agent that ticket used. Nothing spawns while the run is paused or stopped, or every ticket is merged or parked. The listener and each Shuttle read `reap: exit <reason>` at the next reap check and return. The parent reads `parent: exit` and ends its turn. When every ticket is merged or parked, the same teardown runs once (`halt: done`).

`/warp-start`, `/warp-pause`, `/warp-resume`, and `/warp-stop` take `--beam` and `--reason`. Start and resume also take `--force`. Pause and stop are the same teardown. `scan.py pause` and `scan.py stop` set `runState`, end every row in `.warp/agents.json` (`stop: <id>`) and mark it halted, free every Shuttle slot, close the listener's poll, and print `halt: paused agents=<n>` or `halt: stopped agents=<n>`. The beam, the journal, and the registry are pushed to main before the command returns, so a Shuttle on its own VM reads the halt from there. With `CURSOR_API_KEY` in the environment, every cloud agent in the registry, and every agent in this repo whose name carries this run's `[warp:<instance>]` tag, has its run cancelled (`POST /v1/agents/{id}/runs/{runId}/cancel`) and is archived (`POST /v1/agents/{id}/archive`). The lines are `cleanup: cancelled <id>` and `cleanup: archived <id>`. The agent running the command and this run's parent are skipped. Without the key the command prints `cleanup: link https://cursor.com/agents/<id>` for each recorded cloud agent: archive those in the Cursor UI. `cleanup: cut short` means the teardown hit its time limit: run `/warp-cleanup`. Pause does not leave a Shuttle running. Each agent reads `reap: exit paused` or `reap: exit stopped` at its next reap check and returns `result: <id> stopped <reason>`. Branches, worktrees, pull requests, locks, and `jira.startedAt` stay. Nothing from before a halt is resumed. `/warp-start` or `/warp-resume` launches a new Shuttle with a new agent id on the same branch for each ticket that had one out (`fix: <id> resume after halt`), before any new ticket. That is not a recovery, and no one is told a worker died. Stop also writes the completion report (`.warp/warp-complete.html` and the Herald totals) and records `stoppedAt`. Nothing else differs from pause. A paused run and a stopped run both stay that way until the next `/warp-start` or `/warp-resume`. `beam.py pause` and `beam.py resume` set the run state through the same code as `scan.py pause` and `scan.py resume`, so the journal types are `paused` and `running` from either. `scan.py start` and `scan.py resume` print `parent: session <id>`. Keep that id for `orchestrator.py parent-exit --session`. One parent per run: a later start or resume takes the run over. `scan.py start` and `scan.py resume` run `beam.py watchdog`. Every `/warp` tick runs it before `ready()`. A Shuttle heartbeats with `beam.py heartbeat` (`lastSeenAt` and `--agent`), which also prints the reap line. The listener does not heartbeat. The parent writes `listener.parentSeenAt` from `parent-exit --wait` and from `supervise`. `inbound.py heartbeat` exists for a listener someone starts by hand. A run does not use it. A Shuttle is dead when that heartbeat is older than `staleMinutes` (default 15), or it never heartbeated and the claim is older than `staleMinutes`. A fresh heartbeat is left alone. Paused and stopped runs print `watchdog: skipped` and do not start a replacement. The listener is a Subagent of the parent. Start one subagent per ticket in its own worktree, in parallel up to maxAgents. It is not a separate Agent. The watchdog prints no listener line and never starts a listener. The repair sweep does not start one either. A stale heartbeat does not start another listener. The return stamp does. Close the window, open a new one, `/warp-start`. State comes from main plus ticket folders. A dead Shuttle prints `shuttle: replace <id>`: dispatch exactly one Shuttle for that same ticket. Status becomes `recovering`. Branch, pull request, `jira.startedAt`, and locks stay. Do not queue a duplicate. Past `maxRecoveries` (default 5) the line is `shuttle: alarm <id> worker-died` and no Shuttle starts. Herald posts each `herald:` line once. `<id> worker died. A new Shuttle started.` Plugin hooks do not run on cloud runners. A dead turn does not notify Warp. Cursor does not restart it. If the parent's turn dies, run `/warp-resume`. There is no process table.

`/warp-start` and `/warp-resume` fetch origin/main and load that beam when the checkout's beam is missing or empty, then patch in-flight tickets from `.warp/tickets/<id>/`. They do not claim a listener. The listener is one background subagent on the parent's checkout. `subagentVm` does not apply. When `orchestrator.py supervise` prints `listener: poll`, the prompt follows (`LISTEN`): the parent starts exactly one `warp-listen` Subagent in the background with those lines and does not wait. When `listenerModel` is unset the prompt says `model: inherit`. When it is set the prompt says `model: <slug>`. The listener is a Subagent of the parent. Start one subagent per ticket in its own worktree, in parallel up to maxAgents. It is not a separate Agent, and not one per ticket. `listener: hold reason=live` means a holder already has the lease: do not start a second. `listener: hold reason=await-return` means the lease was bumped and the return stamp is not in yet: do not start a second. `listener: idle` means paused, stopped, or finished. `listener: none` means no `slackChannel` or `teamsChannel` is set, so no listener is started at all. `/warp-listen` takes the lease in a chat you open yourself. `/warp-pause` and `/warp-stop` run `inbound.py release`, then `update_state.py`, so main shows paused or stopped before the command returns. Then `orchestrator.py parent-exit --beam .warp/beam.json` prints `parent: exit` and the turn ends. `/warp-update-state` fetches origin/main, loads that beam, patches each in-flight ticket from `.warp/tickets/<id>/` only (it does not copy the branch beam), applies pause, stop, claims, and runState the parent has that the remote beam does not, and pushes the reconciled beam, journal, and board. Tokens, cost, API keys, and webhook URLs are stripped. `.warp/config.yaml` is not committed. The summary lines are `pulled:`, `local won:`, and `commit:`. On every merge, and at the end of each parent tick, that same live beam is what lands on main. The listener must not keep reading while paused or stopped. It is one listener for the beam, not one per awaiting_approval ticket. The beam field is `listener`: `state` (`running` while a holder has the lease, `stopped` between generations), `generation`, `holder`, `parentSeenAt`, `returnReason` (`polled`, `rotated`, `orphaned`, or `stopped`), `agentId` (`listener`), `pollStartedAt`, `lastSeenAt`, `cursor`, `reads`, `lastCount`, `polls`, `startedAt`, and `stoppedAt`. Cursor cannot start it from a Slack message. There is no webhook. `/warp` is one tick of that loop (watchdog, reconcile, ready, claim) and does not implement a ticket. The turn that runs it is the parent: the only agent that starts another agent, and the only agent that waits. Each pass ends with the one wait, `orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>`. It blocks until a ticket folder changes, the inbound queue changes, the listener writes its return stamp, the run halts, or `pollSeconds` pass, and prints `pass: ticket <id>`, `pass: command`, `pass: return`, `pass: halt`, or `pass: timeout`. `pollSeconds` is that wait cap, not the channel-read interval. Then `parent: stay` means run the next pass now, `parent: exit` means end the turn and start nothing, and `parent: exit superseded` means a later `/warp-start` or `/warp-resume` owns the run. `/warp-status` rewrites `.warp/STATUS.md`, `.warp/status.json`, `.warp/BOARD.md`, and `.warp/board.html`. When `.warp/warp-complete.html` exists, the status footer names it. Working rows include `bugbot=` and `ci=` for manual tickets as well as auto-merge. `/warp-status-post` posts that digest. `/warp-export` writes `.warp/WARP_PLAN.md` and `.warp/WARP_PLAN.json`. `/warp-import` replaces the graph from `--plan`, keeps status, agent, branch, attempts, tokens, minutes, alarm, the pull request, and `jira` for ids that still exist (`--keep-status`, the default), and leaves the run stopped. Heartbeats (`lastSeenAt`) are not copied. `/warp-ingest` builds a beam from a schedule and does not dispatch. `/warp-proceed <id>` is `warp:proceed` for one manual ticket that is already `awaiting_approval`, with Bugbot pass and green CI on the beam. The token may be the plan id (`WV-01`), the Jira key (`WAR-1`), or a `#` number (`#01`). A ticket that is not awaiting approval is refused: the reply names the current status and nothing is merged. There is no `--force`. After the merge, Jira moves to Done unless `jiraDoneOnManualMerge` is false.

## /warp-report

Writes `.warp/warp-complete.html`. `reportOnComplete` (default true) also writes it from `beam.py set` when no ticket is queued or active, and from `scan.py stop`. `false` leaves the write to this command.

```bash
python3 <plugin>/scripts/report.py --beam .warp/beam.json
python3 <plugin>/scripts/report.py --beam .warp/beam.json --partial
python3 <plugin>/scripts/report.py --beam .warp/beam.json --out .warp/warp-complete.html --open
```

A run that still has queued or active tickets exits 2 and prints `not complete; pass --partial`. It does not write the file. `--partial` writes a snapshot with a Partial banner. `--open` prints a `file://` URL. `reportPath` is a second copy when it is not already `.warp/warp-complete.html`.

Waves are concurrency-run segments. A ticket is active from the status event that enters the working set until a status event leaves it. The interval is half-open. Each stretch between two boundaries has one set of active tickets. An empty set is drawn on the concurrency chart and is not a wave, and it splits waves so the same tickets resuming later are a new wave. Adjacent stretches with the same set are one wave. A handoff that keeps the count the same but changes who is running is two waves. Dependency levels are not used.

`beam.py set` appends a `status` event (and `bugbot`, `ci`, `alarm` when those change) to the ticket and to `.warp/events.jsonl`. The first status that leaves `queued` sets `runStartedAt`. Usage is recorded with `beam.py usage` or on `set`:

```bash
python3 <plugin>/scripts/beam.py usage --beam .warp/beam.json --id WV-01 --tokens-in 1000 --tokens-out 200 --tokens-cached 50 --cost 1.25
python3 <plugin>/scripts/beam.py set --beam .warp/beam.json --id WV-01 --tokens-in 1000 --tokens-out 200 --cost 1.25
```

Pass the totals the Cursor run reported for that ticket. Each flag replaces the previous value. If the run did not report usage, do not call this and do not invent numbers. The report shows n/a. `beam.py spend` remains the additive `tokens` and `minutes` total and is not tokens in, out, or cached. A non-green `--ci` increments `pr.ciRetries`.

## /warp-init

```bash
python3 <plugin>/scripts/install.py init
python3 <plugin>/scripts/install.py init --dry-run
python3 <plugin>/scripts/install.py init --channel eng-builds
python3 <plugin>/scripts/install.py init --no-allow
python3 <plugin>/scripts/install.py ?
```

Idempotent. A second run on a complete install changes nothing.

| Flag | Effect |
|---|---|
| `--root` | Repo root. Default is the git top level, else the current directory. |
| `--channel NAME` | Channel for a fresh config, or for an empty channel. Lowercased. Letters, digits, `-`, `_`, max 80. |
| `--dry-run` | Print the steps. Write nothing. |
| `--no-allow` | Do not write the project MCP allowlist. `autoAllowTools: false` does the same on every init. |

Steps: create `.cursor/plugins` and `.warp` if missing; copy plugin files that are not already there (existing files are never overwritten); copy `assets/config.example.yaml` on a fresh config, or append missing keys with their defaults and comments; set an empty `slackChannel` or `teamsChannel` (or the old `Warp` default) to `warp`; detect `gitProvider` from `origin` unless it is already `github` or `bitbucket`; set `pushMerge: false` when there is no remote; write the project MCP allowlist when `autoAllowTools` is true (Slack, Teams, Jira, and GitHub `add_issue_comment` when the provider is GitHub); append the gitignore snippet once; write `.warp/version` from the project plugin copy. User-level `~/.cursor` files are not written. Undo the allowlist with `/warp-allow-notify --revoke`.

If `.cursor/plugins/warp` is older than the plugin this command ran from, it prints `plugin is vOLD, repo copy is vNEW: run /warp-upgrade` and does not replace plugin files. `/warp-upgrade` is the command that replaces them. An old `.gitignore` line that is exactly `.warp/` is replaced with the snippet that commits the beam, journal, STATUS, and BOARD and still ignores `.warp/config.yaml` and token files. Other gitignore lines stay.

Commented lines in the example (`# pushMerge: false`, `# runner: local`, `# baseBranch: "develop"`, `# gitProvider: github`, `# ghCli: false`) are hints. A line that starts with `#` is not a key.

When init changes something and `notify` is `verbose`, it writes `.warp/notify-post.json`. The footer starts with `Warp v<version>`.

## /warp-scan

```bash
python3 <plugin>/scripts/scan.py scan --root . --out .warp/beam.json
python3 <plugin>/scripts/scan.py scan --folder HOS/spec
python3 <plugin>/scripts/scan.py scan --folder spec
```

| Flag | Default | Effect |
|---|---|---|
| `--root` | `.` | Repo root. |
| `--folder` | whole repo | Path or name under the root. |
| `--out` | `.warp/beam.json` | Beam path. |
| `--max-agents` | `18` | Stored on the beam. The live cap is `maxAgents` in config. |
| `--model` | `claude-sonnet-5-5-high` | Stored on the beam. The live slug is `model` in config. |

Folder rules:

| Argument | Result |
|---|---|
| An existing path under the repo | Used as given. |
| A name that matches one folder | That folder. |
| A name that matches several folders, only one with plan files | That folder, and a note listing the others. |
| A name that matches several folders, none or more than one with plan files | Nothing is scanned. The matches are listed (exit 3). |
| No such folder, or a path outside the repo | Error. Nothing is written. |
| A folder with no plan files | `no plan found` (exit 2). |

With no folder, plans in more than one folder are reported and the richest plan is used.

Looks for `WARP_PLAN.json`, `schedule.json`, `CURSOR_PLAN.md`, a Jira ticket JSON export, or a markdown table with `id` and `deps`. Writes `.warp/beam.json` and `.warp/scan.json`. `runState` is `stopped`. A successful scan writes `.warp/notify-post.json` with the header `Warp | <repo> / <project>` and a footer that starts with `Warp v<version>`. `notify: quiet` posts nothing. No channel, or no server: the text goes to `.warp/outbox.md`. A failed scan posts nothing.

Jira keys already on the plan, in a Jira export, or in `.warp/jira-map.json` / `jiraKeyMap` are stored during the scan. The script then prints a line like `12 tickets: 9 keyed, 3 need mapping`. For the ones still open it writes `.warp/jira-resolve.json`. When Jira is connected, the agent looks up each plan id in this order: `jiraExternalIdField` (a name or `customfield_NNNNN`), then External ID, External Id, ExternalId, External Key, Plan ID, and Ticket ID, then a label `warp:<id>`, then a remote-link id. A Jira import file (`externalId`, `h2. Size`, `h2. Locks`, `h2. Blocked by`, `h2. Acceptance`, labels `size:S`, `auto-merge`, `area:*`) uses `externalId` as the plan id. It is not stored as `jiraKey`. `jira_sync.py resolve --ticket WV-01 --key WAR-1 --issue-id 10001 --cloud-id <cloudId>` records the lookup before any transition. `jira_sync.py resolve --apply` stores an exact single match and where it came from. A summary match is only a proposal. That is part of `/warp-scan`, not a separate command. `/warp-jira-map` is only for tickets that stay unmapped or ambiguous.

Other `scan.py` subcommands: `export` (`--beam`, `--out`), `import` (`--plan`, `--beam`, `--keep-status`), `status` (`--beam`), `bundle` (`--beam`, `--out` default `.warp/warp-review.zip`), `start`, `stop`, `pause`, `resume` (`--beam`, `--reason`).

## /warp-uninstall

```bash
python3 <plugin>/scripts/install.py uninstall
python3 <plugin>/scripts/install.py uninstall --remove-gitignore
python3 <plugin>/scripts/install.py uninstall --remove-gitignore --yes
```

Without `--yes` it only lists. With `--yes` it deletes `.cursor/plugins/warp` and `.warp/`, and the project allow-notify entries recorded in `.cursor/warp-allow.json`. `--remove-gitignore` removes only the snippet init added. A hand-written `.warp/` line is left alone. It does not touch product code, branches, pull requests, a `stateDir` outside the repo, a user-level plugin, or `~/.cursor` allow-notify files.

## Messages

Herald posts through Slack or Teams MCP. There is no webhook. The header from `scripts/herald_fmt.py` is `Warp | <repo> / <project> | warp:<instance>`. `<project>` is `projectName`, else `jiraProject`, else the folder name, and it is omitted when it equals the repo. `<instance>` is six characters that name one Warp run, which is one beam. `/warp-start` sets it once and it stays on the beam. On a cloud runner it is the last six characters of the parent's cloud agent id. On a laptop it is six characters of a hash of the machine id and the beam's path, so two checkouts on one machine differ. A `/warp-resume` from another window or VM keeps it. It is the tag this run's agents carry in their names, so two Warp runs in one channel are told apart. Before the first `/warp-start` the header ends at the project. The status post adds the fact `Instance: warp:<instance> on <host>`. Init and scan footers start with `Warp v<version>`.

`notify: verbose` posts init, scan, claims, and ticks. `notify: quiet` posts alarms, approval waits, a red gate, and pause/stop. `warp:status` is always answered. `messenger` is `slack`, `teams`, or `both`.

If the channel is empty or the server is missing, the text is appended to `.warp/outbox.md` and the command still succeeds.

## GitHub, Bitbucket, and local-only

`scripts/provider.py resolve` reads `gitProvider` (`auto`, `github`, `bitbucket`). `auto` uses the `origin` host. `/warp-init` writes the host it finds and leaves a custom value alone. Another host, no `origin`, or `pushMerge: false` is local-only: no push, no pull request, no provider call. Reed merges the ticket branch into `baseBranch` with `provider.py merge-local`.

| Key | Default | Role |
|---|---|---|
| `githubMcp` | `github` | GitHub server name. |
| `bitbucketMcp` | `bitbucket` | Bitbucket server name. |
| `ghCli` | `true` | GitHub only: try `gh` when it is already installed and logged in. Warp never installs it. |
| `pushMerge` | `true` | `false` forces local-only. |
| `baseBranch` | empty | Branch tickets start from and merge into. Empty uses origin's default, then `main` or `master`. |
| `runner` | `cloud` | `cloud` is a Cursor cloud agent VM. `local` is this machine. |

`provider.py merge-local --id --branch` squash-merges locally. `provider.py note --reason` writes the outbox line when the run went local.

## Jira

Tool names are in `scripts/mcp_tools.py`. Server name is `jiraMcp` (default `atlassian`), not a tool.

| Tool | Use |
|---|---|
| `getAccessibleAtlassianResources` | `cloudId`, unless `jiraSite` is set. One site is stored in `jiraSite` |
| `getVisibleJiraProjects` | Project list used to fill an empty `jiraProject` |
| `getJiraIssue` | current status. `/warp-jira-view` asks for every field (`fields ["*all"]`, `expand names`) |
| `getTransitionsForJiraIssue` | transitions offered now. Some servers call this `listJiraIssueTransitions` |
| `transitionJiraIssue` | the id `jira_sync.py pick` chose |
| `addOrEditJiraIssueComment` | comment, argument `commentBody`. Older servers call this `addCommentToJiraIssue` |
| `searchJiraIssuesUsingJql` | Scan and claim: exact external-id match, then a `warp:<id>` label. One hit is stored. Summary search waits for `--yes` |
| `getJiraProjectIssueTypesMetadata` | Field names and ids, so `jiraExternalIdField` or External ID can be queried as `cf[NNNNN]` |
| `getJiraIssueRemoteIssueLinks` | Remote-link ids, checked after the external-id field and the label |
| `editJiraIssue` | Write the plan id into the External ID field, or add a label with `update.labels` add (never `fields.labels`, which would replace every label). `/warp-jira-external-id --apply --yes`, `/warp-jira-match --write-external-id --yes`, or `jira_sync.py external-id --yes`. Also `/warp-scan` and `/warp-jira-check` when `jiraWriteExternalId` is true (that flag is the consent, so `--yes` is not required). Not a comment. |
| `getJiraIssueEditmeta` | Whether that field exists and is editable on the issue. A missing field falls back. A read-only field is skipped. |
| `getJiraScreen` | Read a company-managed screen. Used only when trying to attach an External ID field that already exists. |
| `updateJiraScreen` | Add an existing field to a screen. It does not create a field. Returns a plan until commit. |
| `createJiraIssueRemoteIssueLink` | Add a remote link whose globalId is `warp:<id>` when `jiraExternalIdFallback` is `remote-link`. |
| `createJiraField`, `createCustomField`, `createJiraCustomField` | Probes. Not in the Rovo catalog (30 Sep 2026). `/warp-jira-external-id --create-field --yes` looks for one of these names and does not invent a call. |

| When | Jira | Pull request |
|---|---|---|
| Claim | In Progress (`jiraInProgressStatus`) and a comment, if `jiraTransition` is true and the ticket has a key | none |
| Bugbot requested | In Review (`jiraInReviewStatus`). When Bugbot is off, this happens when the pull request opens instead. Empty `jiraInReviewStatus` skips it. A fix send-back stays here | none |
| PR opened | comment with the link. Also the In Review move when Bugbot is off | connected mode only: GitHub `add_issue_comment` or `gh pr comment`. Bitbucket uses the comment tool on `bitbucketMcp` (unnamed here). |
| Bugbot / CI | comment | connected mode only |
| Waiting (`autoMerge` false, sizes not in `autoMergeSizes`) | After Bugbot pass and CI green: QA Ready (`jiraQaReadyStatus`) and a comment, "Bugbot clean, ready for manual review", with the findings-fixed count | connected mode only |
| Merged, auto | Done (`jiraDoneStatus`) and a comment | connected mode only |
| Merged, manual | Done (`jiraDoneStatus`) and a merged comment, unless `jiraDoneOnManualMerge` is false (then the comment only, and the issue stays at QA Ready) | connected mode only |
| Release to queued | no move, unless `jiraRestoreOnRelease` is true | none |
| Pause / stop | no change | none |

A plan id is not a Jira issue key. `WV-01` is not sent as `WV-01`. A key is confirmed when it is on the plan or export, in the map file, its project prefix matches `jiraProject` or `jiraKeyPrefixes`, or Jira has exactly one issue whose external id, `warp:<id>` label, or remote-link id equals the plan id. Otherwise `jiraKey` stays empty and the ticket is `needs mapping`. `/warp-scan` prints `N tickets: K keyed, U need mapping` and, when Jira is connected, resolves those matches before that summary is final. A claim on an unmapped ticket tries that search first. Only a miss or an ambiguous result writes `.warp/outbox.md` and a Herald payload, and it does not call `transitionJiraIssue`. Two Jira issues are reported and neither key is stored. A key set by hand is never replaced.

The first claimed ticket of a run is the one claimed while no ticket has `jira.startedAt` (no earlier ticket was linked and moved to In Progress). If Jira returns not found (`record --result not-found`), or that lookup cannot resolve a real issue key, Warp releases the claim to `queued`, clears `agent`, `branch`, `jira.startedAt`, and `jira.previousStatus`, and stops the run the same way as `/warp-stop`. Herald posts one message, `Run stopped because Jira issues are not linked (WV-01 / WAR-1). Fix jiraProject, /warp-jira-match, or /warp-jira-external-id, then /warp-resume.` It does not also post that the claim still stands. A later miss, after one ticket has `jira.startedAt`, is a per-ticket alarm: the claim stays and the run continues. `jiraTransition: false` does not stop the run.

`/warp-jira-check` runs `jira_sync.py verify` and `catchup`. `verify` with no flags only prints. It does not call Jira, and `needs mapping` on this report does not mean the external-id search already ran. Each ticket has `status: keyed` or `status: unmapped`, `source` (`plan`, `export`, `map`, `manual`, `external`, `label`, `link`, `summary`, `inferred`, or none), and when unmapped a `reason` plus one `fix` command. When every ticket is unmapped the report ends with `why nothing linked:` (`jiraProject is empty`, map keys never copied onto the beam, or no ticket has a Jira key in the plan, map file, or external id). The same line names `jiraMcp`, which must match the Atlassian server Cursor shows. The script cannot tell whether that server is connected. A ticket whose beam status is `merged` and whose `jira.doneAt` is empty is `merged-but-not-done`. `verify` prints that line and the exact `catchup --beam --id` command. `catchup` then prints the Done transition, the merged comment, and the same command.

```bash
python3 <plugin>/scripts/jira_sync.py verify --beam .warp/beam.json
python3 <plugin>/scripts/jira_sync.py verify --link
python3 <plugin>/scripts/jira_sync.py verify --link --dry-run
python3 <plugin>/scripts/jira_sync.py verify --apply results.json
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json
```

| Flag | Effect |
|---|---|
| (none) | Print the report. Write nothing. |
| `--link` | Copy a key already in `.warp/jira-map.json` or `jiraKeyMap` onto a ticket with no confirmed key. Write `.warp/jira-resolve.json` (same JQL as `map --from-jira`). Do not store a Jira search. |
| `--results FILE` | Store one exact match from a saved transcript. `--apply FILE` is the same. |
| `--dry-run` | Print the map copy and the Jira match. Write nothing. |
| `--id ID` | One ticket. |

`catchup` writes `.warp/jira-todo.json` for tickets that have a key, and refuses an active ticket that still has none. `catchup --write` stores an inferred key only when the prefix matches. After a key is mapped, `catchup` asks for the transitions and comments the current beam status still owes (In Progress for a claim, QA Ready or Done for a merge). It does not move the ticket backwards. Linking itself happens when `/warp-scan` or the first claim prints `jira: RESOLVE` and the agent runs that search, or when this check is run with `--link` and then `--apply`.

When `jiraWriteExternalId` is true, `catchup` also prints `MUST DO write External ID` for every mapped ticket whose External ID is not yet confirmed equal to the plan id. That config flag is the consent. `--yes` is not required. A key whose source is `external` is already equal and is skipped. A ticket with no confirmed key is skipped. The agent calls `editJiraIssue` for each line, then `jira_sync.py record-external-id --results`. A missing or read-only field sets `jira.externalIdAttempt` and `/warp-jira-check` prints `externalIdAttempt: skipped — field missing`. That line does not replace `lastAttempt`. `/warp-jira-external-id` is the same write when you want it without waiting for a scan.

## /warp-jira-view

Print every field on one Jira issue. The argument is an issue key (`WAR-1`) or an external id / plan id (`WV-01`). The script does not call Jira. It writes `.warp/jira-view.json`. The agent calls `getJiraIssue`, `getTransitionsForJiraIssue`, and `searchJiraIssuesUsingJql` on `jiraMcp`, then `jira_view.py --results` renders the transcript. Those read tools are already included in `/warp-allow-notify --with-jira`.

A value is an issue key when its prefix is `jiraProject` or `jiraKeyPrefixes`, or when those are empty and it matches `PROJECT-123`. Otherwise it is an external id. A key is fetched with `getJiraIssue`. If that call fails, the same text is looked up as an external id. An external id is taken from the beam and `.warp/jira-map.json` first, then the claim lookup: `jiraExternalIdField`, then the other external-id names, then label `warp:<id>`, then a remote link. The search is limited to `jiraProject`. When `jiraProject` is empty, each visible project is probed. Two matches are listed and neither issue is printed.

```bash
python3 <plugin>/scripts/jira_view.py WAR-1
python3 <plugin>/scripts/jira_view.py WV-01
python3 <plugin>/scripts/jira_view.py WV-01 --results view.json
python3 <plugin>/scripts/jira_view.py WAR-1 --results view.json --comments --links --verbose --all --full --json
```

| Flag | Effect |
|---|---|
| (none) | Classify the argument and write `.warp/jira-view.json`. Print the tool calls. Write no issue fields. |
| `--results FILE` | Render a saved transcript. |
| `--comments` | Comment count and the latest comments. |
| `--links` | Issue links and remote links. |
| `--all` | Include empty and null fields. |
| `--full` | Do not truncate long text. |
| `--verbose` | Add the account id on a user field. |
| `--json` | Print the issue as JSON. |
| `--beam` | Beam path. Default `.warp/beam.json`. |

The report starts with `resolved: WAR-1 (external id)` or `resolved: WAR-1 (issue key)`, the status, the external-id field name and id, and `beam:` / `map:` lines for the stored key. `discovered field` is the id to set as `jiraExternalIdField` when the configured name is different. Custom fields print as `External ID (customfield_10050): WV-01`. Description and comments are plain text. Users are the display name. Labels, components, and versions are comma-joined. Dates are ISO. A field whose name is a token, password, or secret is `<redacted>`.

## /warp-jira-match

Link Warp tickets to Jira issues by summary, then optionally write the plan id into the External ID field. Dry-run is the default. The script does not call Jira. `summary ~` in Jira is fuzzy, so the agent fetches a bounded candidate list (50 issues, at most 2 pages) and the script matches locally.

Order for each unmapped ticket: exact summary (case-insensitive, punctuation and whitespace ignored), then the first `--chars` characters (default 60) when both summaries are at least that long, then a token/ratio score at or above `--min-score` (default 0.9) as a proposal only. `--all` includes tickets that already have a key; those keys are not replaced. A manual key is never overwritten. Done and closed issues are skipped unless `--include-done`. An issue already mapped to another ticket is skipped. Two matches, or one issue claimed by two tickets, are listed and not stored.

```bash
python3 <plugin>/scripts/jira_match.py
python3 <plugin>/scripts/jira_match.py --results candidates.json
python3 <plugin>/scripts/jira_match.py --results candidates.json --apply
python3 <plugin>/scripts/jira_match.py --results candidates.json --apply --yes
python3 <plugin>/scripts/jira_match.py --all --chars 60 --min-score 0.9 --include-done
python3 <plugin>/scripts/jira_match.py --write-external-id --yes
python3 <plugin>/scripts/jira_match.py --set-external-id WV-01=WAR-1 --yes --results edits.json
python3 <plugin>/scripts/jira_sync.py external-id --ticket WV-01 --key WAR-1 --yes
```

| Flag | Effect |
|---|---|
| (none) | Write `.warp/jira-match.json` with JQL. Print the searches. Store nothing. |
| `--results FILE` | Match a saved candidate list, or record an External ID edit. |
| `--apply` | Store one exact or prefix match on the beam and in `.warp/jira-map.json` (source `summary`). |
| `--yes` | Also store one fuzzy proposal. Confirms an External ID write. |
| `--all` | Report tickets that already have a key. Do not replace them. |
| `--chars N` | Prefix length. Default 60. |
| `--min-score N` | Fuzzy threshold. Default 0.9. |
| `--include-done` | Match Done and closed issues. |
| `--id ID` | One plan id. |
| `--write-external-id` | Prepare or record an External ID write for mapped tickets. |
| `--set-external-id ID=KEY` | One pair, for example `WV-01=WAR-1`. |
| `--force-external-id` | Replace a different non-empty External ID. |
| `--dry-run` | Print the match or the before/after and write nothing. |

`--apply` uses the same recording as `resolve --ticket`: the beam and `.warp/jira-map.json` are written together. `/warp-jira-map --from-jira` still tries the external id first, then this same summary comparison, and still waits for `--yes` before storing a summary hit.

`--write-external-id` is a write to Jira through `editJiraIssue`. It needs the connector permission from `/warp-allow-notify --with-jira` (`editJiraIssue` and `getJiraIssueEditmeta` are on that list). The field name comes from `jiraExternalIdField` and the field catalog. A missing or read-only field is skipped and the command still succeeds. Dry-run shows before and after. A different non-empty value is left alone unless `--force-external-id`. A second run sees `jira.externalIdWritten` and does not send the edit again. No Jira comment is added. `jiraWriteExternalId` defaults to false. When it is true, `/warp-scan` and `/warp-jira-check` queue the write for mappings that already exist, and `--yes` is not required. These flags work even when that key is false, once `--yes` is set. The bulk command for an existing map is `/warp-jira-external-id`.

## /warp-jira-map

Not a required step. `/warp-scan` and the first claim already store a key that is on the plan, in the map file, or an exact single Jira match on the external-id field, a `warp:<id>` label, or a remote link. Use this command to review the list, to set a leftover or an ambiguous ticket, or to override a key.

Resolution order for each ticket that is still open: explicit key in the plan or map file, then the external-id field in Jira, then a label or remote link, then a summary match (confirm before it is stored).

```bash
python3 <plugin>/scripts/jira_sync.py map
python3 <plugin>/scripts/jira_sync.py map --set WV-01=WAR-1
python3 <plugin>/scripts/jira_sync.py map --import mappings.csv
python3 <plugin>/scripts/jira_sync.py map --from-jira
python3 <plugin>/scripts/jira_sync.py map --auto --results results.json --dry-run
python3 <plugin>/scripts/jira_sync.py map --from-jira --results results.json --yes
python3 <plugin>/scripts/jira_sync.py map --search
python3 <plugin>/scripts/jira_sync.py map --match results.json --yes
python3 <plugin>/scripts/beam.py set --beam .warp/beam.json --id WV-01 --jira WAR-1
```

| Flag | Effect |
|---|---|
| (none) | Print each ticket id and its key, or `unmapped`. |
| `--set ID=KEY` | Store one pair on the ticket and in `.warp/jira-map.json`. Repeatable. |
| `--import FILE` | CSV (`id,jiraKey`), JSON (`{"WV-01": "WAR-1"}`), or a markdown table with `id` and `jira key`. |
| `--from-jira` | Look up unmapped tickets in a saved Jira search. `--auto` is the same flag. With no `--results`, print the JQL and write no keys. |
| `--auto` | Same as `--from-jira`. |
| `--results FILE` | Transcript `{"fields":[...],"searches":[{"jql":"...","issues":[...]}],"remoteLinks":[...]}`. One exact match is stored with its source and confidence. |
| `--dry-run` | With `--from-jira`, print the match and write nothing. |
| `--search` | Write `.warp/jira-search.json` with JQL for `searchJiraIssuesUsingJql`. Changes nothing. |
| `--match FILE` | Propose pairs whose Jira summary equals the ticket summary. |
| `--yes` | Store the proposals from `--match`, or a unique summary hit from `--from-jira`. Without it, a summary match is not written. |
| `--force` | Store a key whose prefix is not in `jiraProject` or `jiraKeyPrefixes`. |

`jiraProject` does not have to be typed by hand. `/warp-init` and `/warp-scan` write it when one prefix is clear. Plan ids such as `WV` are ignored when they never appear as a Jira key. Several plausible prefixes are not guessed.

```bash
python3 <plugin>/scripts/jira_sync.py project --list
python3 <plugin>/scripts/jira_sync.py project --set WAR
python3 <plugin>/scripts/jira_sync.py project --apply results.json
```

| Flag | Effect |
|---|---|
| (none) | Detect from plan files, branches, and recent commit subjects. Write `jiraProject` only when it is empty and one prefix wins. |
| `--list` | Print candidates. Write nothing. |
| `--set KEY` | Store that project key. Also fills an empty `jiraKeyPrefixes`. Replaces a previous value. |
| `--apply FILE` | Transcript from `getAccessibleAtlassianResources` and `getVisibleJiraProjects`. One project, or one match, is stored. One site is stored in `jiraSite`. Several projects write a probe instead of guessing. Issue keys in the transcript or already on the beam are enough on their own: every key in one project is stored even when `getVisibleJiraProjects` is missing. The line is `jira: set jiraProject to WAR (every stored key is in project WAR)`. |
| `--probe` | Write JQL that searches each visible project for the first plan ids (`externalId`, then `External ID`, then label `warp:<id>`). Writes nothing to `jiraProject`. |
| `--record FILE` | Probe transcript. Exactly one project with a hit is stored, with how it was found. Several hits are listed with issue keys and `project --set`. None lists every project. |

When every stored issue key is in one project, `/warp-scan` and `project --apply` write that key even if the project-list tool is missing. Do not run `project --set` after `jira: set jiraProject to WAR (every stored key is in project WAR)`. Keys in more than one project are named (`jira: jiraProject left empty. Stored keys are in ABC, WAR.`) and nothing is written. A different value already in the config is left in place, and the line says the matched issues differ. The same value is left in place. `project --set` remains the manual override when the keys do not agree.

When it stays empty, init, scan, and `/warp-jira-check` print `jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC)`.

`beam.py set --jira KEY` is the same check. It rejects a prefix that is not configured unless `--force`. A rescan keeps a key set this way, and reapplies `.warp/jira-map.json` and `jiraKeyMap`.

Plans can carry the real key: `schedule.json` field `jiraKey`, a markdown line `Jira: WAR-1`, a table column `Jira Key` or `jiraKey`, or `[WAR-1]` on the ticket heading.

```bash
python3 <plugin>/scripts/jira_sync.py resolve --ticket WV-01 --key WAR-1 --issue-id 10001 --cloud-id cloud-1
python3 <plugin>/scripts/jira_sync.py resolve --apply results.json
python3 <plugin>/scripts/jira_sync.py record --id WV-01 --event claim --result failed --error "transition rejected"
python3 <plugin>/scripts/jira_sync.py verify --beam .warp/beam.json
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json
python3 <plugin>/scripts/jira_sync.py catchup --write --id T-9
python3 <plugin>/scripts/jira_sync.py pick --target "In Progress" --transitions-file transitions.json
python3 <plugin>/scripts/jira_sync.py record --id T-9 --event claim --result moved
python3 <plugin>/scripts/jira_sync.py record-comment --id T-9 --where jira --event claim --comment-id 10001
python3 <plugin>/scripts/jira_sync.py record-external-id --results edits.json
```

`plan --event` is `claim`, `release`, `qa-ready`, or `done`. `record --result` is `moved`, `already`, `skipped`, `unavailable`, `no-transition`, `failed`, or `not-found`. `not-found` on the first claimed ticket releases the claim and stops the run. A later `not-found` does not. `record-external-id` stores each `editJiraIssue` result. A success sets `jira.externalIdWritten`. A skip sets `jira.externalIdAttempt` and does not clear a transition `lastAttempt`.

## /warp-jira-external-id

Write the plan id into Jira's External ID field for tickets that already have a confirmed key. Dry-run is the default. The script does not call Jira. Use this when `.warp/jira-map.json` and the beam already hold `WV-01 -> WAR-1` and the Jira field was never updated.

```bash
python3 <plugin>/scripts/jira_external_id.py
python3 <plugin>/scripts/jira_external_id.py --ticket WV-01
python3 <plugin>/scripts/jira_external_id.py --apply --yes
python3 <plugin>/scripts/jira_external_id.py --apply --yes --force --force-external-id
python3 <plugin>/scripts/jira_sync.py record-external-id --results edits.json
```

| Flag | Effect |
|---|---|
| (none) | List `WV-01 -> WAR-1: External ID currently <value\|empty\|unknown\|field missing> -> would set WV-01`. Write nothing. |
| `--apply --yes` | Print one `MUST DO write External ID` block. The agent calls `editJiraIssue` for each line. |
| `--dry-run` | List pairs even when `--apply` is set. Write nothing. |
| `--ticket ID` | One plan id. |
| `--force` | Queue a ticket that already has `jira.externalIdWritten`, including a key whose source is `external`. An equal live value is not an error. |
| `--force-external-id` | Replace a different non-empty External ID. The line shows before and after. |
| `--results FILE` | Record a saved transcript. With `--apply --yes` the beam is updated. |
| `--create-field` | With `--yes`, probe for a create-field tool. Site-wide admin change. Needs `--apply --yes` to queue the rest. |
| `--recheck` | Ignore `.warp/jira-field.json` and look the field up again. |

The summary is `jira: external id: would write N, already equal N, skipped N (ID reason)`. After `record-external-id` the verb is `written`. Skips are `no key`, `not editable`, and `different value`. A missing field is one summary line, then the fallback, not one skip per ticket.

Dry-run names the method on each line: `(method field)`, `method label`, or `method remote-link`.

When `.warp/jira-field.json` says the field is missing for this project and config, scan and this command queue the fallback only. `--recheck`, or a change to `jiraExternalIdField`, `jiraExternalIdFieldName`, `jiraProject`, `jiraCreateExternalIdField`, or `jiraExternalIdFallback`, looks again.

`jiraCreateExternalIdField` defaults to false. The one command that allows creation is `/warp-jira-external-id --create-field --yes`. If no create tool exists, or Jira denies it, the command still succeeds and uses `jiraExternalIdFallback`. `jira.externalIdWritten.method` is `field`, `label`, or `remote-link`. `/warp-jira-check` prints `jira mapping:`. `/warp-jira-view` prints `stored in Jira:`.

A key that came from the external-id search is already equal and is not queued. A ticket with no confirmed key is skipped. `jira.externalId` on the beam is the desired plan id, not a confirmation that Jira holds it. Idempotency is `jira.externalIdWritten`. The field is discovered with `getJiraProjectIssueTypesMetadata` and checked with `getJiraIssueEditmeta` on each issue. A missing or read-only field is skipped and the command still succeeds. No comment is added.

`/warp-scan` and `/warp-jira-check` print the same MUST DO block when `jiraWriteExternalId` is true. That flag is the consent, so those paths do not need `--yes`. This command needs `--apply --yes`, and it works when the config flag is false. `editJiraIssue` and `getJiraIssueEditmeta` are already on `/warp-allow-notify --with-jira`.

## /warp-allow-notify

Full behavior is also in the README. Summary:

```bash
python3 <plugin>/scripts/allow_notify.py ?
python3 <plugin>/scripts/allow_notify.py --dry-run
python3 <plugin>/scripts/allow_notify.py
python3 <plugin>/scripts/allow_notify.py --list
python3 <plugin>/scripts/allow_notify.py --check
python3 <plugin>/scripts/allow_notify.py --server plugin-slack-slack
python3 <plugin>/scripts/allow_notify.py --allow-server-tools
python3 <plugin>/scripts/allow_notify.py --with-git
python3 <plugin>/scripts/allow_notify.py --user --yes
python3 <plugin>/scripts/allow_notify.py --revoke
```

| Flag | Effect |
|---|---|
| (default) | Project files. Slack, Teams, and Jira tools. |
| `--dry-run` | Unified diff. Writes nothing, including no backup. |
| `--user` | `~/.cursor/permissions.json`, `cli-config.json`, and `hooks.json`. Refuses to write without `--yes`. `--dry-run`, `--list`, and `--check` do not need `--yes`. |
| `--with-jira` | Accepted. Jira tools are already included. |
| `--with-git` | Also GitHub `add_issue_comment`. No Bitbucket name. |
| `--list` | Detected MCP server names. Writes nothing. |
| `--check` | Covered and missing Warp tools, Run Mode caveat, entries that would fix the IDE prompt. Writes nothing. |
| `--server NAME` | Also allow the tools on this server id. Repeatable. |
| `--allow-server-tools` | Also write `server:*` and `*name*:*`. Every tool on that server, including destructive ones. |
| `--revoke` | Remove only what `.cursor/warp-allow.json` recorded. |
| `--root` | Repo root. |
| `--cursor-home` | Stand-in for `~/.cursor`. Tests. |

Default tool names come from `scripts/mcp_tools.py`: `slack_post_message`, `slack_send_message`, `slack_read_channel`, `slack_read_thread`, `slack_search_channels`, `send_channel_message`, `teams_send_message`, `teams_read_channel`, `teams_read_thread`, `teams_search_channels`, and the Jira tools in the table above. `lookupJiraAccountId` is not called. Each tool is also written as `user-<name>`, `project-<name>`, `plugin-<name>-<name>`, and `*<name>*:<tool>`. Extras go in `notifyAllow` as `server:tool`. The Run prompt shows the tool name when it is different.

IDE: `permissions.json` `mcpAllowlist` skips the prompt when Run Mode is Auto-review, Allowlist, or Run Everything. Ask Every Time ignores it. The key replaces the in-app MCP allowlist. CLI entries do not change the IDE button. Headless CLI uses those `Mcp(server:tool)` lines, or `agent -f` for one process. The local hook returns allow for this list only; a hook allow does not currently skip the prompt. Plugin hooks do not run on cloud runners. Cloud agents and automations do not prompt. They run `scripts/mcp_allow.py --server SERVER --tool TOOL` (`--root`, `--cursor-home`) and call the tool only when it prints `allow`. `ask` means do not call it. That is the same list, not every MCP tool, and it does not approve shell commands.

A second run with the same flags writes nothing. Changed files get a `.bak`. `/warp-uninstall` removes the project entries only.

## /warp-upgrade

```bash
python3 <plugin>/scripts/upgrade.py ?
python3 <plugin>/scripts/upgrade.py --root .
python3 <plugin>/scripts/upgrade.py --root . --source /path/to/warp
```

Run it in the repo that has Warp installed. It replaces `.cursor/plugins/warp` with the plugin tree this command is running from (`--source` names another tree). When that tree is a git checkout with an `origin` remote, the command fetches the default branch and copies that tree. If the fetch fails, it prints `fetch failed` and `keeping the installed copy`. The installed copy stays. It does not overwrite `.warp/config.yaml` or the beam. It prints `Warp vX.Y.Z`. Reload Cursor (Developer: Reload Window), then confirm with `/warp-version`. `--force` is not an upgrade flag. `?` prints `--root` and `--source`. A second run with the same source prints the same version.

`/warp-init` does not upgrade an existing copy. This command does. Init still copies only missing plugin files.

The command file is `commands/warp-upgrade.md` (`name: warp-upgrade`, plus `description`), the same shape as `commands/warp-version.md`, listed in `.cursor-plugin/plugin.json` with the other command files. The skill is `skills/warp-upgrade/SKILL.md` (`name: warp-upgrade`), the same shape as `skills/warp-version/SKILL.md`. Cursor's plugin command index keeps the paths it had when that index was published. Those two files were added after every indexed command and skill, they are in the plugin root, and listing them did not add them to that index. Init and upgrade write every `commands/*.md` file to `.cursor/commands/`, including `.cursor/commands/warp-upgrade.md`, so a reload lists `/warp-upgrade`, `/warp-update-state`, `/warp-status`, and the other commands when the plugin command index still omits that path.

The script does not need that slash command. `/warp-version` prints it when an older palette leaves the command out. A 1.4.6 tree includes `scripts/upgrade.py`:

```bash
python3 .cursor/plugins/warp/scripts/upgrade.py
python3 .cursor/plugins/warp/scripts/upgrade.py ?
```

If that file is missing, the project copy is not a full 1.4.6 tree. From a git checkout of https://github.com/smlsr/Warp at main, run the script with `--source` pointing at that checkout and `--root` pointing at the project, then Developer: Reload Window, then `/warp-version`.

## /warp-listen

The listener you open yourself. `inbound.py take --beam .warp/beam.json --holder chat` bumps the generation, so the copy the parent started exits `reap: exit superseded` at its next wake and does not read. Then follow `skills/warp-listen/SKILL.md` with that generation and holder. Same loop: `poll --lease` before any read, one channel read, acks, `wait`. Do not apply the queue and do not merge. The parent applies it on `pass: command`.

```bash
python3 <plugin>/scripts/inbound.py take --beam .warp/beam.json --holder chat
```

## /warp-start and the prompt gate

On `runner: cloud`, or `runner: auto` when `CURSOR_CLOUD`, `CURSOR_CLOUD_AGENT`, or `WARP_CLOUD_SESSION` is set, `/warp-start` checks the MCP allow list before it sets `runState` to running and before it claims. `/warp-resume` is the same command and runs the same check.

When `jiraTransition` is on, `transitionJiraIssue` and `addOrEditJiraIssueComment` must be present. When Slack notify is on (`messenger` is `slack` or `both`), `slack_send_message` must be present. A missing tool is printed on stdout and handed to Herald. The run does not start and nothing is claimed. `scan.py start --force`, or `scan.py resume --force`, starts anyway and later claims skip the check. A local runner does not block. The same check runs on a later claim while the run is running, unless it was force-started.

```bash
python3 <plugin>/scripts/scan.py start --beam .warp/beam.json
python3 <plugin>/scripts/scan.py start --beam .warp/beam.json --force
python3 <plugin>/scripts/scan.py resume --beam .warp/beam.json --force
python3 <plugin>/scripts/prompt_gate.py check --beam .warp/beam.json
python3 <plugin>/scripts/prompt_gate.py ? 
```

## Checkouts, slots, and the committed beam

`subagentVm` defaults to false. `runner: local` forces it false and forces `launch` to `worktree`, skips the cloud-environment start check, and logs one note. Start, `/warp-version`, and status print the effective values. `maxAgents` (default 18) is the one cap on agents that run at once, in a worktree on this machine or on their own VMs. There is no separate cap for this machine. `memoryCheck` defaults to false, so only that cap applies and `free -g` is not read. `true` lets `free -g` lower `maxAgents` on a shared machine when available memory is under 2 GiB per extra subagent. At least one still runs. On a cloud runner, `true` asks for a dedicated VM. The prompt says: run in your own cloud environment on a dedicated VM with its own clone and branch, not a git worktree on this machine. A same hostname falls back to the worktree below, with one warning per run, under the same `maxAgents`. `/in-cloud` in the Agents Window is the manual fallback, one ticket per invocation. One ticket, one worktree, one branch on that path. Start one subagent per ticket in its own worktree, in parallel up to `maxAgents`. `checkout.py launch` runs `git fetch origin main`, then `git worktree add <worktreeRoot>/<id> -b warp/<id>-<jira> origin/main`. `worktreeRoot` defaults to `.warp/worktrees`, which is gitignored. On a shared machine `checkout.py launch` prints `local-cap: <n>`, and `refuse: local subagent cap <n> (live ...)` when live Shuttles are already at it. That number is `maxAgents`, or lower when `memoryCheck` is true. `checkout.py launch` is the gate: it is the only source of a Shuttle prompt, and it records the Shuttle in `.warp/agents.json` before it prints. While the run is paused, stopped, or finished, and for a merged or parked ticket, it prints `spawn: closed <id>` and a `refuse:` line, prints no prompt, and exits 2. Start nothing. Its output is split by the line `prompt: pass every line below this one to the subagent, and nothing above it`. Lines above that mark are for the parent. Lines below it are the worker's prompt, and nothing else is passed to the subagent. The prompt names the ticket, Jira key, locks, acceptance, branch, and absolute worktree path. It also carries `agent: <id>`, the worker contract (one step, one result line, never start another agent, never subscribe, set a timer, loop, sleep, or wait on the pull request, CI, Bugbot, Slack, or an approval), and the reap command: `agents.py reap --beam .warp/beam.json --id <agent> --ticket <id>`, or `agents.py reap --remote --id <agent> --ticket <id>` on a dedicated VM. The subagent works only inside that path, commits, pushes, and opens the pull request. It never merges, never touches the parent checkout, and never calls Jira or Slack. It writes `.warp/tickets/<id>/state.json` and `log.jsonl` at the parent checkout by absolute path and returns one line. `checkout.py verify` checks that the parent is clean apart from `.warp/` and that the diff stays inside the worktree and the locks. A miss raises `lock-escape`. Merge or park runs `git worktree remove` and `git worktree prune`. A failed subagent result raises `worker-died`. The heartbeat watchdog still covers a parent that dies mid-turn. `/warp-resume` reuses the surviving worktree and branch. `checkout.py implement` exits 2. Optional `launch: agent` prints an IMPLEMENT prompt for one new Agent. This plugin does not call a Cloud Agents API. Clone main so `.cursor` rules load, then create the branch. A beam file does not have to exist on the branch before that Agent starts. Two workers never share a ticket or a worktree. `orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>` is the one wait in a run. `--wait` blocks until a ticket folder changes, the inbound queue changes, the listener writes its return stamp, the run halts, or `pollSeconds` pass, and prints `pass: ticket <id>`, `pass: command`, `pass: return`, `pass: halt`, or `pass: timeout`. `--session` is the id `scan.py start` or `scan.py resume` printed as `parent: session <id>`. It prints `parent: stay` while any ticket is not merged or parked. `parent: exit` is when every ticket is merged or parked, or the run is paused or stopped. `parent: exit superseded` means a later start or resume took the run over: end the turn at once. `orchestrator.py supervise --beam .warp/beam.json` prints `listener: poll` and the prompt (`LISTEN`) when no holder is live and the return stamp is in. Start exactly one `warp-listen` Subagent in the background and do not wait. `listener: hold reason=live` and `listener: hold reason=await-return` do not start a second. A stale heartbeat does not start one. The return stamp does.

```bash
python3 <plugin>/scripts/checkout.py ?
python3 <plugin>/scripts/checkout.py launch --beam .warp/beam.json --id T-1
python3 <plugin>/scripts/checkout.py add --beam .warp/beam.json --id T-1 --root .
python3 <plugin>/scripts/checkout.py remove --beam .warp/beam.json --id T-1 --root .
python3 <plugin>/scripts/checkout.py bind --beam .warp/beam.json --id T-1 --agent <agent-id>
python3 <plugin>/scripts/checkout.py verify --beam .warp/beam.json --id T-1 --root .
python3 <plugin>/scripts/checkout.py result --beam .warp/beam.json --id T-1 --line "result: T-1 failed"
```

A Shuttle that exits with a pull request records it (`orchestrator.py opened` or `beam.py set --pr`). That is the agent finishing. The slot stays occupied until the GitHub or Bitbucket check rollup is green (`provider.py rollup`, stored as `pr.rollup`, or `beam.py set --rollup green`). Bugbot runs on the pull request after the push, not inside the VM. Locks stay until merge or park.

`mergeQueue: false` is the default. `true`, or a GitHub ruleset the provider can read, enqueues the pull request (`provider.py merge-pr`) instead of merging it from the agent. A direct merge that branch protection rejects is printed `not merged` and the ticket stays unmerged.

`state_commit.py commit` commits the beam, journal, STATUS, and BOARD on the base branch (`--base` when that must be HEAD). Tokens, cost, API keys, and webhook URLs are removed first. `.warp/config.yaml` is not added. `state_commit.py publish --id T-1` commits that ticket's beam, journal, board, and claim onto the ticket branch and pushes it. The default `checkout.py launch` does not run this. It creates the worktree from `origin/main`. Optional `launch: agent` still prints an IMPLEMENT prompt and does not publish the beam first. `--no-push` commits the branch and does not push. A beam file does not have to exist on the branch before an optional new Agent starts.

```bash
python3 <plugin>/scripts/state_commit.py ?
python3 <plugin>/scripts/state_commit.py commit --root . --beam .warp/beam.json
python3 <plugin>/scripts/provider.py rollup --pr 36 --checks checks.json
python3 <plugin>/scripts/provider.py merge-pr --id T-1 --pr 36
```

## Ticket status directory

The subagent reads the claim from the prompt. It writes only `.warp/tickets/<id>/` in the parent checkout, by absolute path, and does not push that directory. Optional `launch: agent` may still `--push` that directory. It does not commit updates to `.warp/beam.json`.

```bash
python3 <plugin>/scripts/ticket_state.py ?
python3 <plugin>/scripts/ticket_state.py append --id T-1 --state planning --root . --push
python3 <plugin>/scripts/ticket_state.py append --id T-1 --state heartbeat --push
python3 <plugin>/scripts/ticket_state.py append --id T-1 --state lock-escape --escaped src/extra --push
python3 <plugin>/scripts/ticket_state.py append --id T-1 --state check-red --name "make ci" --error "boom" --push
python3 <plugin>/scripts/ticket_state.py observe --beam .warp/beam.json --root .
```

`append` takes `--id`, `--state`, `--root`, `--push`, `--escaped`, `--pr`, `--name`, `--error`, `--alarm`, `--agent`, and `--at`. A worktree subagent passes `--root` as the parent checkout and does not pass `--push`. `--push` is the optional agent path: it commits and pushes only `.warp/tickets/<id>/`. After it writes, `append` prints the reap line: `reap: continue`, or `reap: exit <reason>` and one line saying how to stop. A Shuttle reads that answer every time it writes its state, without a second command. A worktree Shuttle is checked against the parent's beam. With `--push` the check reads the beam from origin's base branch. On a dedicated VM, `append` also writes that VM's cloud agent id as `cloudAgent` in `state.json`, and the parent binds it to the Shuttle's registry row as `cloudId`. `observe` reads a worktree ticket's directory from the parent checkout. It fetches an agent-mode branch and reads only that directory. It patches the live beam. It does not copy a branch beam. Paused and stopped runs print `observe: skipped` and do not fetch. `beam.py watchdog` runs `observe` before it decides a worker has died. A subagent failure returns to the parent, which raises `worker-died` from that result. The watchdog remains for a parent that dies mid-turn.

## /warp-version

```bash
python3 <plugin>/scripts/version.py
python3 scripts/bump_version.py
python3 scripts/bump_version.py --minor
python3 scripts/bump_version.py --major
python3 scripts/check_version.py
python3 scripts/check_version.py --against origin/main
```

The output includes the effective values after a local override, with both caps: `effective: runner=<r> subagentVm=<b> memoryCheck=<b> maxAgents=<n> maxInProgress=<n> launch=<l>`. Start and status print the same line. It also prints `instance: warp:<instance> host=<host> machine=<machine id>`, the tag this run's agents carry, and `this machine: <machine id> host=<host>` for the machine the command ran on. For each retired key still in `.warp/config.yaml` it prints one line, `config: <key>: <value> is retired and ignored.`, with what replaced the key. The output includes the upgrade script under `If /warp-upgrade is not in the command list:`. That script does not need the slash command. `?` on it prints `--root` and `--source`. The same output prints the insurance-sync script when `/warp-update-state` is not in the command list. That script is already on a 1.4.12 install and does not need the slash command. The last lines are headed `If /warp-list or /warp-cleanup is not in the command list:` and are the `agents.py` commands those two run.

```bash
python3 .cursor/plugins/warp/scripts/upgrade.py
python3 .cursor/plugins/warp/scripts/upgrade.py ?
python3 .cursor/plugins/warp/scripts/update_state.py --beam .warp/beam.json
python3 .cursor/plugins/warp/scripts/update_state.py ?
python3 .cursor/plugins/warp/scripts/agents.py list --beam .warp/beam.json
python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud
python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
python3 .cursor/plugins/warp/scripts/agents.py ?
```

`VERSION` is the source of truth. The manifest must match, and `CHANGELOG.md` must have a `## <version>` heading. Every pull request bumps the patch. `bump_version.py` updates all three and inserts a stub (`--note` sets the bullet). CI fails when the pull request version is not newer than `main`.

## Beam

`beam.py ready` recomputes every pending gate on a live run before it lists tickets. A pending or stale-red gate turns green when every member is merged or done and no check is actually red. The evidence is `members merged:` and those ids. G1 with members P-002, P-003, P-004, and P-018 stores `members merged: P-002, P-003, P-004, P-018`. The board is rewritten, and the output includes `G1 pending cleared. Members merged. Tick ran.` plus the dispatch `start` lines for tickets that just became ready. The name alone is not a red check. A red `make ci` is stored on `pr.check` and sent back. Past `maxFixAttempts` (default 5) the ticket parks. A second call does not print those start lines again. A member that is still queued, claimed, in progress, awaiting approval, alarmed, or parked leaves the gate pending. Paused and stopped runs do not recompute. `beam.py` subcommands: `ingest`, `ready`, `set`, `spend`, `usage`, `gate`, `pause`, `resume`, `board`, `check`, `eta`, `heartbeat`, `watchdog`. `pause` and `resume` set the run state through the same code as `scan.py pause` and `scan.py resume`, and the journal types are `paused` and `running` from either. `pause` is the same teardown as `scan.py pause`: `stop: <id>` for each agent, `halt: paused agents=<n>`, then the `cleanup:` lines. `heartbeat` prints the reap line. `watchdog` recovers dead Shuttles and prints no listener line. `set` takes `--status`, `--agent`, `--branch`, `--jira`, `--pr`, `--sha`, `--via local|connected`, `--approved-by`, `--proceeded-by`, `--merge-method`, `--bugbot pass|fail`, `--ci green`, `--check-result green|red|pending`, `--check-name`, `--check-log`, `--alarm`, `--escaped`, `--attempts`, `--force`. A red `make ci` (`--ci red` when `checkCommand` is `make ci`, or `--check-result red --check-name "make ci"`) prints `send-back <id> fix` and a `start` line for that id only. The log is `--check-log`. A second set does not send it again while the ticket is in `fix`. `--escaped` is repeatable and stores each path outside the lock on the ticket with a `lock-escape` alarm. The parent's repair widens the lock to those paths. `--status awaiting_approval`, `merging`, and `merged` are refused until CI is green and, when Bugbot applies, `--bugbot pass`. A fail sets `fix` or, at `maxFixAttempts`, `alarm` / `bugbot-failed`. A new `--sha` during `awaiting_approval` sets `bugbot_running` and does not move Jira. Setting `merged` stores the sha, `mergedAt`, `via`, merge method, and who approved, releases locks, and prints one post-merge MUST DO. `--jira` is rejected when the prefix is not in `jiraProject` or `jiraKeyPrefixes` unless `--force` is set. A plan id is not a Jira key. Do not hand-edit `beam.json`.

## /warp-proceed

```bash
python3 <plugin>/scripts/proceed.py --beam .warp/beam.json WV-01
python3 <plugin>/scripts/proceed.py --beam .warp/beam.json "warp:proceed WAR-1"
python3 <plugin>/scripts/proceed.py --beam .warp/beam.json --by alex "#01"
python3 <plugin>/scripts/proceed.py ?
```

| Flag | Effect |
|---|---|
| `--beam` | Beam file. Default `.warp/beam.json`. |
| `--by` | Who said `warp:proceed`. Stored on `pr.proceededBy` and, when empty, `pr.approvedBy`. |

The token is a plan id, a Jira key, or a `#` number (`01` and `#01` match a ticket whose id or key ends in that number). One match that is `awaiting_approval` with the gate open is accepted. The status stays `awaiting_approval` until the merge command in the same output is run. Local mode prints `provider.py merge-local`. Connected mode prints the squash-merge, then `beam.py set --status merged --sha --via connected`. That set prints one post-merge MUST DO: Jira transition to Done, merged comments, lock release, dependents that are now ready, `status` and `board`, and the Slack reply with the merge sha and the Jira status. Do not stop after the provider merge. A miss, a ticket in any other status, or a closed gate is a refusal and a Slack reply. Nothing is merged.

## Channel verbs

One `warp-listen` listener reads these. It is one background subagent on the parent's checkout. The parent starts it when `orchestrator.py supervise` prints `listener: poll` and does not wait. Each cycle runs `inbound.py poll --lease` before any read, queues each command, and `inbound.py wait` sleeps in Python. A duplicate Slack ts prints `inbound: duplicate` and writes no second ack. The parent applies the queued commands with `inbound.py apply-pending`, woken by `pass: command`. Herald posts an acknowledgement in the same channel before the command runs. There is no `warp:hold`. `/warp-listen` is the same loop after `inbound.py take`.

`warp:proceed <id>`, `warp:retry <id>`, `warp:pause`, `warp:resume`, `warp:stop`, `warp:start`, `warp:status`.

| Command | Ack, then the action |
|---|---|
| `warp:proceed <id>` | `Received warp:proceed XV-01. Merging and moving Jira to Done.` Plan id or Jira key. Only that awaiting_approval ticket. A bad id acks and merges nothing else. |
| `warp:retry <id>` | `Received warp:retry XV-01. Requeueing XV-01.` Status `queued`, alarm cleared, attempts kept. |
| `warp:pause` | `Received warp:pause. Pausing the run and stopping the listener.` The same teardown as `/warp-pause`: every agent is ended. |
| `warp:resume` | `Received warp:resume. Resuming the run.` Does not launch a second listener. |
| `warp:stop` | `Received warp:stop. Stopping the run and the listener.` The same teardown as `/warp-stop`: every agent is ended. |
| `warp:start` | `Received warp:start. Starting the run.` |
| `warp:status` | `Received warp:status. Posting the digest.` |
| anything else starting with `warp:` | `Not understood: warp:hold. Accepted forms: warp:pause, warp:resume, warp:stop, warp:start, warp:proceed <id>, warp:retry <id>, warp:status.` |

```bash
python3 <plugin>/scripts/inbound.py poll --beam .warp/beam.json
python3 <plugin>/scripts/inbound.py accept --beam .warp/beam.json --text "warp:proceed XV-01" --by <who> --source slack --message-id <id>
python3 <plugin>/scripts/inbound.py enqueue --beam .warp/beam.json --text "warp:proceed XV-01" --by <who> --source slack --message-id <id>
python3 <plugin>/scripts/inbound.py polled --beam .warp/beam.json --count <count> --cursor <newest message id>
python3 <plugin>/scripts/inbound.py apply-pending --beam .warp/beam.json
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <id> --pid <pid>
python3 <plugin>/scripts/inbound.py heartbeat --beam .warp/beam.json --agent-id <id>
python3 <plugin>/scripts/inbound.py release --beam .warp/beam.json
python3 <plugin>/scripts/inbound.py status --beam .warp/beam.json
python3 <plugin>/scripts/inbound.py handle --beam .warp/beam.json --text "warp:proceed XV-01" --by <who> --source slack
python3 <plugin>/scripts/inbound.py enqueue --beam .warp/beam.json --text "warp:status" --by <who> --message-id <ts> --source slack
python3 <plugin>/scripts/inbound.py drain --beam .warp/beam.json
python3 <plugin>/scripts/inbound.py ?
```

| Flag | Effect |
|---|---|
| `--beam` | Beam file. Default `.warp/beam.json`. |
| `--agent-id` | For `claim` and `heartbeat` only, which are for a listener someone starts by hand. The id is stored on `listener.agentId`. A second `claim` with the same id prints `listener: already running <id>`: do not start a second. Heartbeat refuses a different id. A poll the parent starts has the id `listener`. |
| `--pid` | Optional process id stored on `listener.pid` by `claim` or `heartbeat`. |
| `--lease` | Generation this copy holds. `poll` checks it before any channel line. `wait` and `returned` use the same number. |
| `--holder` | Holder id that must match the beam. `take` sets it. A mismatch prints `reap: exit superseded` and reads nothing. |
| `--fast` | With `wait`: this cycle queued a command, so sleep `listenerFastSeconds`. Without it, sleep `listenerSlowSeconds`. |
| `--count` | With `wait` or `polled`: how many `warp:` commands this cycle queued. |
| `--cursor` | With `wait` or `polled`: the id of the newest message this read saw. The next read starts after it. |
| `--text` | The channel line to parse or apply. |
| `--by` | Who sent it. Stored on the ack journal and, for proceed, on `pr.proceededBy`. |
| `--message-id` | The channel's message id. Dedupe key for `enqueue`: a repeat is not applied twice. With `accept`, a message an earlier poll already queued prints `inbound: duplicate <id>` and writes no ack. |
| `--source` | `slack` or `teams`. Recorded on the ack. |

`handle` writes `.warp/inbound-ack.json` before it changes the beam. `drain` applies `.warp/pending-commands.jsonl` in order, each ack first. `jiraDoneOnManualMerge: false` changes the proceed ack to say Jira stays at QA Ready. `poll --lease --holder` is the listener's first step of each cycle, before any read. It prints the reap line (`reap: continue`, or `reap: exit <reason>` with exit code 3), then one line per configured channel: `listen: slack channel=<name> since=<cursor>` and `listen: teams channel=<name> since=<cursor>`. A lease mismatch prints `reap: exit superseded` and no channel line. A halt prints `reap: exit paused`, `stopped`, or `done` the same way. An orphaned parent prints `reap: exit orphaned`. Rotation prints `listen: rotate` and exit code 0. `reap: exit` means return `listen: stopped` without reading. `accept` writes the ack and prints `return:` for the parent. It does not apply the command. `enqueue` queues it, and the parent applies it with `apply-pending`. A repeat Slack ts prints `inbound: duplicate` and writes no second ack. `wait` sleeps in Python in 2-second steps and prints `listen: wait`, `listen: rotate`, or `reap: exit`. Pause and stop reap it within about 2 seconds. `returned --reason` writes the return stamp (`polled`, `rotated`, `orphaned`, or `stopped`). `take` bumps the generation for `/warp-listen`. `polled` writes a `polled` stamp and prints `listener: polled <n>`. The listener must not keep reading while paused or stopped. `inbound.py claim` and `inbound.py heartbeat` are only for a listener someone starts by hand. A run does not use them. `heartbeat` writes `listener.lastSeenAt` for the owning agent id. `interval` prints `pollSeconds`. `beam.py heartbeat --id --agent` writes `lastSeenAt` for a Shuttle, then prints the reap line (`reap: continue` or `reap: exit <reason>`), so a Shuttle reads the answer on every beat. `beam.py watchdog` is the check for Shuttles. It prints no listener line and never starts a listener. Shuttle commands:

```bash
python3 <plugin>/scripts/beam.py heartbeat --beam .warp/beam.json --id WV-01 --agent shuttle-WV-01
python3 <plugin>/scripts/beam.py watchdog --beam .warp/beam.json
```

## Agents, the reap check, and the halt

`agents.py` is the registry, the reap check, the local half of the halt, the list, and the clear. `agents.py list --beam .warp/beam.json` is `/warp-list`: what is still out for this run, running or idle. It changes nothing. It prints `instance: warp:<instance> host=<host> machine=<machine id>` first. Then one `agent:` line per row in `.warp/agents.json`, and `registry: live=<n> ended=<n>`. That covers every agent, including a subagent that shares the parent's session and is not a cloud agent of its own. Then one `cloud: id= name= repo= status= updated=` line and its link for each cloud agent in this repo that is in the registry or whose name starts with `[warp:<instance>]`. `status=ACTIVE` is still running. `status=IDLE` is not. `list: kept <id> reason=parent` is this run's parent, `reason=self` is the agent running the command, and `reason=running-untagged` is a running agent that only matched loosely. `/warp-cleanup` leaves those. The last lines are `out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>` and what `/warp-cleanup` would do. The cloud lines need `CURSOR_API_KEY` in the environment. Without it the command prints `cloud: not read. Set CURSOR_API_KEY in the environment to list cloud agents.` and `cloud: link https://cursor.com/agents/<id>` for each recorded cloud agent. `agents.py cleanup --beam .warp/beam.json --cloud --apply --running` is `/warp-cleanup`: it cancels and archives what `/warp-list` shows. Both take the same selection options. `scan.py start`, `scan.py resume`, `scan.py status`, and `version.py` print the same `instance:` line, and `version.py` adds `this machine: <machine id> host=<host>` for the machine the command ran on. Every agent this run starts is named `[warp:<instance>] <ticket> <step>` or `[warp:<instance>] listener`. `cleanup --cloud` prints `cleanup: tag [warp:<instance>]` and matches that tag. `.warp/agents.json` is the one registry: every spawn and every exit is a row. The parent is the only agent that starts another agent, and the only agent that waits. A Shuttle does one step of one ticket and returns one line. The listener is one background subagent on the parent's checkout and returns when its lease ends.

```bash
python3 <plugin>/scripts/agents.py ?
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --tag all
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --untagged
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --scan="Jira","Fix","Bugbot"
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --scan Jira,Fix,Bugbot
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --scan Jira --scan Fix
python3 <plugin>/scripts/agents.py check --beam .warp/beam.json
python3 <plugin>/scripts/agents.py reap --beam .warp/beam.json --id subagent:WV-01 --ticket WV-01
python3 <plugin>/scripts/agents.py reap --remote --id subagent:WV-01 --ticket WV-01
python3 <plugin>/scripts/agents.py reap --beam .warp/beam.json --role parent --id <session>
python3 <plugin>/scripts/agents.py stop --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running --tag a1b2c3
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running --untagged
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --scan Jira,Fix,Bugbot
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --force --scan Jira --scan Fix
```

| Subcommand | Effect |
|---|---|
| `list` | `/warp-list`. Read-only. The instance line, then one line per registry row: `agent: <id> role= ticket= state= started= ended=`, and the link for a cloud id. Then `registry: live=<n> ended=<n>`, one `cloud:` line per matching cloud agent when `CURSOR_API_KEY` is set, and `out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>`. |
| `check` | `spawn: open`, or `spawn: closed paused`, `spawn: closed stopped`, or `spawn: closed done`. |
| `reap` | The check every Warp agent runs first, and again after each state it writes. Prints `reap: continue`, or `reap: exit <reason>` and one line saying how to stop, and exits 3. On exit against this checkout's beam, that agent's row is ended and `reaped` is stamped. `--remote` writes nothing. The parent stamps `reaped` on the row named by `agent=` in `result: <id> stopped <reason> agent=<agent>`, or by the ticket folder's `cloudAgent` when the line has no agent. A result for a different agent is `reap-stale` and does not end the live Shuttle. |
| `stop` | The local half of the halt. No network. Ends every live row (`stop: <id>`), marks every row halted, frees every Shuttle slot, and prints `halt: stopped agents=<n>`. It does not set `runState`. `scan.py pause`, `scan.py stop`, and `beam.py pause` run the same step and then the cloud half. |
| `cleanup` | The clear. `cleanup --cloud --apply --running` is `/warp-cleanup`. It lists the registry first, and `agents: none` when there is no row. `--cloud` without `--apply` is a dry run. See `/warp-list` and `/warp-cleanup` under Run control. |

| Flag | Effect |
|---|---|
| `--beam` | Beam file. Default `.warp/beam.json`. |
| `--id` | With `reap`: this agent's id from the prompt line `agent:`. With `--role parent` it is the session id from `parent: session <id>`. |
| `--ticket` | With `reap`: the ticket this Shuttle works on. |
| `--role` | With `reap`: `shuttle` (the default), `listener`, or `parent`. |
| `--remote` | With `reap`: read the beam from origin's base branch and write nothing. For a Shuttle on its own VM. Pause and stop push the beam before they return, so this check sees them. |
| `--root` | With `reap`: the checkout to read. The default is the beam's checkout. |
| `--cloud` | With `cleanup`: read `GET /v1/agents`. Needs `CURSOR_API_KEY`. `list` accepts it and does not need it: the cloud agents are listed whenever the key is set. |
| `--apply` | With `cleanup --cloud`: archive the dry-run matches. |
| `--running` | With `cleanup --cloud`: also cancel the run of a RUNNING or ACTIVE match, then archive it. Refused while the run is running. |
| `--force` | With `cleanup --running`: allow it while the run is running, and cancel a running agent that only matched loosely. |
| `--tag` | With `list` and `cleanup --cloud`: another run's tag in place of this beam's `[warp:<instance>]`. `a1b2c3`, `warp:a1b2c3`, and `[warp:a1b2c3]` are the same. `--tag all` is the tag of any Warp run, and `--tag '*'` is the same. Quote the star. With another run's tag, this run's registry is left out, and nothing checks whether that run is still going. |
| `--untagged` | With `list` and `cleanup --cloud`: also match agents that carry no Warp tag, from before 1.5.0, loosely, on Warp role words in the name or prompt (Shuttle, IMPLEMENT, fix, rebase, listener, Bugbot). An idle one is archived. One that is still running is left (`reason=running-untagged`) unless `--force`, because it may be someone else's. Read `/warp-list --untagged` first. |
| `--all-idle` | With `list` and `cleanup --cloud`: drop the match. Every idle agent in this repo, tagged or not, unless `--any-repo`. |
| `--any-repo` | With `list` and `cleanup --cloud`: do not limit to this repo. |
| `--scan` | With `list` and `cleanup --cloud`: match these words in the name, summary or description, and first prompt, in every repo the key can see. A comma list (`--scan Jira,Fix,Bugbot`), quoted items (`--scan="Jira","Fix","Bugbot"`), or repeated flags (`--scan Jira --scan Fix`). Case-insensitive. Ignores the repo filter and the tag and registry rules. `--scan` crosses repos: read `/warp-list --scan` before `--apply`. A dry run prints `cleanup: would archive <id>` and `cleanup: would cancel <id>`, then `cleanup: count`. `--apply` archives idle matches. A RUNNING or ACTIVE match is left (`reason=running-scan`) unless `--force`, which cancels it and then archives it. The refusal while the run is running still applies. Archive only. The agent running the command (`reason=self`) and this run's parent (`reason=parent`) are never matched. |

| `reap: exit` reason | Meaning |
|---|---|
| `paused`, `stopped` | `/warp-pause` or `/warp-stop` ran |
| `done` | every ticket is merged or parked |
| `settled` | this ticket is merged or parked |
| `replaced` | the parent started another Shuttle for this ticket, or another parent took the run |
| `halted` | this agent was started before a pause or a stop, and the run moved on without it |
| `archived` | cleanup archived this agent |
| `not-resumed` | the turn returned more than `staleMinutes` ago and the parent did not resume it |
| `not-launched` | no launch was recorded for this id: something other than the parent started it |

`ticket_state.py append`, `beam.py heartbeat`, and `inbound.py poll` print the same reap line. A Shuttle that reads `reap: exit <reason>` returns `result: <id> stopped <reason> agent=<agent>`. `<agent>` is its registry id or its cloud id. The listener returns `listen: stopped`. The parent ends its turn. None of them starts an agent, subscribes, sets a timer, or waits.

The halt and the clear print `cleanup:` lines for cloud agents: `cleanup: cancelled <id>`, `cleanup: archived <id>`, `cleanup: link https://cursor.com/agents/<id>` when there is no key, `cleanup: would cancel <id>` on a dry run, and `cleanup: skip <id> reason=self`, `reason=parent`, `reason=running`, `reason=running-untagged`, `reason=running-scan`, or `reason=status` for an agent that was left. `reason=running-untagged` is a running agent that only matched loosely, by `--untagged` or `--all-idle`. `reason=running-scan` is a running agent that matched `--scan`. Both take `--force` to cancel. `cleanup: would archive <id>` is an idle `--scan` match on a dry run. `cleanup: cut short; run /warp-cleanup for the rest` means the halt gave up after 45 seconds. A Shuttle on its own VM writes `cloudAgent` in its ticket folder and the parent binds it as `cloudId`, so one ticket holds one cloud agent: a fix round on a new VM retires the VM it left, and merge and park archive every VM the ticket used.

## Lock-escape repair

`maxInProgress` (default 20) caps tickets that are open: started, and not merged or parked. At that cap, dispatch takes nothing new from the ready queue. It holds only new tickets. A fix, a rebase, or a rerun still starts, including on a cold start with nothing running, because its ticket is already open. It is an agent like any other and takes a `maxAgents` slot. There is no separate cap for fixes. `/warp-status`, `/warp-start`, and `/warp-resume` print `in progress N/maxInProgress`, or `holding new launches (N/maxInProgress), N fix workers running` when that cap is full, and `live shuttles N/cap, N fix workers running` with the dead tickets still waiting for a replacement. `N fix workers running` is a count that is shown. It is not a cap. `checkout.py launch` counts live Shuttles and refuses with `live N/cap` and the ids that hold the slots. On a shared machine that cap is `maxAgents`, or lower when `memoryCheck` is true. `orchestrator.py supervise --beam .warp/beam.json` is one parent pass: the listener line first, then the ticket pipeline. The listener line is `listener: poll`, `listener: hold reason=live`, `listener: hold reason=await-return`, `listener: idle`, or `listener: none`. `listener: poll` is printed when no holder is live and the return stamp is in, or when none has started, and the prompt follows it, first line `LISTEN`. Start exactly one `warp-listen` Subagent in the background with those lines and do not wait. `listener: hold reason=live` means a holder already has the lease. `listener: hold reason=await-return` means the lease was bumped and the return stamp is not in. `listener: idle` means the run is paused, stopped, or finished. `listener: none` means no `slackChannel` or `teamsChannel` is set, so there is no listener at all. A generation is not a spawn and does not count against a spawn cap. `--returned` is the line the listener returned. `listen: stopped` idles only the pass that still had the generation out. A later pass with the same line starts the next generation when the run should still listen. `pause`, `stop`, `warp:pause`, and `warp:stop` still idle. `--provider` is a JSON file of ticket id to pull request, checks, and Bugbot, used by tests. With no `--provider` file, supervise loads each open pull request from GitHub: head sha, check runs, mergeable, mergeStateStatus, conflicting files, and Bugbot. A CONFLICTING or DIRTY pull request rebases in that pass. Zero check runs older than `ciStartGraceMinutes` (default 5) start CI in that pass. `review: wait` is not printed for either case. `--now` is a timestamp. The same pipeline runs from `orchestrator.py dispatch`. Phases are `implementing`, `pr-open`, `reviewing`, `fixing`, `ready`, `merging`, then `merged` or `parked`. Opening a pull request is not done. `bugbot: request` asks Bugbot on that pull request. The serial merge queue runs on every pass. Every `repairSweepMinutes` (default 15) that pass also sweeps the whole run: what is broken, and what can be fixed that is not already queued or running. Broken covers tickets, pull requests, CI on main, locks, gates, the listener, and the beam, including red or stuck CI, a merge conflict, unresolved Bugbot findings, a stale or orphaned lock, a red base, a stuck gate, a listener with no holder, and a beam that is out of sync with main. The sweep reuses the fixer paths, starts work up to the slot cap, and logs findings and actions. A listener with no holder prints `sweep: finding listener idle` and does not start one. A live holder prints `sweep: skip listener running`. `/warp-status` shows the last sweep. Slack is posted only when the sweep starts something new or escalates.

The parent tick runs this on each pass while the run is running, every `alarmRepairMinutes` (default 15). The listener does not start the repair Agent. Pause and stop do not. A second call does not start a second repair. Only `lock-escape` is repaired, one ticket at a time. The repair widens that ticket's locks to the paths that escaped. It waits when an in-flight ticket holds one of those paths. `bugbot-failed`, `worker-died`, `stuck`, `ci-red`, and `gate-red` are left alone by `alarm_repair.py`. `orchestrator.py supervise` restarts a `worker-died` step, starts a fix Shuttle for `ci-red` and `bugbot-failed`, and restarts `stuck` up to `maxRecoveries`. `gate-red` stays on the gate. `maxAlarmRepairs` (default 5) skips a ticket after that many attempts.

```bash
python3 <plugin>/scripts/alarm_repair.py next --beam .warp/beam.json
python3 <plugin>/scripts/alarm_repair.py next --beam .warp/beam.json --returned WV-01
python3 <plugin>/scripts/alarm_repair.py next --beam .warp/beam.json --returned WV-01 --error "boom"
python3 <plugin>/scripts/alarm_repair.py paths --beam .warp/beam.json --id WV-01 --path src/extra
python3 <plugin>/scripts/alarm_repair.py ?
```

| Flag | Effect |
|---|---|
| `--beam` | Beam file. Default `.warp/beam.json`. |
| `--now` | Timestamp for a test clock, `YYYY-MM-DDTHH:MM:SSZ`. |
| `--returned` | The repair Shuttle for this ticket id has returned. Settle it before starting another. |
| `--error` | With `--returned`, the repair errored. The alarm stays `lock-escape`. |
| `--id` | Ticket whose escaped paths `paths` stores. |
| `--path` | One file or directory outside the lock. Repeat for each path. `paths` does not widen and does not start. |

`next` prints `alarm-repair: start <id>` for one ticket, in plan order. It adds the `escaped` paths to that ticket's locks first and records them on `addedLocks`. The Herald line names the paths added. `alarm-repair: locked <id> by <holder>` means an in-flight ticket holds one of those paths, or one of the ticket's current locks. Nothing is widened and nothing starts until that holder finishes. A queued ticket does not block the widen. `alarm-repair: name <id>` means an older alarm has no path list. Launch one Shuttle to record the paths, then `next --returned`. `alarm-repair: working` and `alarm-repair: waiting` mean a repair is still in flight. Do not launch another.

A failure leaves the alarm and starts the next lock-escape ticket in that same call. The failed ticket is not retried in that pass. Success waits until the ticket is merged, or back on the normal path with the alarm cleared and the repair Shuttle finished, then starts the next. `maxAlarmRepairs` (default 5) skips a ticket after that many attempts. `alarmRepairMinutes` (default 15) is how often a new pass opens. Herald posts one line when a repair starts, when a failure takes the next ticket, and when a ticket is given up.

When that pass opens and the ready set is empty, `next` recomputes every pending gate. A gate turns green when every member is merged or done. The same output posts `G1 pending cleared. Members merged. Tick ran.` and the dispatch `start` lines. Launch those ids in that pass. A second pass does not print them again. A member that is not merged or done leaves the gate pending. Pause and stop do not recompute. Every tick also recomputes, from `beam.py ready` and `orchestrator.py dispatch`, so a merge does not wait for this pass. The same pass sends a red `make ci` back even when the ready set is not empty: `send-back <id> fix` and `start <id>`. Launch that Shuttle with the log. A Shuttle already in `fix` is not started again. Parked does not launch.

The Shuttle that first escapes stores the paths with `beam.py set --status alarm --alarm lock-escape --escaped <path>`. `--escaped` is repeatable. It does not widen the lock. The parent's repair does.
