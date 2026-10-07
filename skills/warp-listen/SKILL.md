---
name: warp-listen
description: "One channel listener for the whole Warp run. Reads Slack and Teams for warp: commands, acks in channel, then acts. Launched by /warp-start and /warp-resume. Stopped by /warp-pause and /warp-stop."
---

# Warp listener

One listener for the beam. Not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. Five tickets waiting on approval are still this one Agent. Do not become one Agent per ticket. A ticket worker is a new Agent, not a Subagent of this listener.

`/warp-start` and `/warp-resume` launch you when `inbound.py claim` prints `listener: started <id>`. `listener: already running <id>` means do not launch a second. `/warp-pause` and `/warp-stop` stop you. You must not keep reading while paused or stopped.

Plugin hooks do not run on cloud runners. Do not wait for a hook to notice Slack. There is no webhook. Cursor cannot start this turn from a Slack message. Stay in this turn and re-read the channel every `pollSeconds` while `listener.state` is `running`. If this turn has already ended, `lastSeenAt` goes stale. The next tick or `/warp-resume` runs `beam.py watchdog`. After `staleMinutes` it clears the stale `running` flag and starts one replacement. Until that heartbeat is stale, `listener: already running` means do not launch a second. `inbound.py release` (or `/warp-pause`) clears it immediately. Pause and stop do not start a replacement.

The slot is `listener` on the beam: `state` (`running` or `stopped`), `agentId` (one id), optional `pid`, `startedAt`, and `lastSeenAt`.

A dead turn does not notify Warp. Cursor does not restart you. Plugin hooks do not run on cloud runners. Write a heartbeat so the watchdog can see you. There is no process table.

## Claim

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <id> --pid <pid>
python3 <plugin>/scripts/inbound.py heartbeat --beam .warp/beam.json --agent-id <id>
python3 <plugin>/scripts/inbound.py status --beam .warp/beam.json
```

Claim writes `lastSeenAt`. Heartbeat writes it again. If status is `listener: running` for a different id, exit. Do not read the channel.

## Loop

While status prints `listener: running <your id>`, heartbeat before you read. Repeat at least every 5 minutes, and always inside `staleMinutes` (default 15).

```bash
python3 <plugin>/scripts/inbound.py heartbeat --beam .warp/beam.json --agent-id <id>
```

1. Read new messages in `slackChannel` with `slack_read_channel`, `slack_read_thread`, and `slack_search_channels`. When `messenger` is `teams` or `both`, also read `teamsChannel` with `teams_read_channel`, `teams_read_thread`, and `teams_search_channels`. Call an MCP tool only when `scripts/mcp_allow.py` prints `allow`.
2. Enqueue each new `warp:` message. A message id that was already written is skipped.

```bash
python3 <plugin>/scripts/inbound.py enqueue --beam .warp/beam.json \
  --text "warp:proceed XV-01" --by <who> --message-id <ts> --source slack
python3 <plugin>/scripts/inbound.py drain --beam .warp/beam.json
```

3. For each `ack:` line, Herald posts that sentence in the same channel before you act. The payload is `.warp/inbound-ack.json`. Post it first. Then do the action under that ack, and only that.
4. `warp:proceed <id>` is the orchestrator merging that one awaiting_approval ticket. A Shuttle does not merge. The id may be the plan id or the Jira key. Follow the `proceed.py` steps drain prints: merge, then the post-merge MUST DO, including Jira to Done when `jiraDoneOnManualMerge` is true. Other waiting tickets stay waiting. A bad id was already acked. Do not merge anything else.
5. `warp:retry <id>` requeues that alarmed ticket. `warp:status` posts the digest after the ack. `warp:pause` and `warp:stop` have already stopped the listener. Exit the loop. Do not keep reading. `warp:resume` and `warp:start` leave you as the one listener. Do not launch a second.
6. An unknown or malformed `warp:` command was acked with the accepted forms. Do nothing else.
7. Wait `pollSeconds`, then read again.
8. On each pass, while this listener is running and the run is not paused or stopped, ask for one lock-escape repair. `alarmRepairMinutes` (default 15) is how often a new pass opens. The script enforces that, so call it every pass. A second call is idempotent.

```bash
python3 <plugin>/scripts/alarm_repair.py next --beam .warp/beam.json
```

`alarm-repair: skipped` means paused or stopped. Do not launch. `alarm-repair: wait` means the interval has not elapsed. `alarm-repair: working` or `alarm-repair: waiting` means a repair Shuttle is still on that ticket. Do not launch another. `alarm-repair: naming` means the path-naming Shuttle is still out. Do not launch another.

`alarm-repair: name <id>` means the alarm has no escaped paths. Start one new Agent for that id whose only job is to name them, then stop. `checkout.py launch` first, then the Agent checked out on that branch. Do not start a Subagent of this listener. Do not use the Task tool. It runs `alarm_repair.py paths --id <id> --path <path>` for each file or directory it needs outside the lock, and it does not edit. When that Agent has finished, run `next --returned <id>` before starting anyone else. Do not wait on a Subagent in this turn.

`alarm-repair: locked <id> by <holder>` means an in-flight ticket holds a path this repair would take. Do not launch and do not widen. The next pass widens and starts after that holder finishes. A queued ticket does not block this.

`alarm-repair: start <id>` means the script already widened that ticket's locks to the paths that escaped (`addedLocks`, and `Added ...` on the herald line). Start exactly one new Agent for that ticket, `IMPLEMENT <id>`, the same way `/warp` does: `checkout.py launch`, then the Agent checked out on that branch. Do not start a Subagent of this listener. Do not use the Task tool. Do not implement the ticket in this turn. It works inside those locks. It does not add more paths itself. The alarm is still `lock-escape` until the work is really on the normal path. Do not clear it.

When this pass opens and the ready set is empty, the same script recomputes every pending gate. A gate turns green when every member is merged or done. The output then includes `herald: G1 pending cleared. Members merged. Tick ran.` and one `start <id>` line per ticket the dispatch tick selected. Post that herald line. Claim and start one new Agent for each of those ids, the same way `/warp` does: `checkout.py launch`, then the Agent checked out on that branch. Do not start a Subagent. Do not use the Task tool. Do not implement the ticket in this turn. Do not stop after the herald line. A second pass does not print those start lines. Do not start a second Agent for that id. A member that is queued, claimed, in progress, awaiting approval, alarmed, or parked leaves the gate pending. A green gate stays green. Pause and stop do not recompute. Every Warp tick also recomputes before `beam.py ready`, so a merge does not wait for this pass. This pass is the backstop when no tick is running. A red `make ci` prints `send-back <id> fix` and `start <id>` in that same pass, even when the ready set is not empty. Start that ticket as one new Agent with the log (`checkout.py launch`, then the Agent on that branch). Do not start a Subagent. A second pass does not print it again while the ticket is in `fix`. Parked (the third red) does not launch.

When that Agent has finished, run `next --returned <id>` before starting another repair. Do not wait on a Subagent in this turn. A failure (`alarm` / `lock-escape` again, or `--error`) leaves the alarm, records the attempt, and the same output starts the next ticket. Do not retry the one that just failed in this pass. A pass that is still on the normal path with the alarm cleared, or merged, waits until that completion, then starts the next. `maxAlarmRepairs` (default 3) skips a ticket after that many attempts. Herald posts each `herald:` line once: a repair started (including the paths added), a failure that takes the next ticket, and a ticket given up. The same lines are in `.warp/alarm-repair.json`.

Paused or stopped: you are not running, so you do not call this. Other alarm reasons are not repaired. `/warp` does not launch these Shuttles. You do.

`inbound.py ?` prints claim, release, handle, enqueue, and drain. `alarm_repair.py ?` prints `next` and `paths`.
