# Changelog

## 1.5.3

- `jiraInReviewStatus` (default `In Review`) is a fourth Jira move. It fires when Bugbot is requested. When Bugbot is off (`bugbotRequired` false, or `bugbotManual` false on a manual ticket), it fires when the pull request opens. A fix send-back stays in In Review and does not move back to In Progress. Sizes outside `autoMergeSizes` still move to QA Ready once review is clean. A missing key keeps `In Review`. An empty string or null (`""`, `null`, `~`, `none`) turns the move off: the issue stays where it is. Bugbot, review, fix send-backs, QA Ready, and Done still run. No matching transition warns and does not fail.
- `/warp-sync` (`jira_sync.py sync`) reads Jira into the beam. Dry-run unless `--apply`. It looks up each ticket's branch and pull request. A merged pull request wins over the Jira status, including when several pull requests match one ticket. A move to an earlier status needs `--force`. It does not dispatch and does not start a listener. When `jiraInReviewStatus` is empty, nothing maps to In Review, a ticket with an open pull request stays reviewing from that lookup, and `jira.inReviewAt` stays unset. A fix send-back stays in place while Jira is still In Review. `/warp-start` picks the work up on the unfinished-first pass.

## 1.5.2

- The listener is one background subagent on the parent's checkout. `subagentVm` does not apply to it. The parent starts it on `listener: poll` and does not wait. `agents/listener.md` sets `is_background: true` and has no model key. `listenerModel` unset means the prompt says `model: inherit`. A set slug is `model: <slug>`. A live run still has to confirm that `is_background` and the model field work.
- A generation lease. The beam stores `listener.generation` and `listener.holder`. Each cycle starts with `inbound.py poll --lease <gen> --holder <id>` before any read. A mismatch prints `reap: exit superseded` and reads nothing. A halt prints `reap: exit paused`, `stopped`, or `done` the same way. `/warp-listen` runs `inbound.py take` so a chat you open yourself holds the lease and any other copy exits.
- The parent writes `listener.parentSeenAt` from `parent-exit --wait` and from `supervise`. A stamp older than `listenerOrphanMinutes` (default 5) makes the listener exit `orphaned` and write the return stamp. A stale heartbeat does not start a replacement. The return stamp does (`polled`, `rotated`, `orphaned`, `stopped`), and only when no holder is live. Lease expiry bumps the generation and does not start the next copy in the same moment.
- `inbound.py wait` sleeps in Python in 2-second steps. `listenerFastSeconds` (15) after a cycle that queued a command, `listenerSlowSeconds` (60) after an empty read. It returns early on halt, a lease mismatch, an orphan, or rotation. Pause and stop reap the listener within about 2 seconds. A read already in flight finishes, then the next poll reaps it. `pollSeconds` is only the parent-wait cap.
- The parent wait also wakes on the inbound queue (`pass: command`) and on the return stamp (`pass: return`). Acks stay deduped by Slack ts: `inbound: duplicate` writes no second ack.
- Rotation at `listenerMaxMinutes` or `listenerMaxReads` (default 60), whichever comes first, prints `listen: rotate` and writes the return stamp. The repair sweep and `listenerStaleMinutes` no longer start a listener. `listenerStaleMinutes` is retired.
- `listen: stopped` idles only the supervise pass that still had the generation out, or when the run should not listen. A later pass with the same line starts the next generation when tickets are still open. Hold lines name the reason: `listener: hold reason=live` and `listener: hold reason=await-return`. Start prints `listener: not started. The next supervise issues it.`

## 1.5.1

- `/warp-list` and `/warp-cleanup` take `--scan`. It matches one or more words, case-insensitive, in an agent's name, summary or description, and first prompt, whatever the Cloud Agents API returns. `--scan="Jira","Fix","Bugbot"`, `--scan Jira,Fix,Bugbot`, and `--scan Jira --scan Fix` are the same. It ignores the repo filter and the tag and registry rules and pages through every agent the key can see, so read the list before `--apply`. Each match prints id, name, repo, status, updated time, the word, the field, and a link, then a count. The agent running the command (`reason=self`) and this run's parent (`reason=parent`) are never matched. Without `--apply`, cleanup is a dry run: `cleanup: would archive` and `cleanup: would cancel`, then a count. With `--apply`, idle matches are archived. A RUNNING or ACTIVE match is left (`reason=running-scan`) unless `--force`, which cancels it and then archives it. The refusal while the run is running still applies. Archive only. Warp never deletes an agent.

## 1.5.0

- Agents still piled up after 1.4.20, and some kept waking and respawning. The listener was a subagent that looped and slept, and the parent restarted it whenever its heartbeat went stale, so each restart left the old loop behind. A Shuttle's prompt carried the parent's line `Start one subagent per ticket`, so a worker could start workers. No prompt forbade a worker from waiting on its pull request, setting a timer, or subscribing. `/warp-pause` ended nothing. `/warp-stop` marked rows stopped and then ran cleanup without `--cloud --apply`, so it never archived an agent. Cleanup skipped every agent that was still running. The registry was stored twice, on the beam and in `.warp/agents.json`, and a write from one process was lost when another saved its older copy.
- The flow is now tick-and-return, and `docs/LIFECYCLE.md` is the rule. One agent is long-lived: the parent. It is the only agent that starts another agent, and the only agent that waits. A Shuttle does one step of one ticket and returns one line. The listener does one poll and returns one line. Nothing depends on a Cursor hook: plugin hooks do not run on cloud runners, and every check is a script that a command, a skill, or a prompt runs.
- The gate. A Shuttle prompt comes only from `checkout.py launch`, which records the Shuttle first. It prints no prompt and exits 2 (`spawn: closed <id>`) while the run is paused, stopped, or finished, and for a merged or parked ticket. Its output is split by `prompt: pass every line below this one to the subagent, and nothing above it`: the line that tells the parent to start workers in parallel is above the mark and never reaches a worker.
- The contract. Every worker prompt carries `agent: <id>`, the contract (one step, one result line, never start another agent, never subscribe, set a timer, loop, sleep, or wait on a pull request, CI, Bugbot, Slack, or an approval), and the reap command. A worker woken by something that is not a new step from the parent runs the reap check and returns.
- The reap check. `agents.py reap --id <agent> --ticket <id>` prints `reap: continue`, or `reap: exit <reason>` and exits 3. The reasons are `paused`, `stopped`, `done`, `settled`, `replaced`, `halted`, `archived`, `not-resumed`, and `not-launched`. `ticket_state.py append`, `beam.py heartbeat`, and `inbound.py poll` print the same line, so a Shuttle reads it every time it writes its state. `--remote` reads the beam from origin's base branch for a Shuttle on its own VM. `--role parent` and `--role listener` are the same check for those two.
- The halt. `/warp-pause` and `/warp-stop` now run one teardown (`scan.py pause`, `scan.py stop`, `beam.py pause`). Every registry row is ended and marked halted, every Shuttle slot is freed, the listener's poll is closed (`halt: paused agents=<n>`), and the state is pushed. With `CURSOR_API_KEY`, every cloud agent in the registry, and every agent in this repo whose name carries this run's tag, has its run cancelled (`POST /v1/agents/{id}/runs/{runId}/cancel`) and is archived. The agent running the command and this run's parent are skipped. Without the key it prints one link per cloud agent. Nothing from before a halt is resumed: resume starts a new Shuttle with a new agent id on the same branch (`fix: <id> resume after halt`), counts no recovery, and tells nobody a worker died. Pause used to leave in-flight Shuttles running. It no longer does. The same teardown runs once when every ticket is merged or parked.
- The clear. `/warp-cleanup` gained `--running`, which cancels the run of a RUNNING or ACTIVE match and then archives it. It is refused while the run is running unless `--force`. This run's parent is never touched (`reason=parent`). The dry run prints `cleanup: would cancel <id>`.
- The listener is one poll. `orchestrator.py supervise` prints `listener: poll` and the prompt (`LISTEN once`) when the last poll finished more than `pollSeconds` ago. The parent starts one `warp-listen` subagent in the foreground. It runs `inbound.py poll`, reads each channel once from the `since` mark, queues the commands, runs `inbound.py polled --count --cursor`, and returns `listen: <count>`. `listener: none` means no channel is configured and no listener is started. A poll that never reports is issued again after `listenerStaleMinutes`. The registry keeps one `listener` row for the run. `inbound.py accept --message-id` does not ack a message an earlier poll already queued. `listener: start`, `listener: backoff`, `listener: cap`, `listener: subagent`, the word `recycle`, `.warp/listener-note.json`, and the config keys `listenerRestartNote` and `maxListenerRestartsPerHour` are gone. An old `config.yaml` that still has those keys is fine: they are ignored. `inbound.py claim` and `heartbeat` remain for a listener someone starts by hand.
- One wait. `orchestrator.py parent-exit --wait --session <id>` blocks until a ticket folder changes, the listener is due, the run halts, or `pollSeconds` pass, then prints `pass: ticket <id>`, `pass: poll`, `pass: halt`, or `pass: timeout` and `parent: stay` or `parent: exit`. The parent used to loop passes with no wait at all. `scan.py start` and `scan.py resume` print `parent: session <id>`. A later start or resume takes the run over, and the earlier parent reads `parent: exit superseded`.
- One ticket, one cloud agent. A Shuttle on its own VM writes its cloud agent id into its ticket folder (`cloudAgent`), and the parent binds it to the registry row (`cloudId`). A halt cancels it by id, before it looks at the account's agent list. A fix round that lands on a new VM, or the replacement of a dead Shuttle, retires the VM the ticket left on the next pass. A retired VM that wakes reads `reap: exit replaced`. Merge and park cancel and archive every VM the ticket used. The parent's own cloud agent is never bound to a Shuttle and never archived.
- `.warp/agents.json` is the one registry. The copy on the beam is used only while the file is unchanged since this process read or wrote it.
- Every agent Warp starts is tagged `warp:<instance>`. The instance is six characters that name one Warp run, which is one beam: the last six characters of the parent's cloud agent id on a cloud runner, or six characters of a hash of the machine id and the beam's path on a laptop. `/warp-start` sets it once and it stays on the beam (`instance`), so a resume from another window or VM keeps it. Cursor has no tag, label, or metadata field on an agent, and its agent list returns only the name, so the tag is the name: `[warp:<instance>] <ticket> <step>` for a Shuttle and `[warp:<instance>] listener` for a poll. `checkout.py launch` prints `name:` for the parent to use, and the same text is the first line of the prompt.
- The instance is shown as `instance: warp:<instance> host=<host> machine=<machine id>` by `/warp-start`, `/warp-resume`, `/warp-status`, `/warp-version`, and `agents.py list`. `.warp/STATUS.md` and `.warp/status.json` record it. Every Slack and Teams header is now `Warp | <repo> / <project> | warp:<instance>`, and the status post has an `Instance` fact. Two Warp runs in one channel, in one repo or in different repos, are told apart.
- Pause, stop, and `/warp-cleanup` now act only on agents in the registry or carrying this run's tag. Before, cleanup matched any agent in the repo whose name or prompt had a Warp role word, including the word "fix", which could archive someone else's idle agent, and it would have matched another Warp run's agents. `agents.py cleanup --cloud` prints `cleanup: tag [warp:<instance>]` and lists this run's tagged agents with their status, running or idle. `--tag all` matches the tag of any Warp run. `--untagged` is the old loose match, for a pile from before the tag: read that dry run before `--apply`. The halt reads the agent list once and looks further only at tagged names, so other agents on the account cost no calls.
- `/warp-start` and `/warp-resume` are now the same command. Either one starts from a stopped run, a paused run, or a run that is already running, and prints which (`run: was paused (<reason>). Continuing.`). Resume used to skip the prompt gate that start ran. Both run it now, and both take `--force`. `beam.py pause` and `beam.py resume` are the same code as `scan.py pause` and `scan.py resume`, so the journal types are `paused` and `running` from either.
- `/warp-pause` and `/warp-stop` are the same teardown and differ in one thing: stop also writes the completion report and records `stoppedAt`. Both commands, the README, and `docs/LIFECYCLE.md` carry the same table.
- Start and resume check for unfinished work and launch it before anything new. They print `unfinished: <n> open. <n> need a fix, <n> need a Shuttle, <n> have a Shuttle out, <n> wait on review or checks.` The order is fixes, then tickets whose Shuttle was halted or died, then new tickets from the ready queue.
- Two caps instead of four. `maxAgents` is how many agents run at once: every Shuttle, whatever its step, on this machine or on its own VM. `maxInProgress` is how many tickets are open at once, and it holds only new tickets. `maxLocalSubagents` and `maxFixWorkers` are retired and ignored. A config that still has them prints `config: <key>: <value> is retired and ignored.` at start and in `/warp-version`. Fix Shuttles used to stop at `maxFixWorkers` (5) while new tickets took the free slots. Now unfinished work takes the slots first. The `effective:` line shows `maxAgents=` and `maxInProgress=`. `memoryCheck: true` lowers `maxAgents` on a shared machine.
- `/warp-list` is new: what is still out for this run, running or idle. It prints the instance, every registry row, each tagged cloud agent with its status, and `out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>`. It changes nothing. `/warp-cleanup` is now the one command that cancels and archives what `/warp-list` shows (`agents.py cleanup --cloud --apply --running`). Both take the same selection: `--tag <instance>` for another run's tag, `--tag all` (or `'*'`) for any Warp run, `--untagged` for agents from before the tag, `--all-idle`, and `--any-repo`. An agent that only matched loosely and is still running is left alone unless `--force`, because it may be someone else's.
- `/warp-list ?` and `/warp-cleanup ?` print the full help: what the command prints or does, the selection table, the safety rules, and examples. `help`, `-h`, and `--help` do the same. `/warp-cleanup ?` cancels nothing and archives nothing, even on the acting command line.
- A pause or a stop wins over work already under way. A pass that was running when the halt landed does not write over it (`pass: aborted`), and a launch that was fetching prints no prompt. A failed Cloud Agents API call never aborts a pass, a pause, or a stop (`cleanup: unreachable <id>`). Start and resume push the state before they print a `start` line, so a Shuttle on its own VM does not read the old pause. `warp:resume` and `warp:start` from the channel do nothing while the run is already running.
- The hook scripts in `hooks/` were committed without the executable bit, so the local IDE could not run them. They are executable now. `hooks/subagent-start.sh` is new and is a local IDE duplicate of the gate. `WARP_SPAWN_GATE=off` turns it off.
- Not verified against a live Cursor run. The Cloud Agents API calls are tested with a recorded transport. Cursor's published API lists `name` on each agent and takes `name` when an agent is created. Whether a subagent the parent starts keeps the name the parent gives it is the one thing to check on a first run: `agents.py cleanup --beam .warp/beam.json --cloud` should list the run's Shuttles under `cleanup: tag [warp:<instance>]`. Agents in the registry are found by id either way.


## 1.4.20

- Shuttles, listeners, and Bugbot runs were piling up as unarchived cloud agents. A new parent session treated an earlier Shuttle as dead even when its heartbeat was fresh, so start and resume launched a replacement and left the old one running. The watchdog and the next pipeline pass could each launch one. Every fix, rebase, and CI start printed `start` and created another agent. Bugbot was requested again on the same head commit, including when a stalled review bumped the review cycle. Listener restarts had no hourly cap. Pause, stop, and a finished run did not tell those agents to exit.
- `.warp/agents.json` records every spawn and every exit (`id`, `ticket`, `role`, `session`, `started`, `ended`, `state`). A ticket has one live Shuttle. A replacement starts only after the turn has returned or the heartbeat is older than `staleMinutes`, and the old row is ended first. A fresh heartbeat stays live across parent sessions. A fix round prints `resume` and continues that Shuttle. `start` is a new agent.
- New spawns stop at `maxSpawnsPerTicket` (8) and `maxSpawnsPerHour` (40). Listener restarts stop at `maxListenerRestartsPerHour` (8) and back off, up to 30 minutes. The ticket becomes `alarm` / `spawn-cap`, or the listener prints `listener: cap`, and later passes do not spawn. Bugbot is requested once per head commit (`bugbot: hold` on the same sha).
- Nothing spawns while the run is paused or stopped, or every ticket is merged or parked. The listener and each Shuttle read `runState` and exit. `/warp-stop` marks every registered agent stopped. Merge, park, and the end of the run run cleanup.
- `/warp-cleanup` lists the registry. Cursor's Cloud Agents API archives with `POST /v1/agents/{id}/archive` when `CURSOR_API_KEY` is set (`GET /v1/agents` with `--cloud`). The key is an environment variable and is never written. Without it, cleanup prints `https://cursor.com/agents/<id>`. A dry run is the default: `agents.py cleanup --beam .warp/beam.json --cloud` prints id, name, repo, status, last update, and link, plus a count, and changes nothing. `--apply` archives that list. A match is IDLE, its repository is this checkout's origin, and it is in `.warp/agents.json` or its name or prompt is a Warp role (Shuttle, IMPLEMENT, fix, rebase, listener, Warp-triggered Bugbot). The agent running the command stays. A RUNNING or ACTIVE agent stays. `--all-idle` includes every IDLE agent in this repo. `--all-idle --any-repo` includes every IDLE agent the key can see. The dry run, then `--apply`, is how to clear a pile of about 199 Warp agents in this repo. Init and upgrade copy `commands/warp-cleanup.md` into `.cursor/commands/`. `/warp-version` prints the script when the slash command is missing.

## 1.4.19

- A launch slot is a live Shuttle: one started in this parent session that has a fresh heartbeat or has not returned yet. Ticket status and a stale claim do not count against `maxAgents`, `maxLocalSubagents`, or `maxFixWorkers`. A dead Shuttle releases its slot immediately.
- On start or resume in subagent mode, a Shuttle from an earlier parent session is dead. Warp releases those slots and marks the tickets for replacement. VM mode keeps a Shuttle with a fresh heartbeat and releases one whose heartbeat is stale.
- Start and resume then dispatch up to the caps: an open pull request that needs a fix (a conflict, CI that never started, red CI, then Bugbot findings), then dead Shuttle replacements, then other ready-queue work. `maxInProgress` still holds only that other work.
- `checkout.py launch` counts live Shuttles. A refusal prints `live N/cap` and the ticket ids that hold the slots. `/warp-status`, start, and resume print `live shuttles N/cap`, how many fix workers are running, and the dead tickets waiting for a replacement.

## 1.4.18

- `maxInProgress` only holds new launches from the ready queue. A fix, a rebase, a CI rerun, a Bugbot re-request, and a merge on a ticket already in progress are not held. `maxFixWorkers` (default 5) caps the fix, rebase, and rerun Shuttles that are actually running. It still applies when in progress is at or above the cap, and it stays within `maxAgents` and `maxLocalSubagents`. On start or resume with no Shuttle out, Warp dispatches those workers immediately: conflicts and CI that never started, then Bugbot findings, then red CI. Status says `holding new launches (N/maxInProgress), N fix workers running`.

## 1.4.17

- Cursor's plugin command index still omits slash commands added after it was published. `commands/warp-update-state.md` was already in `.cursor-plugin/plugin.json`, and `marketplace.json` does not list individual commands for any command. Init and upgrade wrote only `.cursor/commands/warp-upgrade.md`. They now write every `commands/*.md` file to `.cursor/commands/`, including `/warp-update-state` and `/warp-status`. Reload Cursor so those project commands show up.
- `/warp-update-state` does not need the slash command. A 1.4.12 install already has `python3 .cursor/plugins/warp/scripts/update_state.py --beam .warp/beam.json`. `?` prints the flags. `/warp-version` prints those lines when the slash command is missing.
- `/warp-update-state` said each ticket is a new Agent. Each ticket is a subagent in its own git worktree, or on its own VM when `subagentVm` is true. The command, the skill, and the current docs say that. Optional `launch: agent` is still one new Agent, and this plugin does not call that API.

## 1.4.16

- `pipeline.findings_of` crashed when `pr.bugbotFindings` was an int, a string, or another non-list. `jira_sync.mark_bugbot_fail` had stored a running count, and a shuttle snapshot passed a string through `list()`, which split it into characters. The field is now a list of finding strings. A count above 0 is stored as `N Bugbot findings (details not loaded)`, so the merge gate stays closed and a fix run starts. 0 and false are no findings. A dict finding stores its message, or else its body.
- `maxInProgress` (default 20) caps tickets that have started and are not merged or parked. At or above that count, nothing new is taken from the ready queue. Fixes, rebases, reruns, and merges on tickets already in progress keep running. Parked tickets do not count, and being over the cap does not park or cancel anyone. `/warp-status`, start, resume, and the Slack digest show `in progress N/maxInProgress`. The first pass that holds a launch logs that once. This is separate from `maxAgents` and `maxLocalSubagents`.
- `orchestrator.py supervise` reads each open pull request from GitHub when it is started without a `--provider` file. It stores the head sha, rollup, CI, mergeable state, and conflicting files, and it drops a green rollup, CI, or Bugbot result that belongs to an older sha. A CONFLICTING or DIRTY pull request rebases in that same pass. Route registration files keep both sides. A head with no check runs after `ciStartGraceMinutes` (default 5) starts CI in that pass instead of waiting as ordinary pending. A green check for the current head beats a stored pending rollup. Acceptance results fall back to the Shuttle `RESULT.json` when `acResults` was never filled. Bugbot findings belong to the head sha, and a finding from an older sha re-requests Bugbot instead of blocking. A heartbeat, or a start that does not change the commit, the phase, or a check result, does not reset the stall clock. The stall timer is only for a stall that cannot be classified.

## 1.4.15

- The listener skill told it to read the channel once and return, so the subagent exited after a single poll. It now loops until the Warp loop is paused or stopped: poll, post acks, write `.warp/listener.json`, shell `sleep` for `pollSeconds`, and repeat. It returns on pause or stop, or returns `recycle` when its context is large.
- The parent runs `orchestrator.py supervise`. A returned listener, or a heartbeat older than `listenerStaleMinutes`, starts exactly one replacement straight away unless the loop is paused or stopped. Each restart is logged. Herald posts one note, `Listener kept restarting. One listener is running.`, after `listenerRestartNote` restarts.
- `orchestrator.py parent-exit` prints `parent: stay` while any ticket is not merged or parked. `parent: exit` is when every ticket is merged or parked, or the run is paused or stopped.
- `orchestrator.py supervise` and the dispatch tick drive each ticket through `implementing`, `pr-open`, `reviewing`, `fixing`, `ready`, `merging`, then `merged` or `parked`. Opening a pull request is not done. The Shuttle runs `checkCommand`, reports every acceptance criterion, and a failed criterion comes back as a fix run. The parent requests Bugbot on the pull request, reads the findings and the check rollup, and starts a fix Shuttle for a finding, red CI, or a failed criterion. Past `maxFixAttempts` (default 5) the ticket parks. The serial merge queue runs on every supervise pass once Bugbot is finished with no unresolved findings, the rollup is green, the acceptance criteria pass, and `autoMergeSizes` or a manual proceed allows it. After each merge the pass pushes the beam, moves the Jira issue, and posts to Slack, then starts the next ready ticket up to `maxAgents` or `maxLocalSubagents`.
- `/warp-start` and `/warp-resume` adopt a ticket that already has an open pull request into `reviewing` or `fixing`. A `lock-escape` from `checkout.py verify` calls `alarm_repair.py next` inside supervise, which widens the locks and reruns the Shuttle. `worker-died` restarts the step. `ci-red` and `bugbot-failed` start a fix run. `stuck` restarts up to `maxRecoveries`. `gate-red` stays on the gate.
- `/warp-status`, `/warp-start`, and `/warp-resume` list every ticket that is not merged. Alarms and stalls lead, then a count of each state. Each row shows the state, step, age, last active time, pull request, Bugbot, CI, acceptance, stall flag, retry count, alarm, and the next action. The same rows are stored on the beam as `openWork`.
- `orchestrator.py supervise` marks a ticket stalled when it makes no progress within the limit for its state and appends that to `.warp/tickets/<id>/log.jsonl`. Defaults are `stallImplementingMinutes` 90, `stallPrOpenMinutes` 20, `stallReviewingMinutes` 45, `stallFixingMinutes` 90, `stallReadyMinutes` 30, `stallMergingMinutes` 20, `stallQueuedMinutes` 180, `stallApprovalMinutes` 240, and `stallMinutes` 45.
- Each supervise pass tries one fix for each stalled or alarmed ticket, up to `maxStallFixes` (default 5): ask Bugbot again, rerun CI, rebase, restart a silent Shuttle, widen a lock-escape, clear a stale gate, start a base-branch fix, or return an approved pull request to the merge queue. Past the cap the ticket parks and Slack gets one alarm. A short digest posts every `statusDigestMinutes` (default 60). A new stall or alarm posts once and is not repeated.
- Every `repairSweepMinutes` (default 15), supervise sweeps the whole run for what is broken and what it can fix that is not already queued or running. That covers tickets, pull requests, CI on main, locks, gates, the listener, and the beam: red or stuck CI, a merge conflict, unresolved Bugbot findings, a stale or orphaned lock, a red base, a stuck gate, a dead listener, and a beam that is out of sync with main. The sweep reuses the fixer paths, starts work up to the slot cap, and logs findings and actions on `repairSweep` and in `.warp/sweep.jsonl`. `/warp-status` shows the last sweep. Slack is posted only when the sweep starts something new or escalates.
- The default retry cap is 5: `maxFixAttempts`, `maxRecoveries`, `maxAlarmRepairs`, and `maxStallFixes`. `listenerRestartNote` stays 3.

## 1.4.14

- Tickets start as one subagent per ticket in its own git worktree, in parallel up to `maxAgents`. `checkout.py launch` fetches origin and runs `git worktree add <worktreeRoot>/<id> -b warp/<id>-<jira> origin/main`. `worktreeRoot` defaults to `.warp/worktrees`, which is gitignored. The subagent works only inside that path, commits, pushes, and opens the pull request. It never merges, never touches the parent checkout, and never calls Jira or Slack. It writes `.warp/tickets/<id>/state.json` and `log.jsonl` into the parent checkout by absolute path and returns one line.
- `checkout.py verify` runs after each subagent. The parent checkout must be clean apart from `.warp/`, and the diff must stay inside the worktree and the locks. A miss raises `lock-escape`. Merge or park runs `git worktree remove` and `git worktree prune`. A failed subagent result raises `worker-died`. The heartbeat watchdog still covers a parent that dies mid-turn. `/warp-resume` reuses the surviving worktrees and branches.
- Optional `launch: agent` keeps the IMPLEMENT prompt for one new Agent, for a future Cursor Cloud Agents API. This plugin does not call that API.
- `subagentVm` defaults to false, so tickets use a git worktree. On a cloud runner, `true` asks for a dedicated VM: the prompt says run in your own cloud environment on a dedicated VM with its own clone and branch, not a git worktree on this machine. The subagent fetches the latest main, clones it, creates `warp/<id>-<jira>`, runs `hostname` and `free -g` first, and pushes `.warp/tickets/<id>/` on that branch. The parent patches the beam from those folders. The same hostname is a worktree on this machine: the ticket log records it, shared-machine rules apply, and one warning is posted per run. `maxLocalSubagents` defaults to 18. `memoryCheck` defaults to false, so `free -g` is not read and only that cap applies.
- `runner` is the main driver. `runner: local` forces `subagentVm` false and `launch` back to `worktree`, skips the cloud-environment start check, and overrides other cloud-only settings the same way. Start logs one note. Start, `/warp-version`, and status print the effective values.
- A cloud start with `subagentVm` true warns, and `--force` allows it, when `.cursor/environment.json` and `cloudSnapshot` are both missing. Cloud subagents use the MCP servers at cursor.com/agents, not the local session. `/in-cloud` in the Agents Window is the manual fallback, one ticket per invocation. The isolation and parallelism rules live in `rules/warp-session.mdc`.
- A local merge commits the live beam onto the base branch, then checks the previous branch back out. That checkout no longer deletes the working-tree beam when the ticket branch does not track it.

## 1.4.13

- `checkout.py launch` does not create the branch. It prints an IMPLEMENT prompt that contains the claim: ticket id, Jira key, locks, acceptance, and branch `warp/<id>-<jira>`. The Agent clones main so `.cursor` rules load, then creates that branch. A beam file does not have to exist before the Agent starts. A branch that is only the beam commit is started from latest main.
- The parent does not end the turn while the run is running and any ticket is claimed, in review, awaiting checks, or queued. Each pass starts one listener, reads `warp:` commands, fetches `.warp/tickets/<id>/`, patches the beam, dispatches, and passes again. `orchestrator.py parent-exit` prints `parent: stay` in that case and `parent: exit` when the run is paused or stopped, or when nothing is claimed, queued, or awaiting checks. Pause and stop still sync state, and then the listener may stop.

## 1.4.12

- The listener is a Subagent of the parent; each ticket is a new Agent. One listener Subagent per parent turn. It reads Slack and Teams, acknowledges `warp:` commands, and returns them. The parent applies them. It does not merge and does not implement tickets. A running flag from the previous turn does not block the next tick.
- On every merge the parent commits the live `.warp/beam.json`, journal, and board onto main. The ticket branch's beam is dropped. The same beam is pushed at the end of each parent tick. Tokens, cost, API keys, and webhook URLs are stripped. `.warp/config.yaml` is not committed.
- Close the window, open a new one, `/warp-start`. State comes from main plus ticket folders. `/warp-start` and `/warp-resume` fetch origin/main, load that beam when the checkout is empty, then patch each in-flight ticket from `.warp/tickets/<id>/`.
- `/warp-update-state` is the insurance sync. It loads main, patches `.warp/tickets/<id>/` only, puts back pause, stop, claims, and runState the parent has that main does not, and pushes the beam. `/warp-pause` and `/warp-stop` run that sync before they return.

## 1.4.11

- 1.4.11 version bump, no functional changes.

## 1.4.10

- A ticket Agent writes only `.warp/tickets/<id>/`. `state.json` holds the current state, timestamps (started, last update, heartbeat), pull request, check result, error, alarm reason, and escaped paths. `log.jsonl` is an append-only line per event. The Agent commits and pushes that directory when the state changes and on a heartbeat. It does not commit updates to `.warp/beam.json`. The launch snapshot stays read-only.
- Every tick fetches each in-flight branch and reads only that ticket's directory, then patches the live beam. It does not replace the beam. A missing heartbeat, a lock-escape line, and a check failure are read from that directory after the Agent has exited. Provider pull-request checks still count when the directory never recorded a check. Those directories may merge to main. The beam file may not.

## 1.4.9

- Cursor's plugin command index lists every Warp command and skill except `commands/warp-upgrade.md` and `skills/warp-upgrade/SKILL.md`. Those files are in the plugin root. Their frontmatter (`name`, `description`), directory, and filename match `/warp-version`. They were added after every indexed command and skill. Replacing the `./commands/` and `./skills/` globs with an explicit list did not add them. The index keeps the paths it had when it was published.
- `/warp-upgrade` stays `commands/warp-upgrade.md` with `name: warp-upgrade`, packaged the same way as `commands/warp-version.md`. Init and upgrade write that file to `.cursor/commands/warp-upgrade.md` so a reload lists it. The script is still `python3 .cursor/plugins/warp/scripts/upgrade.py`.

## 1.4.8

- `/warp-upgrade` is `commands/warp-upgrade.md` with `name: warp-upgrade`. `.cursor-plugin/plugin.json` now lists that file, and the `warp-upgrade` skill, with the other commands and skills. Reload Cursor (Developer: Reload Window) so it shows up next to them.
- `/warp-version` prints `python3 .cursor/plugins/warp/scripts/upgrade.py` and the same command with `?` when that slash command is missing from an older palette. The script does not need the slash command. A 1.4.6 tree includes `scripts/upgrade.py`. If the project copy does not, run the script from a git checkout of smlsr/Warp at main with `--source` pointing at that checkout and `--root` pointing at the project, then reload and run `/warp-version`.

## 1.4.7

- Each ticket starts as one new Agent: its own conversation, VM, and checkout. Do not start a Subagent. Do not use the Task tool. Do not implement the ticket in the orchestrator's turn.
- Before that Agent starts, `checkout.py launch` fetches origin and cuts a new ticket branch from the tip of the base branch, then commits the beam, journal, board, and that ticket's claim onto the ticket branch and pushes it. A ticket branch already in flight is not rebased. Tokens, cost, API keys, and webhook URLs are stripped. `.warp/config.yaml` is not committed. The Agent reads the claim from `.warp/beam.json` on that branch. A fresh clone of main is not started. If the branch has no beam, the Agent does not start.
- The Slack listener stays one Agent for the beam. Ticket workers are not Subagents of that listener. A local runner is still one git worktree of the ticket branch.

## 1.4.6

- User docs and the README cover 1.4.1 through 1.4.5: lock-escape repair, orchestrator merge rules, one checkout per ticket, stale gates (G1 members P-002, P-003, P-004, P-018), and `make ci`. The Install section lists every `/warp-upgrade` step. The scan footer says the beam, journal, and board are committed.

## 1.4.5

- A red `make ci` check command or CI check is stored on `pr.check` and sent back to that ticket with the log, so the run does not sit idle. The gate stays while that check is red. A green result is recorded and is not a failure. A stale gate whose members are merged and whose current checks are not red still clears. Evidence that is only the name `make ci` is not a red check.

## 1.4.4

- A pending gate, or a stale red gate, turns green when every member is merged or done, or when every ticket on the beam is. The evidence is `members merged:` and those ids, and the board is rewritten. A member that is not merged, a ticket that is still alarmed or parked, or a check that is actually red leaves the gate as it is. A recovered ticket that is merged counts. A green gate stays green.
- Every tick recomputes pending gates before `beam.py ready` and `orchestrator.py dispatch`. The listener's `alarmRepairMinutes` pass (default 15) does it again when the ready set is empty, then runs the dispatch tick in that same pass. Herald posts `G1 pending cleared. Members merged. Tick ran.` A second pass does not dispatch again. Pause and stop do not recompute.

## 1.4.3

- One checkout per ticket. On `runner: cloud` the Warp session launches one Cursor cloud agent per ticket (`environment: cloud`, `subagent_type: shuttle`). The plugin has no cloud-agent API, so `checkout.py implement` refuses to write the ticket in the orchestrator checkout. On `runner: local`, `git worktree add` and `git worktree remove` give each ticket its own worktree and branch. The beam stores the agent id and worktree path. A second agent on the same ticket, or two occupied tickets in one worktree, is refused.
- A Shuttle that exits with a pull request has finished. The slot stays occupied until the GitHub or Bitbucket check rollup is green. Bugbot runs on the pull request, not inside the VM. Locks stay until merge or park. `mergeQueue` (default false) or a detected GitHub merge queue enqueues the pull request. A merge that branch protection rejects is not marked merged.
- The beam, journal, and board are committed so the next cloud agent sees in-flight work, locks, and parked tickets. Tokens, cost, API keys, and webhook URLs are stripped. `.warp/config.yaml` stays gitignored. `/warp-init` replaces an old ignore-all `.warp/` line and leaves unrelated gitignore lines in place.
- `/warp-start` on a cloud runner refuses to start or claim when Jira or Slack is enabled and the MCP allow-list is missing `transitionJiraIssue`, `addOrEditJiraIssueComment`, or `slack_send_message`. It names the missing tools and does not claim. `--force` starts anyway. A later claim stays blocked unless the run was force-started. A local runner does not block.
- `/warp-upgrade` replaces `.cursor/plugins/warp` with the plugin this command is running from, fetching the default branch when that tree is a git checkout. A failed fetch keeps the installed copy. Config and the beam are left in place. It prints `Warp vX.Y.Z` and asks for a Cursor reload. `/warp-init` still does not replace an existing plugin copy.

## 1.4.2

- The orchestrator is the only merger. It dispatches, merges, and tracks, and it writes no ticket product code. Shuttles and Reed do not merge. The cap is `maxAgents`. A project may set 18.
- Ready requires every dependency and `after` id to be merged on the base branch, and no overlap with a lock an in-flight ticket holds, including a parent folder, using the full lock list. A green pull request frees the slot and keeps the locks until merge or park. Dispatch fills free slots from the ready set, starred and priority first, then lowest rank.
- The merge queue is serial. `checkCommand` empty means Bugbot and CI as configured. `appendOnlyPaths` defaults to empty and keeps both sides of a conflict. Any other conflict is sent back. The third red parks the ticket. A red base branch stops merging. Restart classifies merged, in flight, and pending from git.

## 1.4.1

- The one `warp-listen` listener repairs `lock-escape` alarms, and no other reason, while the run is running. `alarmRepairMinutes` (default 15) opens a pass. `alarm_repair.py next` is idempotent if the listener ticks twice. Pause and stop do not repair.
- The repair widens that ticket's locks to the paths stored on the alarm (`escaped`). It does not take a path an in-flight ticket holds. It waits, then widens and starts. A queued ticket's overlapping lock can be widened. The added paths are recorded on the ticket and in the Herald line.
- One repair at a time, in plan order. A failure leaves the alarm and starts the next ticket in that call. Success waits until the ticket completes, then starts the next. `maxAlarmRepairs` (default 3) leaves the alarm and later passes skip it. `/warp-init` backfills both keys.

## 1.4.0

- This marks 1.4.0.

## 1.3.26

- A Shuttle and the one `warp-listen` listener write a heartbeat on the beam (`lastSeenAt` and the agent id) at claim, at each status change, and while the turn is alive. `beam.py heartbeat` and `inbound.py heartbeat` do that. There is no process table. A dead turn does not notify Warp, and Cursor does not restart it. Plugin hooks do not run on cloud runners.
- `beam.py watchdog` runs on every Warp tick and on `/warp-start` and `/warp-resume`. `staleMinutes` (default 15) marks a worker dead when the heartbeat is older than that, or it never heartbeated and the claim or listener start is older than that. Pause and stop do not replace anyone. A fresh heartbeat is left alone. `/warp-init` backfills the key.
- A dead listener is cleared and exactly one replacement is reserved. Herald posts `Listener died. A new one started.` A dead Shuttle stays on the same branch, pull request, `jira.startedAt`, and locks, with status `recovering`, and exactly one new Shuttle is dispatched. Herald posts `<id> worker died. A new Shuttle started.` A second tick does not start a second worker. Past `maxRecoveries` (default 3) the ticket is `alarm` / `worker-died` and no new Shuttle starts.
- User docs match 1.3.20 through 1.3.25: a README Dead workers section, autoMergeSizes including L and XL, one listener, agreed jiraProject with no project --set, and tests that keep those sentences.

## 1.3.25

- Customize does not install a pasted GitHub URL. `.cursor-plugin/marketplace.json` lists the root plugin so a team marketplace import of https://github.com/smlsr/Warp can find Warp. `plugin.json` adds publisher, repository, and homepage. The logo remains `assets/logo.svg`. The README install section says where to paste the URL, then install from Customize and reload.

## 1.3.24

- A size in autoMergeSizes auto-merges after Bugbot and CI, including L and XL. Scan reads that list from config.yaml. Labels L, size:L, and L — … match the same token. A size not in the list stays on the manual path.

## 1.3.23

- One `warp-listen` sub-agent reads Slack and Teams for the whole run. It is one listener for the beam (`listener.state`, `listener.agentId`, optional `listener.pid`), not one per awaiting_approval ticket, Shuttle, or Reed. `/warp-start` and `/warp-resume` run `inbound.py claim` and launch it only when the reply is `listener: started`. `listener: already running` does not launch a second. `/warp-pause` and `/warp-stop` run `inbound.py release`. The listener must not keep reading while paused or stopped.
- Recognized `warp:` commands are acknowledged in the channel before they run. `warp:proceed XV-01` on an awaiting_approval ticket acks `Received warp:proceed XV-01. Merging and moving Jira to Done.` and merges only that ticket. A plan id or a Jira key both match. A bad id acks and merges nothing else. Pause, resume, stop, start, retry, and status get the same ack-then-act path. Unknown commands ack the accepted forms.
- Cursor cannot start the listener from a Slack message. There is no webhook, and plugin hooks do not run on cloud runners. If the listener's turn has ended, the flag can still say running. Release it, then `/warp-resume`. Until this build is on the live run, approve from the agent chat with `/warp-proceed XV-01`.

## 1.3.22

- Scan writes jiraProject when every stored issue key is in one project, even if getVisibleJiraProjects is missing. Mixed keys are named and left empty. A different existing value is not overwritten. The same value is left in place, with no project --set instruction.

## 1.3.21

- The first claimed ticket of a run that cannot be resolved to a live Jira issue is released and the run stops. First ticket means no ticket has `jira.startedAt` yet: no earlier ticket was linked and moved to In Progress. `record --result not-found` (or a failed claim whose error says the issue was not found) and a lookup that cannot resolve a real issue key both take this path. The ticket returns to `queued`. `agent`, `branch`, `jira.startedAt`, and `jira.previousStatus` are cleared. Implementation does not start and no pull request is opened. The run ends the same way as `/warp-stop`, with reason `tickets are not linked to Jira (WV-01 / WAR-1)`.
- Herald posts one message: `Run stopped because Jira issues are not linked (WV-01 / WAR-1). Fix jiraProject, /warp-jira-match, or /warp-jira-external-id, then /warp-resume.` It does not also post that the claim still stands.
- A later miss, after one ticket has `jira.startedAt`, stays a per-ticket alarm and does not stop the run. `jiraTransition: false` does not stop the run.

## 1.3.20

- Plugin hooks do not run on cloud runners. Resume, stop notes, and the MCP allow check now run from scripts the Warp commands already call: `resume_hint.py`, `session_note.py`, and `mcp_allow.py`. Local IDE hooks only repeat those scripts.
- `sessionStart` prints the beam hint from `resume_hint.py` on `/warp-start`, `/warp-resume`, and `/warp-status`. It does not dispatch.
- `stop` and `subagentStop` append `session-stop` and `subagent-stop` through `session_note.py`. `/warp-stop` and `/warp-pause` write `session-stop`. A Shuttle writes `subagent-stop` when it finishes. A repeat is skipped while that line is still last.
- `beforeMCPExecution` stays a local IDE duplicate. Cloud agents run `mcp_allow.py` and call a tool only when it prints `allow`. The list is the same Slack, Teams, Jira, and optional GitHub pairs. It is not every MCP tool, and it does not approve shell commands.
- The README install section lists the `mcpAllowlist` globs for Slack, Jira, Teams, and GitHub. `/warp-init` and `/warp-allow-notify` write them. Run Mode must be Auto-review, Allowlist, or Run Everything.

## 1.3.19

- Write `.warp/warp-complete.html` when every ticket is merged, done, skipped, blocked, or alarmed, and when the run is stopped. `/warp-report` writes it on demand. `--partial` writes a snapshot while work is in progress. `reportOnComplete: false` skips the automatic write. Herald posts the totals and the local path. The file stays gitignored with `.warp`.
- The report groups tickets into concurrency-run segments: the same set of active tickets is one wave, an empty gap is not a wave, and a handoff that keeps the count the same but changes who is running is two waves.
- `beam.py set` appends status, Bugbot, CI, and alarm events on the ticket and in `.warp/events.jsonl`. `beam.py usage` (and the same flags on `set`) records tokens in, out, cached, and cost. Missing numbers stay n/a. `spend` stays a separate additive total.
- Docs cover 1.3.11 through 1.3.18 again, including the version check that passes when the tree already matches `main`, plus the completion report.

## 1.3.18

- A manual merge (`warp:proceed` or a provider approval) moves Jira to Done, posts the merged comments, records sha, mergedAt, via, merge method, and who approved, releases locks, and unblocks dependents. The set prints one post-merge MUST DO, including the Slack reply with the merge sha and the Jira status. `jiraDoneOnManualMerge: false` leaves the issue at QA Ready.
- `proceed.py` accepts a plan id (`WV-01`), a Jira key (`WAR-1`), or a `#` number (`#01`). A ticket that is not awaiting approval is refused and nothing is merged.
- `/warp-jira-check` shows `merged-but-not-done` when the beam is merged and `jira.doneAt` is empty, and `catchup` prints the Done transition, the merged comment, and the exact command. If QA Ready has no path to Done, the transition picker uses the name and then one done-category transition; a miss is recorded in the outbox and Herald.

## 1.3.17

- Manual tickets run Bugbot and the fix loop before QA Ready. awaiting_approval and merge wait for a Bugbot pass and green CI. bugbotManual false skips Bugbot on the manual path only. New commits after QA Ready re-run Bugbot and leave the Jira status in place.

## 1.3.16

- The version check passes when the working tree already matches main, so a pull request that was just merged is not marked failed. Script annotations use Optional instead of X | None so the scripts parse on Python 3.9.

## 1.3.15

- Skipped. No separate change; rolled into 1.3.16.

## 1.3.14

- 1.3.14 version bump, no functional changes.

## 1.3.13

- /warp-init writes the project MCP allowlist for Slack, Teams, and Jira. Entries include user-, plugin-, and *name* variants because the Run dialog often does not use the mcp.json key. server:* stays behind --allow-server-tools. --check and --list diagnose a prompt. autoAllowTools defaults to true. --no-allow skips one init. User-level files still need --user --yes.

## 1.3.12

- /warp-jira-external-id remembers a missing External ID field and, by default, adds label warp:<id> with editJiraIssue update.labels add. Field creation stays off unless jiraCreateExternalIdField or --create-field --yes. The Rovo MCP catalog has no create-field tool; a missing tool or a denial is not a failure. getJiraScreen and updateJiraScreen only add a field that already exists. --recheck looks again. jira.externalIdWritten.method is field, label, or remote-link. No comment is added.

## 1.3.11

- /warp-jira-external-id and scan/catchup write existing plan-id mappings into Jira External ID when jiraWriteExternalId is true. The config flag is the consent, so scan does not need --yes. Keys found by the external-id search are already equal and are skipped. record-external-id records each edit. A missing or read-only field is skipped. A different non-empty value needs --force-external-id. No comment is added.

## 1.3.10

- Docs now cover 1.3.3 through 1.3.9: plan id versus Jira key, external-id lookup, jiraProject detection, /warp-jira-check, /warp-jira-view, and /warp-jira-match. The README has a command table, upgrade steps, and a troubleshooting tree. GUIDE.md walks Jira from import to merge.

## 1.3.9

- /warp-jira-match links unmapped tickets to Jira by summary. Exact and a 60-character prefix are stored with --apply. A fuzzy score at or above 0.9 is a proposal until --yes. Ambiguous matches are not stored. --write-external-id writes the plan id into the External ID field with editJiraIssue after --yes, skips a missing or read-only field, and does not add a comment. jiraWriteExternalId defaults to false, so scan and claim do not write that field.

## 1.3.8

- /warp-jira-view prints every field on a Jira issue. An issue key such as WAR-1 is fetched with getJiraIssue. An external id such as WV-01 is resolved from the beam and the map, then from the external-id field, and from each visible project when jiraProject is empty. Two matches are listed and neither issue is printed. --comments, --links, --all, --full, --verbose, and --json control the report.

## 1.3.7

- A claim that finds a Jira issue by external id now has to record it before any transition: resolve --ticket WV-01 --key WAR-1 --issue-id ID --cloud-id CLOUD writes the beam and the map together, and the transition todo uses only that key. A plan id is refused. Jira import JSON uses externalId as the plan id (h2. Size, Locks, Blocked by, Acceptance, and size/auto-merge/area labels) and never as jiraKey. When several Jira projects are visible, project --probe and project --record pick the one that contains the plan id. verify prints key stored or key missing, jira.id, and jira.lastAttempt from record --result failed --error.

## 1.3.6

- /warp-jira-check prints why each ticket is unmapped and the one command that fixes it. verify with no flags only reads the beam and does not call Jira. verify --link copies a key already in the map file and writes the same JQL as map --from-jira. verify --apply stores one exact match. A needs mapping line on this report does not mean the lookup already ran.

## 1.3.5

- jiraProject is detected instead of hand-edited. Init and scan write it when one prefix is clear from the plan, branches, or recent commits, and ignore plan-id prefixes such as WV that have no Jira key. Several prefixes are not guessed. When Jira is connected, one visible project or one match is stored, and a single site is stored in jiraSite. An empty jiraKeyPrefixes becomes that project. A value already set is left alone. The warning is: jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC).

## 1.3.4

- Jira auto-match checks the external-id field first (configured name or customfield id, then External ID, External Id, ExternalId, External Key, Plan ID, and Ticket ID), then a warp:<id> label and a remote link. One exact hit is stored with its source and confidence. A summary match waits for --yes. Two matches are reported and neither is stored. A manual key is never overwritten. map --from-jira and --auto apply a saved search; --dry-run writes nothing.

## 1.3.3

- A plan id such as `WV-01` is not sent to Jira. A key is kept when its prefix matches `jiraProject` or `jiraKeyPrefixes`, or it was set on the plan, in a Jira export, in the map file, or by hand.
- `/warp-scan` stores `schedule.json` `jiraKey`, a markdown `Jira:` line, a `Jira Key` column, `[WAR-1]` in the heading, `.warp/jira-map.json`, and `jiraKeyMap`. A rescan keeps a key set with `beam.py set --jira`.
- Scan and the first claim look up Jira's external id field. One exact match is stored. The summary line is `N tickets: K keyed, U need mapping`. `/warp-jira-map` is only for a ticket that stays unmapped or ambiguous, or for an override.
- A claim with no key searches before it flags. A miss or an ambiguous result goes to `.warp/outbox.md` and Herald and does not call Jira. `catchup` then asks for the transition the current status still owes.
- Config keys `jiraKeyPrefixes`, `jiraKeyMap`, and `jiraExternalIdField`. `/warp-init` adds them to an existing file.

## 1.3.2

- README covers `/warp-allow-notify` (project and `--user --yes`, dry run, revoke, Jira and GitHub tools, `notifyAllow`, backups, idempotency, and which Cursor surface each file changes). `docs/COMMANDS.md` is the command reference. `docs/CONFIG.md` lists every config key and its default.
- Quick start, upgrade (`/warp-uninstall` then `/warp-init`, or init alone to backfill keys), and troubleshooting (Run prompts, Jira not moving, no Slack message, channel name).
- Scripts people run directly accept `?`, `help`, `-h`, and `--help`. `?` is always help. A bare `help` after an option that takes a value stays that value.
- `beam.py` no longer defaults `jiraProject` to `HOS` when a config file is absent. The default is empty, matching `assets/config.example.yaml`.

## 1.3.1

- `/warp-allow-notify` writes a specific MCP allowlist so Slack and Teams posts, and optionally the Jira and GitHub tools Warp calls, can run without a Cursor Run prompt.
- `/warp-version` prints the installed plugin version and, when it differs, the source copy. `/warp-init` and `/warp-status` print it too. Herald init and scan messages end with `Warp v1.3.1`.
- Init records the installed version in `.warp/version`. When the project copy is older than the source copy, it says to run `/warp-uninstall` then `/warp-init`.
- Every pull request must bump the patch version and add a changelog entry. `scripts/bump_version.py` does both. CI rejects a pull request whose version is not newer than main.

## 1.3.0

The manifest stayed at 1.3.0 from `/warp-init` through the Jira comment work. Those changes shipped without a later bump:

- `/warp-init`, `/warp-uninstall`, and `/warp-scan` with an optional folder.
- One Herald formatter, header `Warp | <repo> / <project>`, and a shared lowercase `warp` channel. Warp does not invite anyone to the channel.
- Jira moves to In Progress on claim, to QA Ready or Done on the merge path, and comments on the issue and the pull request. `/warp-jira-check` shows what is missing. Re-running init adds config keys that an old file lacks.
- Git provider `auto`, GitHub, or Bitbucket, and local-only merges when there is nothing to push.
- Default coding model `claude-sonnet-5-5-high`.

## 1.2.0

- Initial plugin: scan a plan, dispatch workers, post status to Slack and Teams, and keep the beam out of git.
