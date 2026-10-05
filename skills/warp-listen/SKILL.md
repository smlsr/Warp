---
name: warp-listen
description: "One channel listener for the whole Warp run. Reads Slack and Teams for warp: commands, acks in channel, then acts. Launched by /warp-start and /warp-resume. Stopped by /warp-pause and /warp-stop."
---

# Warp listener

One listener for the beam. Not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. Five tickets waiting on approval are still this one sub-agent.

`/warp-start` and `/warp-resume` launch you when `inbound.py claim` prints `listener: started <id>`. `listener: already running <id>` means do not launch a second. `/warp-pause` and `/warp-stop` stop you. You must not keep reading while paused or stopped.

Plugin hooks do not run on cloud runners. Do not wait for a hook to notice Slack. There is no webhook. Cursor cannot start this turn from a Slack message. Stay in this turn and re-read the channel every `pollSeconds` while `listener.state` is `running`. If this turn has already ended, the flag can still say running, and a later start will not launch a second listener. `inbound.py release` (or `/warp-pause`) clears it. `/warp-resume` launches one listener again.

The slot is `listener` on the beam: `state` (`running` or `stopped`), `agentId` (one id), and optional `pid`.

## Claim

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <id> --pid <pid>
python3 <plugin>/scripts/inbound.py status --beam .warp/beam.json
```

If status is `listener: running` for a different id, exit. Do not read the channel.

## Loop

While status prints `listener: running <your id>`:

1. Read new messages in `slackChannel` with `slack_read_channel`, `slack_read_thread`, and `slack_search_channels`. When `messenger` is `teams` or `both`, also read `teamsChannel` with `teams_read_channel`, `teams_read_thread`, and `teams_search_channels`. Call an MCP tool only when `scripts/mcp_allow.py` prints `allow`.
2. Enqueue each new `warp:` message. A message id that was already written is skipped.

```bash
python3 <plugin>/scripts/inbound.py enqueue --beam .warp/beam.json \
  --text "warp:proceed XV-01" --by <who> --message-id <ts> --source slack
python3 <plugin>/scripts/inbound.py drain --beam .warp/beam.json
```

3. For each `ack:` line, Herald posts that sentence in the same channel before you act. The payload is `.warp/inbound-ack.json`. Post it first. Then do the action under that ack, and only that.
4. `warp:proceed <id>` merges that one awaiting_approval ticket. The id may be the plan id or the Jira key. Follow the `proceed.py` steps drain prints: merge, then the post-merge MUST DO, including Jira to Done when `jiraDoneOnManualMerge` is true. Other waiting tickets stay waiting. A bad id was already acked. Do not merge anything else.
5. `warp:retry <id>` requeues that alarmed ticket. `warp:status` posts the digest after the ack. `warp:pause` and `warp:stop` have already stopped the listener. Exit the loop. Do not keep reading. `warp:resume` and `warp:start` leave you as the one listener. Do not launch a second.
6. An unknown or malformed `warp:` command was acked with the accepted forms. Do nothing else.
7. Wait `pollSeconds`, then read again.

`inbound.py ?` prints claim, release, handle, enqueue, and drain.
