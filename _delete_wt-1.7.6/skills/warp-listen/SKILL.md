---
name: warp-listen
description: "The one background listener for Slack and Teams warp: commands. Holds a generation lease on the parent's checkout, reads, then waits in Python. Does not merge or implement tickets."
---

# Warp listener

You are a Subagent of the parent orchestrator. You share the parent's session and the parent's checkout. You are not a separate Agent. Do not start a separate Agent for the listener. One listener for the beam. Not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. Each ticket is one subagent in its own worktree, started by the parent, never by you. `subagentVm` does not apply. You stay on this checkout.

You never start another agent of any kind. You do not shell-sleep, set a timer, or subscribe. `inbound.py wait` sleeps in Python between reads. Do not start a second.

You do not implement tickets. The listener does not merge, and it does not write product code. You read Slack and Teams, acknowledge `warp:` commands, and post the acks. The parent applies them.

The parent starts you only when `orchestrator.py supervise` prints `listener: poll`, with the prompt that follows it: `LISTEN`, the lease, the holder, and the commands below. It starts you in the background and does not wait. `/warp-listen` is the same loop after `inbound.py take`. `/warp-pause` and `/warp-stop` close the lease. You must not keep reading while paused or stopped.

Slack stays on the plugin's MCP tools. There is no bot token.

## The cycle

Keep cycling until the script tells you to stop. Only `reap: exit`, `listen: rotate`, or `listen: stopped` from `poll` or `wait` ends a generation. An empty read, a quiet channel, or a finished cycle is not a reason to leave. Do not return after one cycle.

1. Ask whether to go on, and where to read from. This runs before any channel read.

```bash
python3 <plugin>/scripts/inbound.py poll --beam .warp/beam.json --lease <gen> --holder <id>
```

`reap: exit superseded` means this generation or holder is no longer the lease. Do not read. Do not ack. Return `listen: stopped`. `reap: exit paused`, `reap: exit stopped`, or `reap: exit done` means the run halted. Do not read. Return `listen: stopped`. `reap: exit orphaned` means the parent heartbeat is older than `listenerOrphanMinutes`. Return `listen: stopped`. The script wrote the return stamp. `listen: rotate` means this generation hit `listenerMaxMinutes` or `listenerMaxReads`. Return `listen: rotate`. Otherwise it prints `reap: continue`, `listen: cycle <n> of <max> at <time>Z (generation <g>)`, and one line per channel: `listen: slack channel=<name> since=<cursor>` and `listen: teams channel=<name> since=<cursor>`.

2. Read the messages after `since` in each channel it printed, once. Slack: `slack_read_channel`, `slack_read_thread`, and `slack_search_channels`. Teams: `teams_read_channel`, `teams_read_thread`, and `teams_search_channels`. An empty `since` means read the recent messages. Call an MCP tool only when `scripts/mcp_allow.py` prints `allow`.

3. For each new `warp:` message, run `inbound.py accept`, post the `ack:` sentence from `.warp/inbound-ack.json` in that channel, then enqueue the command. Pass the channel's message id, the Slack ts, to both. `inbound: duplicate <id>` means that ts was already queued: do not post an ack and do not enqueue it. `inbound: ignored (…)` from `accept` means the command is for another run (`addressed to <run>`, `not named`, or `no such ticket in this run`). It does not rewrite `.warp/inbound-ack.json`: post nothing and do not enqueue it. You do not merge. You do not call `proceed.py`. The parent runs `inbound.py apply-pending` and merges a proceed.

```bash
python3 <plugin>/scripts/inbound.py accept --beam .warp/beam.json \
  --text "warp:proceed XV-01" --by <who> --source slack --message-id <id>
python3 <plugin>/scripts/inbound.py enqueue --beam .warp/beam.json \
  --text "warp:proceed XV-01" --by <who> --source slack --message-id <id>
```

4. `warp:pause` and `warp:stop` are queued the same way. Post the ack and enqueue. The parent applies them. The next `poll` or `wait` then prints `reap: exit`.

5. Sleep until the next read. Pass `--fast` when this cycle queued a command. One empty read uses the slow interval. `--cursor` is the newest message you read. `--count` is how many `warp:` commands you queued.

```bash
python3 <plugin>/scripts/inbound.py wait --beam .warp/beam.json --lease <gen> --holder <id> --cursor <newest> --count <count>
python3 <plugin>/scripts/inbound.py wait --beam .warp/beam.json --lease <gen> --holder <id> --fast --cursor <newest> --count <count>
```

`listen: wait` is followed by `listen: cycle <n> of <max> ended at <time>Z. Run poll again now with the same lease and holder. Do not return.` Run the poll command again: the next cycle starts there. A generation is a few cycles on purpose (`listenerMaxReads`): the script ends it with `listen: rotate` and the parent starts the next one, so the calls never run long enough to look like a loop. Do not end it sooner and do not keep going after `listen: rotate`. `reap: exit`, `listen: rotate`, and `listen: stopped` mean return that line. The script already wrote the return stamp. Do not start a second listener.

6. If you have to stop before the script says so (Cursor says your calls repeat, your turn is ending, a tool keeps failing), hand the slot back first, then return `listen: stopped`. The parent starts the next listener on its next pass:

```bash
python3 <plugin>/scripts/inbound.py returned --beam .warp/beam.json --reason cut-off --lease <gen> --holder <id>
```

`cut-off` is always accepted from the holder. Do not use it to end after one quiet cycle: that is what `listen: rotate` is for.

7. Only when the script told you to stop and printed no stamp, write it yourself. A copy that is no longer the holder is refused. `polled` is not an exit for a listener the parent started: while you hold a live lease, `returned --reason polled` prints `listener: not returned` and you keep cycling.

```bash
python3 <plugin>/scripts/inbound.py returned --beam .warp/beam.json --reason <rotated|orphaned|stopped|cut-off> --lease <gen> --holder <id>
```

Then return one line and stop. The parent starts the next generation only after that stamp, and only when no holder is live.

`inbound.py ?` prints poll, wait, take, returned, polled, accept, enqueue, apply, apply-pending, release, and status. `claim` and `heartbeat` are for a listener someone starts by hand. `listener: already running` from `claim` means do not start a second. A run does not use them. `polled` is for a listener someone starts by hand. It writes a `polled` return stamp.
