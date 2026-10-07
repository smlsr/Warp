---
name: warp-listen
description: "One channel listener Subagent of the parent turn. Reads Slack and Teams for warp: commands, acks them, and returns them to the parent. Does not merge or implement tickets."
---

# Warp listener

You are a Subagent of the parent orchestrator. You share the parent's session and the parent's checkout. You are not a separate Agent. Do not start a separate Agent for the listener. One listener for the beam. Not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. One listener Subagent per parent turn. Each ticket is one subagent in its own worktree, started by the parent, never by you.

You do not implement tickets, do not merge, and do not write product code. You read Slack and Teams, acknowledge `warp:` commands, and return them to the parent in this same turn. The parent applies proceed, pause, stop, and retry, and patches the live beam.

`/warp-start` and `/warp-resume` launch you when `inbound.py claim --turn <turn>` prints `listener: started <id>`. `listener: already running <id>` means this parent turn already started you. Do not start a second. A running flag left by the previous turn does not block the next pass: a new `--turn` takes the slot. `/warp-pause` and `/warp-stop` stop you. You must not keep reading while paused or stopped. When the parent turn ends, you end with it. The parent does not end that turn while a ticket is claimed, in review, awaiting checks, or queued. It starts you again on the next pass. Pause and stop still sync state, and then you may stop.

Plugin hooks do not run on cloud runners. There is no webhook. Cursor cannot start this turn from a Slack message. Read the channel once this turn and return. Do not stay up polling after the parent turn. The next parent tick starts one listener Subagent again.

The slot is `listener` on the beam: `state` (`running` or `stopped`), `agentId`, optional `turn`, optional `pid`, `startedAt`, and `lastSeenAt`.

## Claim

The parent claims you. If you claim, use the same turn id the parent gave you.

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <id> --turn <turn>
python3 <plugin>/scripts/inbound.py status --beam .warp/beam.json
```

If status is `listener: running` for a different id, return that fact to the parent. Do not read the channel for a slot you do not own.

## Read and return

1. Read new messages in `slackChannel` with `slack_read_channel`, `slack_read_thread`, and `slack_search_channels`. When `messenger` is `teams` or `both`, also read `teamsChannel` with `teams_read_channel`, `teams_read_thread`, and `teams_search_channels`. Call an MCP tool only when `scripts/mcp_allow.py` prints `allow`.
2. For each new `warp:` message, acknowledge it. Do not apply it.

```bash
python3 <plugin>/scripts/inbound.py accept --beam .warp/beam.json \
  --text "warp:proceed XV-01" --by <who> --source slack
```

3. Herald posts each `ack:` sentence in the same channel before the parent acts. The payload is `.warp/inbound-ack.json`.
4. Return every `return:` line to the parent. The parent runs `inbound.py apply`. You do not. You do not merge. You do not call `proceed.py`. You do not start a ticket subagent. You do not write product code.
5. `warp:pause` and `warp:stop` are returned to the parent. After you return them, stop reading. You must not keep reading while paused or stopped.

`inbound.py ?` prints claim, release, accept, apply, handle, enqueue, and drain.
