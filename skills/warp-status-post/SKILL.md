---
name: warp-status-post
description: "Post Warp status to Slack and Teams, including the status files. Use when the user asks to send status to the channel, or when a channel message says warp:status."
---

# Post status to Slack or Teams

Teams and Slack cannot pull files from a stopped agent. They can ask, and Warp can push, when the agent is running or a tick fires.

## Push now

```bash
python3 <plugin>/scripts/status_post.py --beam .warp/beam.json --out .warp/status-post.json
```

Read `.warp/status-post.json`. Post `text` to every messenger in `config.messenger`. Attach `.warp/STATUS.md`, `.warp/status.json`, and `.warp/BOARD.md` with the connected Slack or Teams file tool. If the connector can only send text, paste `statusMarkdown` and say the files are in `.warp/`. If a connector is missing, append the same text to `.warp/outbox.md`.

Channel names, if set: `teamsChannel`, `slackChannel` in `.warp/config.yaml`. Otherwise post to the channel the user named.

## Pull from the channel

On each tick, read new messages in the configured channels. If a message is `warp:status`, run this skill and reply in that thread. Also honor `warp:pause`, `warp:resume`, `warp:stop`, `warp:start`, `warp:proceed <id>`, and `warp:retry <id>`.

A quiet notify setting still posts this reply. Someone asked.
