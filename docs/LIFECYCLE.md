# Agent lifecycle

This is how Warp starts agents, how they end, and how a run is torn down. It is the rule the commands, skills, and prompts follow. Read it before changing any of them.

## The rule

One agent is long-lived: the parent. The parent is the only agent that starts another agent, and the only agent that waits. A Shuttle does one step and returns one line. The listener is one background subagent on the parent's checkout and does not start agents. That is tick-and-return for every Shuttle.

| Agent | Lives for | Started by | Returns |
|---|---|---|---|
| Parent (the orchestrator) | the run | you, with `/warp-start` or `/warp-resume` | ends its turn on `parent: exit` |
| Shuttle | one step of one ticket: implement, fix, restart, or repair | the parent, from `checkout.py launch` | `result: <id> ok ...`, `failed ...`, or `stopped <reason> agent=<agent>` |
| Listener | the run, one background subagent on the parent's checkout | the parent, on `listener: poll`, and only after the return stamp | `listen: stopped`, `listen: rotate`, or `reap: exit <reason>` |

Reed and Herald are roles. The parent usually plays them in its own turn. When it starts one as a subagent, the same rule holds: one step, one return, and it starts nothing.

A worker never starts an agent, never subscribes to anything, never sets a timer, never loops, and never waits on a pull request, CI, Bugbot, Slack, or an approval. If any of those is needed, the worker returns and the parent does it on a later pass.

## Run control

Four commands, two behaviors.

`/warp-start` and `/warp-resume` are the same command. Either one sets the run running, from a stopped run, a paused run, or a run that is already running. It prints which: `run: was stopped. Starting.`, `run: was paused (<reason>). Continuing.`, or `run: was already running. This session takes it over.` Both run the same prompt gate, and `--force` means the same on both. The run state is never a reason to refuse, and there is nothing to choose between them. A named run can still refuse: it has no scan, it is live on another machine (`--takeover`), or its `startAfter` is not met (`--now`). See "Named runs".

`/warp-pause` and `/warp-stop` are the same teardown. One thing differs.

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

### What start does first

Start and resume check for unfinished work before anything new, and print it:

```text
unfinished: 6 open. 1 need a fix, 3 need a Shuttle, 0 have a Shuttle out, 2 wait on review or checks. These start first, up to maxAgents 18. New tickets start after, while fewer than maxInProgress 20 are open.
```

The order is fixed: open pull requests that need a fix (a conflict, CI that never started, red CI, Bugbot findings), then tickets whose Shuttle was halted or died, then new tickets from the ready queue. `unfinished: none` means the run starts from the ready queue.

### Two caps

| Key | Counts | Default |
|---|---|---|
| `maxAgents` | Agents that run at once: every Shuttle, whatever its step, in a worktree on this machine or on its own VM | 18 |
| `maxInProgress` | Tickets that are open at once: started, and not merged or parked. A ticket waiting on review or CI is open and uses no agent | 20 |

A fix, a rebase, or a relaunch is an agent like any other. It takes a `maxAgents` slot, and it starts even when `maxInProgress` is full, because its ticket is already open. `maxInProgress` holds only new tickets. Unfinished work draws on the slots first, so a new ticket starts only in a slot unfinished work did not need. `memoryCheck: true` can lower `maxAgents` on a shared machine when memory is tight.

`maxLocalSubagents`, `maxFixWorkers`, `listenerStaleMinutes`, `listenerRestartNote`, and `maxListenerRestartsPerHour` are retired ([CONFIG.md](CONFIG.md) "Retired keys"). A config that still has them prints `config: <key>: <value> is retired and ignored.` at start and in `/warp-version`.

## Why

Agents pile up when four things are true together. A worker can start a worker. A worker waits for something instead of returning. Nothing ends an agent that is no longer wanted. And nobody can list what is out. Each mechanism below removes one of those.

Plugin hooks do not run on cloud runners. Nothing here depends on a hook. Every check is a script that a command, a skill, or a prompt runs. The hooks in `hooks/` are local IDE duplicates of the same scripts.

## Five mechanisms and a tag

### 1. The gate

A prompt for a Shuttle comes from one place, `checkout.py launch --id <id>`. It records the row in `.warp/agents.json` before it prints. It prints nothing, and exits 2 with `spawn: closed <id>`, while the run is paused, stopped, or finished, and for a ticket that is merged or parked. The pipeline (`orchestrator.py supervise`) decides which ids to launch: `start <id>` is a new Shuttle, `resume <id>` continues the one that returned, and `shuttle: hold`, `spawn: cap`, and `spawn: closed` mean start nothing.

The output has two parts. Lines above `prompt: pass every line below this one to the subagent, and nothing above it` are for the parent. Lines below it are the worker's prompt. The parent's instruction to start workers in parallel never reaches a worker.

The listener's prompt comes from `orchestrator.py supervise`, after `listener: poll`, and only when no holder is live and the return stamp is in.

### The tag

Every agent Warp starts is tagged `warp:<instance>`.

The instance is six characters that name one Warp run, which is one beam. `/warp-start` sets it once and it stays on the beam (`instance` in `.warp/beam.json`, pushed to main with it). On a cloud runner it is the last six characters of the parent's cloud agent id. On a laptop one machine can run Warp in several checkouts, so it is six characters of a hash of the machine id and the beam's path. It belongs to the run, not to one parent: a `/warp-resume` from another window or another VM keeps it, so the new parent still finds the agents the earlier one started. Two Warp runs, in one repo or in different repos, have different instances.

Cursor has no tag, label, or metadata field on an agent. The name is the one label its API returns in the agent list, so the tag is the name.

| Agent | Name |
|---|---|
| Shuttle | `[warp:<instance>] <ticket> <step>`, for example `[warp:a1b2c3] WV-01 implement` or `[warp:a1b2c3] WV-01 fix` |
| Listener poll | `[warp:<instance>] listener` |

`checkout.py launch` prints `name: [warp:<instance>] <ticket> <step>` above the `prompt:` mark, and the parent gives the subagent exactly that name. `supervise` does the same for the listener. The same text is the first line of the prompt, so the tag is there when Cursor derives a name from the prompt.

The instance is shown wherever you would look for it:

| Where | What it shows |
|---|---|
| `/warp-start`, `/warp-resume`, `/warp-status` | `instance: warp:a1b2c3 host=<host> machine=<machine id>` |
| `/warp-version` | the same line, and `this machine: <machine id> host=<host>` for the machine the command ran on |
| `agents.py list` | the same line first |
| `.warp/STATUS.md`, `.warp/status.json` | `Instance warp:a1b2c3 ...`, and an `instance` object |
| Every Slack and Teams message | the header: `Warp | <repo> / <project> | run <name> | warp:a1b2c3`. `| run <name>` appears only on a named run |
| The status post | a fact, `Instance: warp:a1b2c3 on <host>` |

The tag is how this run's agents are told apart from every other agent, including another Warp run's. A dry run lists them with their status, which is how you see one that is still running:

```bash
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud
```

It prints `cleanup: tag [warp:a1b2c3]` and then each match. Pause, stop, and `/warp-cleanup` act on an agent only when it is in the registry or its name or prompt starts with this instance's tag. Words like "fix" or "listener" in another agent's name do not match. Another Warp run's tag does not match. `--tag <instance>` names another run's tag in place of this one, and `--tag all` matches the tag of any Warp run, for what a run left behind after its beam is gone. `--untagged` matches agents that carry no Warp tag at all, loosely, for a pile from before 1.5.0.

The parent is not tagged: its name comes from what you typed. It is in the registry as the `parent` row, and it is never cancelled or archived.

An agent that shares the parent's session, which is every worktree Shuttle and the listener, is not a cloud agent of its own and does not appear in Cursor's agent list. The registry row is its record, and the reap check is how it ends.

Cursor's API has no call that lists an agent's timers or subscriptions. Cancelling the run and archiving the agent is what Warp does to end them.

### 2. The contract

Every worker prompt carries its agent id (`agent: <id>`), the contract, and the reap command:

> You are one worker for one ticket. Do this step and return one line. You never start another agent of any kind. You never subscribe to anything, set a timer, loop, sleep, or wait on the pull request, CI, Bugbot, Slack, or an approval. The parent does all the waiting. If this conversation is woken later by anything that is not a new step from the parent (a CI result, a pull-request comment, a timer), run the reap check and return. Do not act on it.

A heartbeat is not a timer. A Shuttle writes it between steps of work it is already doing.

### 3. The reap check

Every Warp agent asks one question first, and again after each state it writes: continue, or exit.

```bash
python3 <plugin>/scripts/agents.py reap --beam .warp/beam.json --id <agent> --ticket <id>
python3 <plugin>/scripts/agents.py reap --remote --id <agent> --ticket <id>
python3 <plugin>/scripts/agents.py reap --beam .warp/beam.json --role parent --id <session>
```

It prints `reap: continue`, or `reap: exit <reason>` and one line saying how to stop, and exits 3. `ticket_state.py append` and `beam.py heartbeat` print the same line, so a Shuttle that writes its state reads the answer without a second command. `inbound.py poll` and `inbound.py wait` print it for the listener.

| Reason | Meaning |
|---|---|
| `paused`, `stopped` | `/warp-pause` or `/warp-stop` ran. An agent that was out at that moment keeps this answer after the run resumes |
| `done` | every ticket is merged or parked |
| `settled` | this ticket is merged or parked |
| `replaced` | the parent started another Shuttle for this ticket, the ticket moved to another VM and this is the one it left, or another parent took the run |
| `halted` | this agent had already returned when a pause or a stop ran, and the run moved on without it |
| `archived` | cleanup archived this agent |
| `not-resumed` | the turn returned more than `staleMinutes` ago and the parent did not resume it |
| `not-launched` | no launch was recorded for this id: something other than the parent started it |
| `superseded` | listener only: its lease generation or holder no longer matches. Another copy, or a `/warp-listen` chat, holds the lease |
| `orphaned` | listener only: `listener.parentSeenAt` is older than `listenerOrphanMinutes` (default 5). The parent is gone. Nothing replaces the listener until a parent runs `supervise` again |

A Shuttle in a worktree reads the parent's beam on disk. On `reap: exit` that check ends the agent's row and stamps `reaped`, so `agents.py list` shows who is gone.

A Shuttle on its own VM passes `--remote`, which reads the beam and the registry from origin's base branch and writes nothing. Pause and stop push both before they return, and start and resume push before they print a `start` line, so the remote check sees the run as it is. The VM does not end the row and does not stamp `reaped`. It returns `result: <id> stopped <reason> agent=<agent>`. `<agent>` is that Shuttle's registry id or its cloud id, the same id `reap` matches. The parent records the line with `checkout.py result`. That ends only the matching row, and only while it is still live, and stamps `reaped` on it. When the line does not name an agent, the ticket folder's `cloudAgent` is that id. A late result from a halted or replaced VM does not match the Shuttle that replaced it, so the new row stays live. The parent journals that result as `reap-stale`. A row a halt already ended keeps its end reason. A second record leaves the stamp where it is.

### 4. The halt

`/warp-pause` and `/warp-stop` run the same teardown. The only difference is what comes after: a stopped run also writes the completion report. Either one continues with `/warp-start` or `/warp-resume`.

1. `runState` becomes `paused` or `stopped`.
2. Every row in `.warp/agents.json` is ended (`stop: <id>`) and marked `halted`. Every Shuttle slot is freed. The listener's poll is closed. The line is `halt: <state> agents=<n>`.
3. The beam, journal, and registry are pushed to the base branch, so agents on other VMs can read the halt.
4. With `CURSOR_API_KEY` set: every cloud agent in the registry is read by id, has its run cancelled if one is going, and is archived. Then the account's agent list is swept for agents in this repo whose name carries this run's `[warp:<instance>]` tag, and those get the same. The registry's agents go first, so they do not depend on the list. The sweep reads the list once and looks further only at tagged names, so other agents on the account cost nothing. The agent running the command and this run's parent are skipped. An agent the API cannot read is left alone (`cleanup: unreachable <id>` and its link). The teardown gives up after 45 seconds and says `cleanup: cut short; run /warp-cleanup for the rest`. A failed call never aborts the pause or the stop.
5. Without the key: `cleanup: link https://cursor.com/agents/<id>` for each recorded cloud agent. Archive those in the Cursor UI.
6. The parent's next `parent-exit` prints `parent: exit`. Each worker's next reap check prints `reap: exit`.

A subagent that shares the parent's session cannot be cancelled from outside. It exits at its next reap check. A cloud agent with its own VM can be, and is, when the key is set.

Nothing from before a halt is resumed. `/warp-resume` starts a new Shuttle with a new agent id on the same branch, worktree, and pull request. The lines are `start <id> ... step=restart` and `fix: <id> resume after halt`. The ticket passes through `recovering` like any relaunch, but it is not counted as a recovery and nobody is told a worker died. Work that a cancelled VM had not pushed stays in that archived agent. Unarchive it in the Cursor UI to get it back.

The same teardown runs once when every ticket is merged or parked (`halt: done`).

A pause or a stop wins over work that is already under way. A pass that was reading and writing the beam when the halt landed does not write over it: `supervise` prints `pass: aborted. The run was paused during this pass.`, ends anything that pass recorded, and the parent starts nothing from it. A `checkout.py launch` that was fetching when the halt landed prints `spawn: closed <id>` and no prompt.

`warp:resume` and `warp:start` from the channel do nothing while the run is already running. The parent that applies them owns the run, and a new session there would leave the run with no parent.

### 5. The list and the clear

Two commands, and they take the same selection.

| Command | What it does |
|---|---|
| `/warp-list` | Shows what is still out for this run, running or idle. Changes nothing |
| `/warp-cleanup` | Cancels the run of each one that is running, and archives all of them |

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
```

`/warp-list` prints the instance, every registry row, each tagged cloud agent with its status, and `out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>`. `/warp-cleanup` never runs on its own. Leave `--apply` off for a dry run.

| Option | Selection |
|---|---|
| none | This run: the registry, and cloud agents in this repo tagged `[warp:<instance>]` |
| `--tag <instance>` | Another run's tag in place of this one. `a1b2c3`, `warp:a1b2c3`, and `[warp:a1b2c3]` are the same |
| `--tag all` | The tag of any Warp run. `--tag '*'` is the same |
| `--untagged` | Also agents with no Warp tag, from before 1.5.0, matched loosely on Warp role words in the name or prompt (Shuttle, IMPLEMENT, fix, rebase, listener, Bugbot) |
| `--all-idle` | Every idle agent in this repo |
| `--any-repo` | Do not limit to this repo |
| `--scan <words>` | Every cloud agent the key can see whose name, summary or description, or first prompt contains one of the words. Ignores the repo filter and the tag and registry rules |

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --scan="Jira","Fix","Bugbot"
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --scan Jira,Fix,Bugbot
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --scan Jira --scan Fix
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --scan Jira,Fix,Bugbot
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --force --scan Jira,Fix,Bugbot
```

`--scan="Jira","Fix","Bugbot"`, `--scan Jira,Fix,Bugbot`, and `--scan Jira --scan Fix` are the same three words. Matching is case-insensitive. Each list match prints id, name, repo, status, updated time, `word=` and `field=` (`name`, `summary`, `description`, or `prompt`), and a link, then `scan: count`. `--scan` crosses repos: it pages through every agent the key can see, so check that list before `--apply`.

Without `--apply`, cleanup is a dry run. It prints `cleanup: would archive <id>` for an idle match and `cleanup: would cancel <id>` for a running match that `--force` would cancel, then `cleanup: count`, and changes nothing. With `--apply`, idle matches are archived. A RUNNING or ACTIVE match is left (`reason=running-scan`) unless `--force`, which cancels it and then archives it. The refusal while this run is running still applies: pause or stop first, or pass `--force`. Archive only. Warp never deletes an agent. The agent running the command (`reason=self`) and this run's parent (`reason=parent`) are never matched.

The agent running the command and this run's parent are never touched. While this run is running, `/warp-cleanup` archives the idle ones and refuses the running ones: pause or stop first, or pass `--force`. An agent that only matched loosely and is still running is left alone (`reason=running-untagged`) unless `--force`, because it may be someone else's. A running `--scan` match is left alone (`reason=running-scan`) unless `--force`.

Archive is reversible in the Cursor UI. Warp never deletes an agent.

## Named runs

The instance (`warp:<instance>`) names one beam. A run name (`run:` in the config) names a run that is saved in the repo. A named run keeps the same instance on its beam, and the instance comes back when another agent takes the run up. Local files stay at `.warp/`. The parent publishes the shared files (beam, journal, `agents.json`, status, stats) to `.warp/runs/<name>/` on the base branch after each merge and at each checkpoint (`checkpointMinutes`, or when status changes). A ticket's own folder rides on its ticket branch. One run has one parent; a second `/warp-start` on the same run takes it over, as in "Run control". Pause, stop, and `/warp-cleanup` act on their own run's registry and tag, so they leave the other runs in the repo alone. See [RUNS.md](RUNS.md).

A named run also has an owner (`owner` on the beam: user, machine, host, time, session). A start from the same machine takes the run over silently. A start from another machine while the published run is live refuses (`refuse: <name> is running under ...`) unless `--takeover`; the earlier parent then exits `parent: exit superseded`. Supervise refreshes other runs' published status every 5 minutes at most, and `blocks.py check` runs in each pass: when a `/warp-start G0-G3` range is complete, the run pauses and Herald posts the block summary. Slack and Teams commands address a run by name (`warp:pause admin`, `warp:start admin G2-G3`); with more than one run in the channel an unaddressed state command is refused.

## One parent pass

The parent runs passes in one turn until `parent: exit`.

1. `inbound.py apply-pending`. Queued `warp:` commands are applied. A pause or stop here is the halt above.
2. `orchestrator.py supervise --beam .warp/beam.json`. The first line is `run: pass <n> at <time>Z`. The pass number goes up by one each pass, so a gap means an output was lost (an overwritten terminal); the beam still has the state. Before the listener it checks merged pull requests for late Cursor Bugbot findings (`bugbot: late …`, `run: reopened for Bugbot follow-ups`). Then the listener, then the pipeline.
   - `listener: poll`, then the prompt (first line `[warp:<instance>] listener`, then `LISTEN`): start one `warp-listen` subagent in the background with those lines and do not wait. `subagentVm` does not apply. When `listenerModel` is unset the head line says `model: inherit`. When it is set the head line says `model: <slug>`.
   - `listener: hold reason=live holder=<id> generation=<n>`: that holder already has the lease. `listener: hold reason=await-return generation=<n>`: the lease was bumped and the return stamp is not in. Start nothing. Only a return stamp, or a holder that has not polled or waited for `listenerSilentMinutes` (`listener: lost`), starts one.
   - `listener: idle`: the run is paused, stopped, or finished. `listener: none`: no `slackChannel` or `teamsChannel` is set. Start nothing.
   - `listener: lost holder=<id> generation=<n> (no poll or wait for <m>m). Its slot is free.`, then `listener: poll`: the holder stopped polling and waiting (Cursor can stop it as a loop). Start the one listener the `listener: poll` line names.
   - `start <id> ...`: run `checkout.py launch --id <id>` and start one subagent with the lines below the `prompt:` mark. Start the ready ones together, up to `maxAgents`, in the background so the pass goes on. A foreground batch also works: the pass continues when they return.
   - `resume <id> ...`: send that same Shuttle the follow-up. Do not start a second one.
   - `shuttle: not launched <id>`, then the `start` or `resume` line again: an earlier pass printed it and nobody launched it. Launch it now with `checkout.py launch`.
   - Everything else (`bugbot: request`, `merge:`, `jira: MUST DO`, `slack:`) is the parent's own work. Before a merge, supervise runs the merge check on the pull request's head (`merge: check green`, `red`, or `skipped`); a red check is not merged and goes to a fix. The merge uses `mergeMethod` (a merge commit by default). After it, supervise deletes the branch when `deleteBranchOnMerge` is on and publishes the run state to the base branch itself (`state: published (merged <id>)`).
   - `bugbot: not started`, `bugbot: running …`, `bugbot: done …`: what Cursor Bugbot is doing on each head. The run is waiting on it, not stalled.
   - The pass's `herald:` lines are already in `.warp/notify-pass.json`. The last lines say to post it (`herald: post .warp/notify-pass.json …`) and then run `notify.py posted`. Do not post the `herald:` lines one by one.
   - `run: stopped …` or `run: paused …`: start nothing. Go to `parent-exit`.
3. When a Shuttle returns, record its line (`checkout.py result`, `checkout.py verify`, `session_note.py --type subagent-stop`). `result: <id> stopped <reason> agent=<agent>` is how a VM Shuttle reports `reap: exit`. `checkout.py result` ends that agent's row when it is still live and stamps `reaped`. A result whose agent is not the live Shuttle is `reap-stale` and leaves the new row alone.
4. `orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>`. This is the one wait in a run. It blocks until a ticket folder changes, the inbound queue changes, the listener writes its return stamp, the run halts, or `pollSeconds` pass (`bugbotPollSeconds` while Cursor Bugbot runs), and prints `pass: ticket <id>`, `pass: command`, `pass: return`, `pass: halt`, or `pass: timeout`, then `bugbot: waiting on Cursor Bugbot: …` when anything waits on it, then `run: next is pass <n>`. A listener that went silent past `listenerSilentMinutes` also wakes it (`pass: return`), so the next supervise replaces it at once. Each step writes `listener.parentSeenAt`. `pollSeconds` is that cap, not the channel-read interval.
   - `parent: stay`: run the next pass now.
   - `parent: exit`: run `session_note.py --type session-stop` and end the turn. Start nothing. The exit closes a listener lease it still shows (`listener: generation <n> closed with the parent's exit.`), so `.warp/listener.json` never shows a lease running after the parent left. While a merged ticket still waits for Cursor Bugbot's report (up to 60 minutes after its merge), the run is not finished and the parent stays.
   - `parent: exit superseded`: a later `/warp-start` or `/warp-resume` owns the run. End the turn at once.

`<session>` is the id `scan.py start` and `scan.py resume` print as `parent: session <id>`. One parent per run: a second start or resume takes the run over, and the earlier parent exits on its next `parent-exit`.

If the parent's turn dies, nothing restarts it. Workers finish their step, write their ticket folder, and return. Run `/warp-resume`. State comes from the base branch plus the ticket folders.

## The listener is one background subagent

One listener stays up on the parent's checkout. `subagentVm` does not apply. `supervise` prints `listener: poll` when no holder is live and the previous generation's return stamp is in (`polled`, `rotated`, `orphaned`, `stopped`, `lost`, or `cut-off`), or when none has started. The parent starts that one in the background and does not wait. A gap of zero listeners is preferred over two. Lease expiry bumps the generation and does not start the replacement in the same moment. The repair sweep does not start one.

Cursor stops an agent whose tool calls repeat (it stopped the listener after nine poll, read, and wait cycles). So a generation is short on purpose: it rotates after `listenerMaxReads` (4) reads, each `wait` sleeps `listenerSlowSeconds` (120), and every `poll` and `wait` prints its cycle number and time, so no two calls print the same thing. A listener that has to stop before the script says so (Cursor flags it, its turn ends) returns `inbound.py returned --reason cut-off`, which is always accepted from the holder and frees the slot. One that is stopped without a word is lost once it has not polled or waited for `listenerSilentMinutes` (default 5): `parent-exit --wait` wakes, and `supervise` prints `listener: lost …` and starts the next one. `listenerStaleMinutes` is retired; `listenerSilentMinutes` is the new key.

```bash
python3 <plugin>/scripts/inbound.py poll --beam .warp/beam.json --lease <gen> --holder listener
python3 <plugin>/scripts/inbound.py accept --beam .warp/beam.json --text "warp:proceed XV-01" --by <who> --source slack --message-id <ts>
python3 <plugin>/scripts/inbound.py enqueue --beam .warp/beam.json --text "warp:proceed XV-01" --by <who> --source slack --message-id <ts>
python3 <plugin>/scripts/inbound.py wait --beam .warp/beam.json --lease <gen> --holder listener --cursor <newest> --count 1
```

`poll --lease` runs before any read. A generation or holder mismatch prints `reap: exit superseded` and no channel line. A halt prints `reap: exit paused`, `stopped`, or `done` the same way. An orphaned parent (`listener.parentSeenAt` older than `listenerOrphanMinutes`, default 5) prints `reap: exit orphaned`. Rotation at `listenerMaxMinutes` or `listenerMaxReads` (default 4) prints `listen: rotate`. Otherwise it prints `reap: continue`, `listen: cycle <n> of <max> at <time>Z (generation <g>)`, and `listen: slack channel=<name> since=<cursor>` for each channel. The listener reads messages after that mark, accepts and queues each `warp:` command, and posts the ack. A duplicate Slack ts prints `inbound: duplicate` and writes no second ack. `wait` sleeps in Python in 2-second steps, `listenerFastSeconds` (15) after a queued command and `listenerSlowSeconds` (120) after an empty read, and returns early on halt, a lease mismatch, an orphan, or rotation. Pause and stop reap it within about 2 seconds. A read already in flight finishes, then the next poll reaps it. `listen: wait` is followed by `listen: cycle <n> of <max> ended at <time>Z. Run poll again …`: poll again with the same lease. The script writes the return stamp on the way out. The parent starts the next generation only from that stamp.

`/warp-listen` is a chat you open yourself. `inbound.py take --holder chat` bumps the generation so any other copy exits superseded at its next wake. Same loop. The parent only applies, woken by the pending file.

`listen: stopped` idles only the supervise pass that still had `pollStartedAt` set, or when the run should not listen. A later pass with the same argument starts the next generation when tickets are still open. Explicit `pause`, `stop`, `warp:pause`, and `warp:stop` still idle.

The registry keeps one `listener` row for the run, with a `polls` count. A generation is not a spawn and never counts against a spawn cap. Slack stays on the plugin's MCP tools. There is no bot token. `agents/listener.md` sets `is_background: true` and no model key. A live run still has to confirm that `is_background` and the model field work.

## One ticket, one cloud agent

A Shuttle on its own VM writes its cloud agent id into its ticket folder (`cloudAgent` in `state.json`, written by `ticket_state.py append --push`). A worktree Shuttle shares the parent's VM and reports none. The parent binds that id to the Shuttle's row as `cloudId`. Four things follow.

- A halt cancels and archives that agent by id.
- A fix round that lands on a new VM retires the VM the ticket left. So does replacing a dead Shuttle. The next pass cancels and archives the retired VM, so a ticket never holds more than one cloud agent.
- A retired VM that wakes up reads `reap: exit replaced`, even though it carries the same agent id, and it never becomes the live VM again.
- Merge and park cancel and archive every cloud agent the ticket used.

The parent's own cloud agent is never bound to a Shuttle and never archived.

## The registry

`.warp/agents.json` is the one registry. The beam carries a copy for the pass that is running, and the file wins whenever the two differ.

| Field | Meaning |
|---|---|
| `id` | `subagent:<ticket>`, `shuttle-<ticket>-rN`, `listener`, `parent:<session>`, `bugbot:<ticket>:<sha>`, or a cloud id (`bc-...`) |
| `role` | `shuttle`, `listener`, `parent`, or `bugbot` |
| `state` | `starting`, `running`, `ended`, or `stopped` |
| `endReason` | `returned`, `dead`, `replaced`, `merged`, `parked`, `paused`, `stopped`, `done`, `archived`, `polled`, `rotated`, `orphaned`, `lost`, `retry` (`warp:retry` ended the Shuttle). A listener row ends `polled`, `rotated`, `orphaned`, or `stopped`, the generation's return stamp |
| `halted` | the row is from before a pause or a stop, and is never resumed |
| `reaped` | when the agent read `reap: exit`. A worktree Shuttle stamps its own row. A VM Shuttle does not: the parent stamps the row named by `agent=` or `cloudAgent` |
| `cloudId`, `retired`, `archivedIds` | the VM the Shuttle is on, VMs it left, and VMs already archived |
| `superseded` | a parent row a later start replaced |
| `polls` | how many polls the listener row has run |

`spawns` is one entry per Shuttle launch the pipeline issued and per Bugbot request. `maxSpawnsPerTicket` and `maxSpawnsPerHour` count those. A listener poll is not a spawn.

## What Cursor does on its own

A cloud agent that opens a pull request can be woken by Cursor when CI fails on it or a comment arrives, depending on the Cloud Agents settings for the account. That wake-up is not from the parent. The contract tells the worker to run the reap check and return without acting, and the parent handles red CI on its next pass. To stop the wake-ups at the source, turn off automatic CI fixes for Cloud Agents in the Cursor dashboard for the repos Warp runs in.

## Lines

| Line | Printed by | Meaning |
|---|---|---|
| `instance: warp:<instance> host=<host> machine=<id>` | `scan.py start`, `resume`, `status`, `version.py`, `agents.py list` | which Warp run this is. Its agents carry that tag |
| `parent: session <id>` | `scan.py start`, `scan.py resume` | keep this id for `parent-exit --session` |
| `pass: ticket <id>` / `command` / `return` / `halt` / `timeout` | `parent-exit --wait` | why the wait ended |
| `pass: aborted. The run was <state> during this pass.` | `supervise` | a pause or stop landed mid-pass. Start nothing from it |
| `parent: stay` / `parent: exit` / `parent: exit superseded` | `parent-exit` | run another pass, or end the turn |
| `listener: poll` / `hold reason=live` / `hold reason=await-return` / `idle` / `none` | `supervise` | start one background listener, or start nothing |
| `acceptance: verify-shuttle <id> <n>/2 missing <ids>` | `supervise` | the work is done but acceptance results are missing: launch the `step=verify` Shuttle it started |
| `acceptance: MUST DO <id>: ...` | `supervise` | two verify Shuttles recorded nothing: check the criteria and record them by hand |
| `merge: request <id> <url>` | `supervise` | the ticket is ready; supervise merges it through the provider in the same pass |
| `merge: <id> merged on the provider (...)` | `supervise` | the pull request is MERGED on the provider; only now is the ticket merged |
| `merge: not merged <id>: <reason>` / `merge: MUST DO <id>: ...` | `supervise` | it could not merge here; merge it (GitHub MCP `merge_pull_request` with `merge_method` from `mergeMethod`, or `gh pr merge --merge`); the next pass confirms it |
| `merge: wait <id> (...)` | `supervise` | asked to merge; not merged on the provider yet |
| `heal: <id> was recorded merged, but <url> is open on the provider. ...` | `scan.py start`, `scan.py resume` | a merge recorded without one is undone; the ticket is ready to merge again |
| `shuttle: returned <id> step=<step> (<state>)` | `supervise`, `beam.py watchdog` | that Shuttle came back: its folder has `done`, a check result, or (a review Shuttle) the review, written after it started. Its slot is free; it is not dead |
| `listener: generation <n> belonged to the previous session and ended with it.` | `scan.py start`, `scan.py resume` | the old lease is closed; the next supervise prints `listener: poll` |
| `config: published to <path> (machine keys and secrets left out)` | `scan.py start`, `scan.py resume` | the config copy is in the repo |
| `listen: <kind> channel=<name> since=<cursor>` | `inbound.py poll` | where the listener reads from |
| `listen: rotate` | `inbound.py poll`, `inbound.py wait` | this generation hit `listenerMaxMinutes` or `listenerMaxReads`. Return it. The return stamp is written |
| `listen: wait` + `listen: cycle <n> of <max> ended at <time>Z. Run poll again …` | `inbound.py wait` | the sleep is over. Poll again now with the same lease. Do not return |
| `run: pass <n> at <time>Z` | `supervise` | the pass number. A number that skips means an output was lost |
| `run: next is pass <n>` | `parent-exit --wait` | the number the next `supervise` prints |
| `state: published (merged <id>)` / `state: published (started <id> on <branch>)` | `supervise`, `checkout.py launch` (with `pushStateNew: true`) | the run state is on the base branch. Nothing to push. `state: not published (…)` says why; push it with `session_note.py` or the next checkpoint |
| `merge: branch deleted <id> <branch>` / `merge: branch not deleted <id> <branch>: <why>` | `supervise` | `deleteBranchOnMerge` is on and the merged ticket's branch was deleted, or why not |
| `merge: <id> merged on the provider (a merge commit with the run state, <sha>, confirmed MERGED)` | `supervise` | Warp pushed one merge commit with the run state and GitHub shows the pull request merged (`stateInMerge`) |
| `merge: <id> pushed <sha> to the base branch with the run state. …` | `supervise` | the merge commit is on the base; the next pass records it merged when GitHub shows it. Nothing to do |
| `merge: <id> through the provider (<why>)` | `supervise` | the state-carrying merge was not possible (a refused push, a moved branch, a conflict, `squash`); `gh pr merge` runs instead |
| `state: in the merge commit (<id>)` | `supervise` | that merge carried the run state; no separate `Warp state` commit |
| `action: sync` + `state: …` + `herald: Run state pushed to <base> at <sha> (…)` | `inbound.py apply-pending` | `warp:sync` from the channel: the run state was pushed. Post the pass update the lines after it name, then `notify.py posted` |
| `run: stopped at <time>. Start nothing …` / `run: paused …` | `supervise` | the run is stopped or paused: no Shuttle, no listener, no resume. Run `parent-exit` and end the turn |
| `herald: post .warp/notify-pass.json to <channel> …` + `herald: then run: … notify.py posted --root .` | `supervise` (last lines) | this pass's `herald:` lines as one payload. Post it, then mark it posted. Unmarked lines are carried into the next pass's payload |
| `herald: <id> claimed: <summary> (size, auto-merge or approval; locks …)` | `supervise` | the first start of a ticket |
| `herald: <id> pull request opened: <url>` | `supervise` | once per pull request, however it was opened |
| `herald: <id> is ready and waits for approval … Reply warp:proceed <id> …` | `supervise` | with `approve: wait <id>` |
| `run: reopened for Bugbot follow-ups` | `supervise` | a finished run has follow-up tickets again; its end-of-run teardown runs once more when they settle |
| `bugbot: late <id> <url> <n> finding(s) after the merge. Follow-up ticket <id>-BB<n> queued.` | `supervise` | Cursor Bugbot found issues on a merged pull request. The follow-up runs like any ticket and asks for no Jira move |
| `bugbot: wait <id> (the Shuttle's review passed; waiting for Cursor Bugbot on <sha>)` | `supervise` | with `bugbotTrigger: shuttle`, the merge still waits for Cursor Bugbot's own result where Bugbot runs |
| `bugbot: running <id> <url> on <sha> (started <hh:mm>Z)` + `herald: <id> Cursor Bugbot is reviewing PR #<n> (<sha>).` | `supervise` | Cursor Bugbot's check started on that head. Post the Herald line. Nothing else to do |
| `bugbot: running <id> on <sha>, <m> so far` | `supervise` | Bugbot is still checking. The run is waiting on it, not stalled. No post |
| `bugbot: done <id> pass on <sha> (took <m>)` / `bugbot: done <id> <n> finding(s) on <sha> (took <m>): …` + one `herald:` line | `supervise` | Bugbot's check ended. A finding goes to a fix in the same pass. A result older than 30 minutes when first seen (a resume) is shown and not posted |
| `bugbot: not started <id> on <sha> (asked <m> ago)` | `supervise` | Bugbot was asked about that head and its check has not shown up. After `bugbotStartMinutes` Warp asks it with a `bugbot run` comment |
| `bugbot: waiting on Cursor Bugbot: <id> running <m>, <id> not started <m>` | `parent-exit --wait` | what the run is waiting on. While it prints, the wait returns after `bugbotPollSeconds` (120) instead of `pollSeconds` |
| `bugbot: review for <id> kept for <sha>; the head Warp has is <sha>. …` | `supervise` (ticket folder) | a Shuttle reviewed the commit it just pushed; the review applies when the pull request shows that commit |
| `fix: <id> came back with no new commit; Cursor Bugbot's finding on <sha> still stands: …` | `supervise` | the fix pushed nothing. It is counted and fixed again with the finding |
| `shuttle: not launched <id>. Launch it now …` | `supervise` | the `start` or `resume` line below it was printed before and never launched |
| `merge: check green <id> (<command>)` / `merge: check skipped <id>: <why>` | `supervise` | the merge check ran on the pull request's head and passed, or there was nothing to run |
| `merge: check red <id> (<command>). Not merged; back to fix.` | `supervise` | the tests fail on that head. The fix Shuttle gets the output |
| `jira: <id> already Done (recorded <time>). Nothing to move.` | `supervise` | a Done move is recorded; it is not asked for again |
| `jira: <id> fixes Bugbot findings on <parent> after its merge. No Jira move.` | `supervise` | a follow-up merged |
| `listener: lost holder=<id> generation=<n> (no poll or wait for <m>m). Its slot is free.` | `supervise` | the listener stopped without a return stamp. The `listener: poll` after it starts the next one |
| `listener: generation <n> closed with the parent's exit.` | `parent-exit` | the parent left and closed the lease |
| `listener: returned cut-off. The slot is free; …` | `inbound.py returned --reason cut-off` | a holder that had to stop handed its slot back |
| `listener: returned lost (no poll or wait for <m>m; the slot is free)` | `inbound.py returned` | a silent holder's slot was handed back by anyone |
| `listen: cycle <n> of <max> at <time>Z (generation <g>)` | `inbound.py poll` | which cycle this is. The generation rotates after `listenerMaxReads` |
| `times: filled in <ids> from the pull requests` | `scan.py start`, `scan.py resume` | opened, reviewed, approved, merged, and Spent were read in for tickets from earlier runs |
| `times: merged time filled in for <ids> from the merge commits` | `supervise`, `heal.py --times`, `warp:sync` | a merged ticket had no `pr.mergedAt` (every merge before 1.6.22); its merge commit's time was written |
| `refuse: CURSOR_API_KEY is not set. …` | `scan.py start`, `scan.py resume` | this run starts cloud agents and Warp could not cancel or archive them. Set the key, or `requireCursorApiKey: false` |
| `listener: not returned. ...` | `inbound.py returned --reason polled` or `rotated` | a live holder tried to leave after a cycle. Keep cycling. A holder that has to stop uses `--reason cut-off` |
| `alarm-repair: launch <id>. Run ... checkout.py launch ...` | `alarm_repair.py next`, `supervise` | launch the repair Shuttle with that command. `--returned <id>` only after it returns |
| `alarm-repair: not launched <id>. ...` | `alarm_repair.py next --returned` | no repair Shuttle was launched. The attempt is not counted. Launch it |
| `listen: stopped` | `inbound.py poll`, `inbound.py wait`, after a `reap: exit` line | return it. Do not read |
| `listener: lease generation=<n> holder=<id>` | `inbound.py take` | a `/warp-listen` chat holds the lease. Any other copy exits `superseded` |
| `listener: returned <reason>` | `inbound.py returned` | the return stamp is written |
| `listener: not started. The next supervise issues it.` | `scan.py start`, `scan.py resume` | start and resume do not start a listener |
| `inbound: duplicate <id>` / `enqueue: duplicate <id>` | `inbound.py accept` / `inbound.py enqueue` | an earlier poll already queued that message. No second ack, no second queue entry |
| `listener: polled <n>` | `inbound.py polled` | the poll is recorded |
| `spawn: closed <id>` and `refuse: ...` | `checkout.py launch` | no prompt was printed |
| `name: [warp:<instance>] <ticket> <step>` | `checkout.py launch` | the name to give the subagent. It is the tag |
| `model: <slug> (<why>)` | `checkout.py launch` | the model to start the subagent with: the ticket's size model, `model`, or `fixModel` from fix round `fixModelAfter` on |
| `cleanup: tag [warp:<instance>]` | cleanup | which tag this cleanup matches |
| `reap: continue` / `reap: exit <reason>` | `agents.py reap`, `ticket_state.py append`, `beam.py heartbeat`, `inbound.py poll` | carry on, or stop now |
| `stop: <id>` | pause, stop | that registry row was ended |
| `halt: <state> agents=<n>` | pause, stop, end of run | the local teardown ran |
| `cleanup: cancelled <id>` / `archived <id>` / `link <url>` | halt, cleanup, merge, park | what happened to a cloud agent |
| `cleanup: would cancel <id>` / `would archive <id>` | cleanup without `--apply` | a dry run. Nothing changed |
| `cleanup: unreachable <id>` / `cancel failed` / `archive failed` | halt, merge, park, retire | the API call failed. The agent is left alone and tried again later |
| `cleanup: skip <id> reason=self` / `parent` / `running` / `running-untagged` / `running-scan` / `status` | cleanup | why an agent was left |
| `list: kept <id> reason=...` | `/warp-list` | the same reasons, in a listing |
| `out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>` | `/warp-list` | what is still out |
| `run: was paused (<reason>). Continuing.` / `was stopped. Starting.` / `was already running.` | start, resume | where the run came from |
| `unfinished: <n> open. ...` | start, resume | the work that starts first |
| `config: <key>: <value> is retired and ignored.` | start, resume, version | a retired key is still in `.warp/config.yaml` |
