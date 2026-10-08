# Agent lifecycle

This is how Warp starts agents, how they end, and how a run is torn down. It is the rule the commands, skills, and prompts follow. Read it before changing any of them.

## The rule

One agent is long-lived: the parent. The parent is the only agent that starts another agent, and the only agent that waits. Every other agent does one step and returns one line. That is tick-and-return.

| Agent | Lives for | Started by | Returns |
|---|---|---|---|
| Parent (the orchestrator) | the run | you, with `/warp-start` or `/warp-resume` | ends its turn on `parent: exit` |
| Shuttle | one step of one ticket: implement, fix, restart, or repair | the parent, from `checkout.py launch` | `result: <id> ok ...`, `failed ...`, or `stopped <reason> agent=<agent>` |
| Listener | one poll of Slack and Teams | the parent, on `listener: poll` | `listen: <count>` |

Reed and Herald are roles. The parent usually plays them in its own turn. When it starts one as a subagent, the same rule holds: one step, one return, and it starts nothing.

A worker never starts an agent, never subscribes to anything, never sets a timer, never loops, and never waits on a pull request, CI, Bugbot, Slack, or an approval. If any of those is needed, the worker returns and the parent does it on a later pass.

## Run control

Four commands, two behaviors.

`/warp-start` and `/warp-resume` are the same command. Either one sets the run running, from a stopped run, a paused run, or a run that is already running. It prints which: `run: was stopped. Starting.`, `run: was paused (<reason>). Continuing.`, or `run: was already running. This session takes it over.` Both run the same prompt gate, and `--force` means the same on both. There is nothing to refuse and nothing to choose between them.

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

`maxLocalSubagents` and `maxFixWorkers` are retired. A config that still has them prints `config: <key>: <value> is retired and ignored.` at start and in `/warp-version`.

## Why

Agents pile up when four things are true together. A worker can start a worker. A worker waits for something instead of returning. Nothing ends an agent that is no longer wanted. And nobody can list what is out. Each mechanism below removes one of those.

Plugin hooks do not run on cloud runners. Nothing here depends on a hook. Every check is a script that a command, a skill, or a prompt runs. The hooks in `hooks/` are local IDE duplicates of the same scripts.

## Five mechanisms and a tag

### 1. The gate

A prompt for a Shuttle comes from one place, `checkout.py launch --id <id>`. It records the row in `.warp/agents.json` before it prints. It prints nothing, and exits 2 with `spawn: closed <id>`, while the run is paused, stopped, or finished, and for a ticket that is merged or parked. The pipeline (`orchestrator.py supervise`) decides which ids to launch: `start <id>` is a new Shuttle, `resume <id>` continues the one that returned, and `shuttle: hold`, `spawn: cap`, and `spawn: closed` mean start nothing.

The output has two parts. Lines above `prompt: pass every line below this one to the subagent, and nothing above it` are for the parent. Lines below it are the worker's prompt. The parent's instruction to start workers in parallel never reaches a worker.

The listener's prompt comes from `orchestrator.py supervise`, after `listener: poll`, and only when a poll is due.

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
| Every Slack and Teams message | the header: `Warp | <repo> / <project> | warp:a1b2c3` |
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

It prints `reap: continue`, or `reap: exit <reason>` and one line saying how to stop, and exits 3. `ticket_state.py append` and `beam.py heartbeat` print the same line, so a Shuttle that writes its state reads the answer without a second command. `inbound.py poll` prints it for the listener.

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

A Shuttle in a worktree reads the parent's beam on disk. On `reap: exit` that check ends the agent's row and stamps `reaped`, so `agents.py list` shows who is gone.

A Shuttle on its own VM passes `--remote`, which reads the beam and the registry from origin's base branch and writes nothing. Pause and stop push both before they return, and start and resume push before they print a `start` line, so the remote check sees the run as it is. The VM does not end the row and does not stamp `reaped`. It returns `result: <id> stopped <reason> agent=<agent>`. `<agent>` is that Shuttle's registry id or its cloud id, the same id `reap` matches. The parent records the line with `checkout.py result`. That ends only the matching row, and only while it is still live, and stamps `reaped` on it. When the line does not name an agent, the ticket folder's `cloudAgent` is that id. A late result from a halted or replaced VM does not match the Shuttle that replaced it, so the new row stays live. The parent journals that result as `reap-stale`. A row a halt already ended keeps its end reason. A second record leaves the stamp where it is.

### 4. The halt

`/warp-pause` and `/warp-stop` run the same teardown. The only difference is what comes after: a paused run continues with `/warp-resume`, a stopped run writes the completion report and waits for `/warp-start`.

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

## One parent pass

The parent runs passes in one turn until `parent: exit`.

1. `inbound.py apply-pending`. Queued `warp:` commands are applied. A pause or stop here is the halt above.
2. `orchestrator.py supervise --beam .warp/beam.json`. The first line is the listener. The rest is the pipeline.
   - `listener: poll`, then the prompt: start one `warp-listen` subagent in the foreground with those lines and wait for it. It returns `listen: <count>`. Run `inbound.py apply-pending` again.
   - `listener: hold`: a poll is out, or the next one is not due. `listener: idle`: the run is paused, stopped, or finished. `listener: none`: no `slackChannel` or `teamsChannel` is set. Start nothing.
   - `start <id> ...`: run `checkout.py launch --id <id>` and start one subagent with the lines below the `prompt:` mark. Start the ready ones together, up to `maxAgents`, in the background so the pass goes on. A foreground batch also works: the pass continues when they return.
   - `resume <id> ...`: send that same Shuttle the follow-up. Do not start a second one.
   - Everything else (`bugbot: request`, `merge:`, `herald:`, `jira: MUST DO`, `slack:`) is the parent's own work.
3. When a Shuttle returns, record its line (`checkout.py result`, `checkout.py verify`, `session_note.py --type subagent-stop`). `result: <id> stopped <reason> agent=<agent>` is how a VM Shuttle reports `reap: exit`. `checkout.py result` ends that agent's row when it is still live and stamps `reaped`. A result whose agent is not the live Shuttle is `reap-stale` and leaves the new row alone.
4. `orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>`. This is the one wait in a run. It blocks until a ticket folder changes, the listener is due, the run halts, or `pollSeconds` pass, and prints `pass: ticket <id>`, `pass: poll`, `pass: halt`, or `pass: timeout`.
   - `parent: stay`: run the next pass now.
   - `parent: exit`: run `session_note.py --type session-stop` and end the turn. Start nothing.
   - `parent: exit superseded`: a later `/warp-start` or `/warp-resume` owns the run. End the turn at once.

`<session>` is the id `scan.py start` and `scan.py resume` print as `parent: session <id>`. One parent per run: a second start or resume takes the run over, and the earlier parent exits on its next `parent-exit`.

If the parent's turn dies, nothing restarts it. Workers finish their step, write their ticket folder, and return. Run `/warp-resume`. State comes from the base branch plus the ticket folders.

## The listener is one poll

The listener does not loop and does not sleep. `supervise` issues one poll when the last one finished more than `pollSeconds` ago.

```bash
python3 <plugin>/scripts/inbound.py poll --beam .warp/beam.json
python3 <plugin>/scripts/inbound.py accept --beam .warp/beam.json --text "warp:proceed XV-01" --by <who> --source slack
python3 <plugin>/scripts/inbound.py enqueue --beam .warp/beam.json --text "warp:proceed XV-01" --by <who> --source slack
python3 <plugin>/scripts/inbound.py polled --beam .warp/beam.json --count 1 --cursor <newest message id>
```

`poll` prints the reap line, then `listen: slack channel=<name> since=<cursor>` for each channel. The listener reads messages after that mark, accepts and queues each `warp:` command, posts the ack, runs `polled`, and returns. A poll that never reports is issued again after `listenerStaleMinutes`. The registry keeps one `listener` row for the run, with a `polls` count. Polls are not spawns and never count against a spawn cap.

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
| `endReason` | `returned`, `dead`, `replaced`, `merged`, `parked`, `paused`, `stopped`, `done`, `archived`, `polled`, `lost` |
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
| `pass: ticket <id>` / `poll` / `halt` / `timeout` | `parent-exit --wait` | why the wait ended |
| `pass: aborted. The run was <state> during this pass.` | `supervise` | a pause or stop landed mid-pass. Start nothing from it |
| `parent: stay` / `parent: exit` / `parent: exit superseded` | `parent-exit` | run another pass, or end the turn |
| `listener: poll` / `hold` / `idle` / `none` | `supervise` | start one poll, or start nothing |
| `listen: <kind> channel=<name> since=<cursor>` | `inbound.py poll` | where the listener reads from |
| `listener: polled <n>` | `inbound.py polled` | the poll is recorded |
| `spawn: closed <id>` and `refuse: ...` | `checkout.py launch` | no prompt was printed |
| `name: [warp:<instance>] <ticket> <step>` | `checkout.py launch` | the name to give the subagent. It is the tag |
| `cleanup: tag [warp:<instance>]` | cleanup | which tag this cleanup matches |
| `reap: continue` / `reap: exit <reason>` | `agents.py reap`, `ticket_state.py append`, `beam.py heartbeat`, `inbound.py poll` | carry on, or stop now |
| `stop: <id>` | pause, stop | that registry row was ended |
| `halt: <state> agents=<n>` | pause, stop, end of run | the local teardown ran |
| `cleanup: cancelled <id>` / `archived <id>` / `link <url>` | halt, cleanup, merge, park | what happened to a cloud agent |
| `cleanup: unreachable <id>` / `cancel failed` / `archive failed` | halt, merge, park, retire | the API call failed. The agent is left alone and tried again later |
| `cleanup: skip <id> reason=self` / `parent` / `running` / `running-untagged` / `status` | cleanup | why an agent was left |
| `list: kept <id> reason=...` | `/warp-list` | the same reasons, in a listing |
| `out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>` | `/warp-list` | what is still out |
| `run: was paused (<reason>). Continuing.` / `was stopped. Starting.` / `was already running.` | start, resume | where the run came from |
| `unfinished: <n> open. ...` | start, resume | the work that starts first |
| `config: <key>: <value> is retired and ignored.` | start, resume, version | a retired key is still in `.warp/config.yaml` |
