---
name: warp
description: "Run the Warp master loop on any repo that has a plan. Use when the user says start, stop, pause, resume, scan, or supervise the build."
---

# Warp — master loop

Scan builds the plan. Start dispatches. Stop and pause do not.

You are the parent. One agent is long-lived, and it is you. You are the only agent that starts another agent, and the only agent that waits. A Shuttle does one step of one ticket and returns one line. The listener is one background subagent on this checkout. It does not start an agent. `docs/LIFECYCLE.md` is the whole rule.

Plugin hooks do not run on cloud runners. Every check here is a script you run. Before the first pass, run `python3 <plugin>/scripts/resume_hint.py --root .` and follow it. Do not dispatch from the hint. If it says there is no beam, tell the user to run `/warp-ingest`. At the end of every pass, run `python3 <plugin>/scripts/orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>`. `<session>` is the id `scan.py start` or `scan.py resume` printed as `parent: session <id>`. That command is the one wait in a run. It blocks until a ticket folder changes, the inbound queue changes, the listener writes its return stamp, the run halts, or `pollSeconds` pass. It writes `listener.parentSeenAt` on each step. Do not sleep, set a timer, or subscribe to a pull request anywhere else. `parent: stay` means run the next pass now and do not end the turn. `parent: exit` is when you run `python3 <plugin>/scripts/session_note.py --type session-stop --root .`, start nothing, and end the turn. `parent: exit superseded` means a later `/warp-start` or `/warp-resume` owns the run: end the turn at once. When a ticket's Agent finishes, run `python3 <plugin>/scripts/session_note.py --type subagent-stop --root .`, then reconcile that ticket from the beam. That journal line does not launch a ticket. Call an MCP tool only when it is in `scripts/mcp_tools.py` (plus `notifyAllow`). For any other tool, `python3 <plugin>/scripts/mcp_allow.py --server SERVER --tool TOOL --root .` must print `allow`. `ask` means do not call it.

## Control

| Command | runState | Dispatch | In-flight |
|---|---|---|---|
| `/warp-scan [folder]` | stopped | no | none yet |
| `/warp-start` or `/warp-resume` | running | yes | the same command, from paused, stopped, or running. Unfinished work first, then new tickets, up to maxAgents |
| `/warp-pause` | paused | no | every agent is ended. Branches, pull requests, and locks stay |
| `/warp-stop` | stopped | no | the same teardown as pause, then the completion report |
| `/warp-list` | unchanged | no | what is still out for this run, running or idle. `--scan` lists word matches in any repo and changes nothing |
| `/warp-cleanup` | unchanged | no | cancels and archives what `/warp-list` shows. `--scan` crosses repos: read the list before `--apply` |

```bash
python3 <plugin>/scripts/scan.py start --beam .warp/beam.json
python3 <plugin>/scripts/scan.py pause --beam .warp/beam.json --reason "hold"
python3 <plugin>/scripts/scan.py resume --beam .warp/beam.json
python3 <plugin>/scripts/scan.py stop --beam .warp/beam.json --reason "end of day"
```

`scan.py start` and `scan.py resume` print `unfinished: <n> open. ...` and launch that work first: fixes, then tickets whose Shuttle was halted or died, then new tickets. Two caps apply. `maxAgents` is how many agents run at once, of any kind. `maxInProgress` is how many tickets are open at once and holds only new tickets.

`scan.py ?` prints these subcommands and their flags. Quote `?` if the shell expands it.

`ready` returns nothing unless `runState` is `running`. A tick on a stopped or paused beam only reconciles and writes status.

## Pause and stop

Pause and stop are the same teardown. The one difference is that stop also writes the completion report and records the end of the run. Start and resume are the same command: `scan.py start` and `scan.py resume` do the same thing from any state and print `run: was paused (<reason>). Continuing.` or `run: was stopped. Starting.` Every row in `.warp/agents.json` is ended (`stop: <id>`) and marked halted, every Shuttle slot is freed, and the listener's poll is closed (`halt: paused agents=<n>` or `halt: stopped agents=<n>`). The beam and the registry are pushed to main. With `CURSOR_API_KEY` in the environment, every cloud agent in the registry, and every agent in this repo whose name carries this run's `[warp:<instance>]` tag, has its run cancelled and is archived. The agent running the command and this run's parent are skipped. Without the key the command prints `cleanup: link https://cursor.com/agents/<id>` for each one.

After either command, `parent-exit` prints `parent: exit`. Start nothing and end the turn. Do not wait for the agents. Each one reads `reap: exit` at its next check and returns `result: <id> stopped <reason> agent=<agent>`. Nothing from before the halt is resumed: `/warp-resume` and `/warp-start` launch a new Shuttle on the same branch for each ticket that had one out (`fix: <id> resume after halt`), with no recovery counted. `/warp-cleanup` is the clear you run yourself for anything left.

## Listener

The listener is a Subagent of the parent, and it is one background subagent on this checkout. `subagentVm` does not apply to it. One `warp-listen` Subagent for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. It shares this session and this checkout. It is not a separate Agent. Do not start a separate Agent. It holds a generation lease, reads Slack and Teams, posts acks, and enqueues `warp:` commands. `inbound.py wait` sleeps in Python between reads. You run `inbound.py apply-pending` and merge a proceed. It does not implement tickets, does not merge, and does not write product code.

You do not claim it. `orchestrator.py supervise` decides. `listener: poll` means no holder is live and the previous generation's return stamp is in, or none has started. The next lines are its prompt: `LISTEN`, the lease, the holder, and `background: true`. Start exactly one `warp-listen` Subagent in the background with those lines, named `[warp:<instance>] listener`, and do not wait for it. When `listenerModel` is unset the head line says `model: inherit`. When it is set the head line says `model: <slug>`. `listener: hold reason=live holder=<id> generation=<n>` means that holder already has the lease: do not start a second. `listener: hold reason=await-return generation=<n>` means the lease was bumped and the return stamp is not in yet: do not start a second. A stale heartbeat does not start one. The return stamp does. `listener: idle` means paused, stopped, or every ticket is merged or parked. `listener: none` means no `slackChannel` or `teamsChannel` is set: no listener is started at all. When the listener returns, pass that line to the next pass as `supervise --returned "<line>"` only on the pass that saw the return. `listen: stopped` idles that one pass. A later pass with the same line starts the next generation when the run should still listen. The listener must not keep reading while paused or stopped. Pause and stop reap it within about 2 seconds, at the next wait step. A read already in flight finishes, then the next poll reaps it. Plugin hooks do not run on cloud runners. A dead turn does not notify Warp. Cursor does not restart it. `/warp-listen` takes the lease in a chat you open yourself, and any other copy exits superseded.

## Shuttles

`subagentVm` defaults to false. `runner: local` forces it false. `maxAgents` is the one cap on agents that run at once, on this machine or on their own VMs. `memoryCheck` defaults to false, so only that cap applies. On a cloud runner, `true` starts one subagent per ticket on its own dedicated VM, in parallel up to maxAgents. The prompt asks for a dedicated VM, not a git worktree on this machine. A same hostname falls back to a worktree and one warning per run. Start one subagent per ticket in its own worktree, in parallel up to maxAgents, on that path.

`checkout.py launch --id <id>` is the only place a Shuttle prompt comes from. It records the Shuttle in `.warp/agents.json` first. Lines above `prompt: pass every line below this one to the subagent, and nothing above it` are for you. One of them is `name: [warp:<instance>] <ticket> <step>`: give the Subagent exactly that name. `<instance>` is the six characters `scan.py start` printed as `instance: warp:<instance>`. Cursor has no tag field, so the name is how pause, stop, and `/warp-cleanup` find this run's agents among all the others, including another Warp run's. Pass the Subagent the lines below the mark and nothing else. `spawn: closed <id>` and exit 2 mean no prompt was printed, because the run is paused, stopped, or finished, or the ticket is merged or parked: start nothing. Run Shuttles in the background so the pass goes on. `start <id>` is a new Shuttle. `resume <id>` with `resume: <id> agent=<agent>` continues the Shuttle that returned: send it the follow-up and do not start a second one. `shuttle: hold`, `spawn: cap`, and `spawn: closed` mean start nothing.

Every prompt carries `agent: <id>`, the worker contract, and the reap command. A Shuttle runs `agents.py reap` first, and reads the `reap:` line that `ticket_state.py append` prints after every state it writes. `reap: exit <reason>` makes it return `result: <id> stopped <reason> agent=<agent>`. Treat that line as a clean stop, not a failure. Do not relaunch that ticket because of it.

A Shuttle on its own VM writes its cloud agent id into its ticket folder. The pass binds it to the Shuttle's registry row, so pause and stop can cancel that agent by id. A fix round that lands on a new VM retires the VM the ticket left (`cleanup: archived <id>`, or `cleanup: link` without the key).

## Heartbeat and watchdog

`beam.py watchdog` runs on this tick, before `ready()`. `scan.py start` and `scan.py resume` run it too. It does not look at a process table. First it fetches each in-flight ticket branch and reads only `.warp/tickets/<id>/`. It patches the live beam from `state.json` and `log.jsonl`. It does not copy the branch beam. A Shuttle writes that directory (`ticket_state.py append --push`) at claim, at each status change, and at least every 5 minutes while the turn is alive, inside `staleMinutes` (default 15). That is not a timer: it writes between steps of work it is already doing. Paused and stopped runs do not fetch and do not relaunch.

A Shuttle is dead when `lastSeenAt` is older than `staleMinutes`, or it never heartbeated and the claim is older than `staleMinutes`. A fresh Shuttle heartbeat (`alive` or `fresh`) is left alone. `watchdog: skipped` means the run is paused or stopped: do not recover and do not start a replacement. The watchdog prints no listener line and never starts one.

- `shuttle: replace <id>`: status is `recovering`. Branch, pull request, `jira.startedAt`, and locks stayed. Dispatch exactly one Shuttle for that same ticket. Do not queue a duplicate and do not release the lock. Herald posts one line: `<id> worker died. A new Shuttle started.`
- `shuttle: alarm <id> worker-died`: `maxRecoveries` (default 5) is spent. Do not start another.

Lock-escape repair is this tick's job. Run `alarm_repair.py next` and widen that ticket's locks to the paths that escaped. Start one subagent for a repair the same way you start any ticket, in that ticket's worktree. The listener does not start it.

A second tick must not launch a second replacement for the same worker. The first write reserved it.

## Orchestrator

The orchestrator is the only merger. This loop dispatches, merges, and tracks. It writes no ticket product code. The cap is `maxAgents`. A project may set 18. One ticket, one branch, one checkout. Do not implement a ticket in this session. `checkout.py implement` refuses.

Claim the ticket, then run `checkout.py launch`. It fetches origin and adds a git worktree at `<worktreeRoot>/<id>` on `warp/<id>-<jira>` from `origin/main`. Start one subagent per ticket in that worktree, in parallel up to `maxAgents`, with the prompt it prints. The prompt names the ticket, Jira key, locks, acceptance, branch, and absolute worktree path. The subagent works only inside that path, commits, pushes, and opens the pull request. It never merges, never touches the parent checkout, and never calls Jira or Slack. It writes `.warp/tickets/<id>/state.json` and `log.jsonl` at the parent absolute path and returns one line. After it returns, run `checkout.py verify`. A miss raises `lock-escape`. A line `result: <id> failed` is `worker-died` (`checkout.py result`). You do the Jira and Slack work. You merge serially. You push the beam on each merge and tick. Optional `launch: agent` prints an IMPLEMENT prompt for one new Agent. This plugin does not call a Cloud Agents API. Clone main so `.cursor` rules load. A beam file does not have to exist on the branch before that Agent starts.

A non-zero launch is not idle. Do not end the turn because launch failed. Record an optional Agent id with `checkout.py bind --agent`. `checkout.py remove` deletes a worktree. Merge and park do that too, then `git worktree prune`. `/warp-resume` reuses a surviving worktree and branch. Never two tickets in one worktree. Never two workers on one ticket.

On start, resume, and after every merge, failure, and freed slot: rebuild when the run is starting (`orchestrator.py rebuild --facts`), then `beam.py ready`, then start that list only. Starred and priority first, then lowest rank. Do not hold a slot for a ticket that is not ready. Do not wait for a dependency level. Record the pull request when the Shuttle exits (`orchestrator.py opened`). A launch slot is a live Shuttle. A review waiting on the rollup does not hold `maxAgents`. Bugbot runs on the pull request after push, not in the VM. Locks stay until merge or park. If `mergeQueue` is true, or GitHub reports a merge queue, `provider.py merge-pr` enqueues and does not mark the ticket merged. A rejected direct merge is not merged. Then `state_commit.py commit` on the base branch.

`checkCommand` empty means Bugbot and CI as configured. `make ci` is a valid command. A red result is stored on `pr.check` and sent back to that ticket with the log (`send-back <id> fix` and a `start` line). Start that ticket as one subagent in its own worktree (`checkout.py launch`). A green result is not a failure. The gate stays while that check is red. `appendOnlyPaths` is empty by default. Keep both sides of a conflict there. Send any other conflict back. Park once attempts reach `maxFixAttempts` (default 5) (`orchestrator.py fail`). Sizes not in `autoMergeSizes` wait for `/warp-proceed` or `warp:proceed`. If the base branch is red, stop merging and dispatch a fix ahead of every rank. Done means every ticket is merged or explicitly parked, and the base branch is green.

## One running tick

1. `beam.py check`
2. `beam.py watchdog`. Launch the replacements above. Herald posts each `herald:` line once.
3. Reconcile in-flight PRs and Jira.
4. `beam.py ready` recomputes every pending gate. A gate turns green when every member is merged or done. Post `herald: <gate> pending cleared. Members merged. Tick ran.` when it prints that line.
5. Claim the ready list only. No person cap. A `recovering` ticket is not in this list. `start` lines are the same ids. Claim each id once. `holding new launches (N/M), N fix workers running` means the ready queue waits. It does not stop a fix, a rebase, a CI rerun, a Bugbot re-request, or a merge. On start or resume, a Shuttle with a fresh heartbeat keeps its slot. A stale heartbeat does not. Launch fix `start` lines first (conflict, CI that never started, red CI, Bugbot findings), then `shuttle: replace` lines, up to the live caps. `maxInProgress` does not hold those. `checkout.py launch` counts live Shuttles and refuses with `live N/cap` and the ids that hold the slots.
6. Claim each id, run `checkout.py launch`, and start one subagent in that worktree with the prompt it prints. Start those subagents in parallel up to `maxAgents`. Then `beam.py heartbeat` for that id. Herald posts each claim. When the subagent returns, run `checkout.py verify`. A failure line raises `worker-died`.
7. `scan.py status` and `beam.py board`.
8. Herald posts the tick digest: done, working, left, next ready.
9. Run `inbound.py apply-pending`, then `orchestrator.py supervise`. `listener: poll` means start exactly one background listener with the prompt that follows and do not wait for it. `listener: hold reason=live` and `listener: hold reason=await-return` mean do not start a second. `listener: idle` and `listener: none` mean do not start one. Post a `herald:` line from supervise once. Push the live beam, then run `orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>`. It waits for the next thing to do and prints `pass: ticket <id>`, `pass: command`, `pass: return`, `pass: halt`, or `pass: timeout`. `pass: command` means the inbound queue changed: apply it and go back to wait. `pass: return` means the listener wrote its return stamp: the next supervise starts the next generation when no holder is live. If `supervise` prints `pass: aborted`, a pause or a stop landed during the pass: start nothing from that output and go straight to `parent-exit`. `parent: stay` means run the next pass now and do not end the turn. The same `supervise` pass is the pipeline. `start <id> ... step=implement`, `step=fix`, or `step=restart` means launch one Shuttle subagent on that ticket's branch or worktree. `bugbot: request <id> <url>` means ask Bugbot on that pull request and wait for it. `alarm-repair: start` means launch the repair Shuttle. `merge:` with `herald:`, `jira: MUST DO`, and `slack:` means push the beam, move the Jira issue, and post to Slack. A `shuttle: hold` line means that Shuttle is already out: do not start a second. `fixer: <id> bugbot` asks Bugbot again. `ci: rerun <id>` reruns CI. `fixer: <id> rebase` launches the fix Shuttle to rebase onto main. `fixer: <id> shuttle` restarts a silent step. `fixer: <id> lock-escape` is the widen-and-rerun repair. `merge: queue <id>` puts an approved pull request back in the merge queue. `dispatch-base-fix` starts a fix for a red base branch. `slack: digest` is the status digest. `slack: alarm` posts once for a new stall or alarm. `sweep:` is the run-wide broken-state pass (`repairSweepMinutes`, default 15). `lock: remove` drops a lock no live ticket owns. `sweep: finding listener idle` and `sweep: skip listener running` notice the listener and do not start one. The return stamp is the only start. `beam: sync` marks a beam that is behind main. `slack: sweep` posts only when that sweep started something new or escalated. `open-work:` lists every ticket that is not merged. `park: <id> fixer cap` means that ticket is parked: post the one alarm and do not start another Shuttle. Opening a pull request is not done. Adopt an existing open pull request into `reviewing` or `fixing` and do not relaunch it from scratch. Start the next pass in this same turn when `parent-exit --wait` returns `parent: stay`: apply pending commands, supervise, read `.warp/tickets/<id>/`, patch the beam, dispatch with `checkout.py launch` and one subagent per worktree, push, and wait again. A launch refusal is not idle. `parent: exit` means every ticket is merged or parked, or the run is paused or stopped. Then `session_note.py --type session-stop` commits the live beam onto main. Start nothing after it. When every ticket is merged or parked, `parent-exit` runs the end-of-run teardown once (`halt: done`) and archives what the run left.

## Status file

`.warp/STATUS.md` and `.warp/status.json` are the files to open. Done, working, left, then open work. Regenerated every tick and on `/warp-status`. `/warp-start` and `/warp-resume` print the same open-work rows. Alarms and stalls lead, then a count of each state. The rows are stored on the beam as `openWork`. The last repair sweep is the `sweep:` line (`sweep: at=...` or `sweep: none`).

## Notify

`notify: verbose` is the default. Herald posts scan, start, pause, resume, stop, claim, PR opened, Bugbot result, approval wait, merge, alarm, and gate change. See `agents/herald.md`.
