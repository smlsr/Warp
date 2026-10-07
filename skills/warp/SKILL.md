---
name: warp
description: "Run the Warp master loop on any repo that has a plan. Use when the user says start, stop, pause, resume, scan, or supervise the build."
---

# Warp — master loop

Scan builds the plan. Start dispatches. Stop and pause do not.

Plugin hooks do not run on cloud runners. Before this tick, run `python3 <plugin>/scripts/resume_hint.py --root .` and follow it. Do not dispatch from the hint. If it says there is no beam, tell the user to run `/warp-ingest`. At the end of a pass, run `python3 <plugin>/scripts/orchestrator.py parent-exit --beam .warp/beam.json`. `parent: stay` means do not end the turn. `parent: exit` is when you run `python3 <plugin>/scripts/session_note.py --type session-stop --root .`. When a ticket's Agent finishes, run `python3 <plugin>/scripts/session_note.py --type subagent-stop --root .`, then reconcile that ticket from the beam. That journal line does not launch a ticket. Call an MCP tool only when it is in `scripts/mcp_tools.py` (plus `notifyAllow`). For any other tool, `python3 <plugin>/scripts/mcp_allow.py --server SERVER --tool TOOL --root .` must print `allow`. `ask` means do not call it.

## Control

| Command | runState | Dispatch | In-flight |
|---|---|---|---|
| `/warp-scan [folder]` | stopped | no | none yet |
| `/warp-start` | running | yes | claims up to maxAgents |
| `/warp-pause` | paused | no | finish the current step, checkpoint |
| `/warp-resume` | running | yes | reconcile, then claim |
| `/warp-stop` | stopped | no | checkpoint, do not claim again until start |

```bash
python3 <plugin>/scripts/scan.py start --beam .warp/beam.json
python3 <plugin>/scripts/scan.py pause --beam .warp/beam.json --reason "hold"
python3 <plugin>/scripts/scan.py resume --beam .warp/beam.json
python3 <plugin>/scripts/scan.py stop --beam .warp/beam.json --reason "end of day"
```

`scan.py ?` prints these subcommands and their flags. Quote `?` if the shell expands it.

`ready` returns nothing unless `runState` is `running`. A tick on a stopped or paused beam only reconciles and writes status.

## Listener

The listener is a Subagent of the parent. `subagentVm` defaults to false. `runner: local` forces it false. `maxLocalSubagents` defaults to 18, and `memoryCheck` defaults to false, so only that cap applies. On a cloud runner, `true` starts one subagent per ticket on its own dedicated VM, in parallel up to maxAgents. The prompt asks for a dedicated VM, not a git worktree on this machine. A same hostname falls back to a worktree, `maxLocalSubagents`, and one warning per run. Start one subagent per ticket in its own worktree, in parallel up to maxAgents, on that path. One `warp-listen` Subagent for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. It shares this session and this checkout. It is not a separate Agent. Do not start a separate Agent. It loops: read Slack and Teams, post acks, enqueue `warp:` commands, write `.warp/listener.json`, `sleep` for `pollSeconds`, and repeat. It does not return while the loop is running. It returns on pause or stop, or returns `recycle` when its context is large. You run `inbound.py apply-pending` and merge a proceed. It does not implement tickets, does not merge, and does not write product code.

One listener while the loop is running, never several. Claim with one `--turn` id. `listener: started` means start it. `listener: already running` means this pass already started it: do not start a second. A running flag left by the previous turn does not block the next pass. A new `--turn` takes the slot when `orchestrator.py supervise` prints `listener: start`. `listener: hold` means one is alive: do not start another. `listener: idle` means paused or stopped. A Cursor subagent can still exit. When it returns, or `.warp/listener.json` is older than `listenerStaleMinutes`, and the loop is not paused or stopped, start exactly one replacement straight away. Each restart is logged. Herald posts one note, `Listener kept restarting. One listener is running.`, when restarts repeat. Do not end the turn while `orchestrator.py parent-exit` prints `parent: stay`. That is the whole time the listener is supposed to be running, including when no ticket is claimed. `/warp-pause` and `/warp-stop` run `inbound.py release` after the state sync. The listener must not keep reading while paused or stopped. Plugin hooks do not run on cloud runners. A dead turn does not notify Warp. Cursor does not restart it. You do, from `supervise`.

## Heartbeat and watchdog

`beam.py watchdog` runs on this tick, before `ready()`. `scan.py start` and `scan.py resume` run it too. It does not look at a process table. First it fetches each in-flight ticket branch and reads only `.warp/tickets/<id>/`. It patches the live beam from `state.json` and `log.jsonl`. It does not copy the branch beam. A Shuttle writes that directory (`ticket_state.py append --push`) at claim, at each status change, and at least every 5 minutes while the turn is alive. The listener writes the live beam with `inbound.py heartbeat` on every read. Both stay inside `staleMinutes` (default 15). Paused and stopped runs do not fetch and do not relaunch.

A Shuttle is dead when `lastSeenAt` is older than `staleMinutes`, or it never heartbeated and the claim is older than `staleMinutes`. A fresh Shuttle heartbeat (`alive` or `fresh`) is left alone. `watchdog: skipped` means the run is paused or stopped: do not recover and do not start a replacement. `listener: subagent` means start one listener Subagent this turn if claim prints `started`. The watchdog does not reserve a listener id.

- `shuttle: replace <id>`: status is `recovering`. Branch, pull request, `jira.startedAt`, and locks stayed. Dispatch exactly one Shuttle for that same ticket. Do not queue a duplicate and do not release the lock. Herald posts one line: `<id> worker died. A new Shuttle started.`
- `shuttle: alarm <id> worker-died`: `maxRecoveries` (default 3) is spent. Do not start another.

Lock-escape repair is this tick's job. Run `alarm_repair.py next` and widen that ticket's locks to the paths that escaped. Start one subagent for a repair the same way you start any ticket, in that ticket's worktree. The listener does not start it.

A second tick must not launch a second replacement for the same worker. The first write reserved it.

## Orchestrator

The orchestrator is the only merger. This loop dispatches, merges, and tracks. It writes no ticket product code. The cap is `maxAgents`. A project may set 18. One ticket, one branch, one checkout. Do not implement a ticket in this session. `checkout.py implement` refuses.

Claim the ticket, then run `checkout.py launch`. It fetches origin and adds a git worktree at `<worktreeRoot>/<id>` on `warp/<id>-<jira>` from `origin/main`. Start one subagent per ticket in that worktree, in parallel up to `maxAgents`, with the prompt it prints. The prompt names the ticket, Jira key, locks, acceptance, branch, and absolute worktree path. The subagent works only inside that path, commits, pushes, and opens the pull request. It never merges, never touches the parent checkout, and never calls Jira or Slack. It writes `.warp/tickets/<id>/state.json` and `log.jsonl` at the parent absolute path and returns one line. After it returns, run `checkout.py verify`. A miss raises `lock-escape`. A line `result: <id> failed` is `worker-died` (`checkout.py result`). You do the Jira and Slack work. You merge serially. You push the beam on each merge and tick. Optional `launch: agent` prints an IMPLEMENT prompt for one new Agent. This plugin does not call a Cloud Agents API. Clone main so `.cursor` rules load. A beam file does not have to exist on the branch before that Agent starts.

A non-zero launch is not idle. Do not end the turn because launch failed. Record an optional Agent id with `checkout.py bind --agent`. `checkout.py remove` deletes a worktree. Merge and park do that too, then `git worktree prune`. `/warp-resume` reuses a surviving worktree and branch. Never two tickets in one worktree. Never two workers on one ticket.

On start, resume, and after every merge, failure, and freed slot: rebuild when the run is starting (`orchestrator.py rebuild --facts`), then `beam.py ready`, then start that list only. Starred and priority first, then lowest rank. Do not hold a slot for a ticket that is not ready. Do not wait for a dependency level. Record the pull request when the Shuttle exits (`orchestrator.py opened`). The slot stays occupied until `provider.py rollup` is green. Bugbot runs on the pull request after push, not in the VM. Locks stay until merge or park. If `mergeQueue` is true, or GitHub reports a merge queue, `provider.py merge-pr` enqueues and does not mark the ticket merged. A rejected direct merge is not merged. Then `state_commit.py commit` on the base branch.

`checkCommand` empty means Bugbot and CI as configured. `make ci` is a valid command. A red result is stored on `pr.check` and sent back to that ticket with the log (`send-back <id> fix` and a `start` line). Start that ticket as one subagent in its own worktree (`checkout.py launch`). A green result is not a failure. The gate stays while that check is red. `appendOnlyPaths` is empty by default. Keep both sides of a conflict there. Send any other conflict back. Park on the third red (`orchestrator.py fail`). Sizes not in `autoMergeSizes` wait for `/warp-proceed` or `warp:proceed`. If the base branch is red, stop merging and dispatch a fix ahead of every rank. Done means every ticket is merged or explicitly parked, and the base branch is green.

## One running tick

1. `beam.py check`
2. `beam.py watchdog`. Launch the replacements above. Herald posts each `herald:` line once.
3. Reconcile in-flight PRs and Jira.
4. `beam.py ready` recomputes every pending gate. A gate turns green when every member is merged or done. Post `herald: <gate> pending cleared. Members merged. Tick ran.` when it prints that line.
5. Claim the ready list only. No person cap. A `recovering` ticket is not in this list. `start` lines are the same ids. Claim each id once.
6. Claim each id, run `checkout.py launch`, and start one subagent in that worktree with the prompt it prints. Start those subagents in parallel up to `maxAgents`. Then `beam.py heartbeat` for that id. Herald posts each claim. When the subagent returns, run `checkout.py verify`. A failure line raises `worker-died`.
7. `scan.py status` and `beam.py board`.
8. Herald posts the tick digest: done, working, left, next ready.
9. Run `inbound.py apply-pending`, then `orchestrator.py supervise`. `listener: start` means claim a new `--turn` and start exactly one listener. `listener: hold` means do not start a second. If the listener just returned, pass `--returned` with that word (`recycle`, `pause`, or `stop`) and start the replacement in this same moment. Post a `herald:` line from supervise once. Push the live beam, then run `orchestrator.py parent-exit`. `parent: stay` means do not end the turn and do not release the listener. The listener is supposed to be running whenever the loop is running. Start the next pass in this same turn: supervise the one listener, read `.warp/tickets/<id>/`, patch the beam, dispatch with `checkout.py launch` and one subagent per worktree, push, and pass again. A launch refusal is not idle. `parent: exit` means the run is paused or stopped. Then `session_note.py --type session-stop` clears the listener and commits the live beam onto main. Pause and stop still sync state first, and then the listener may stop.

## Status file

`.warp/STATUS.md` and `.warp/status.json` are the files to open. Done, working, left. Regenerated every tick and on `/warp-status`.

## Notify

`notify: verbose` is the default. Herald posts scan, start, pause, resume, stop, claim, PR opened, Bugbot result, approval wait, merge, alarm, and gate change. See `agents/herald.md`.
