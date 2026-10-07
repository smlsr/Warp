---
name: warp-start
description: Start Warp dispatch after a scan
---

Set runState to running with `scripts/scan.py start`, tell Herald, then claim the one channel listener. Refuse if there is no beam. If start prints `refuse:` and names missing MCP tools, do not start and do not claim. Post that list (Herald already has it). `scan.py start --force` starts anyway. A local runner does not block. `scan.py start` prints the resume hint and runs `beam.py watchdog`. Follow it. Do not dispatch when it says the beam is missing or paused. `watchdog: skipped` means do not recover and do not start a replacement. A later claim runs the same check unless this start was `--force`.

One listener for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. The slot is `listener` on the beam: `state`, `agentId`, optional `pid`, and `lastSeenAt`.

Read the watchdog lines before you claim:

- `listener: replace <id>`: the dead listener was cleared and this id already owns the slot. Launch exactly one `warp-listen` Agent with that id. One Agent for the beam, not one Agent per ticket. Do not claim a different id. Herald posts one line: `Listener died. A new one started.`
- `listener: alive <id>` or `listener: fresh <id>`: do not launch a second.
- `listener: stopped`: claim, then launch only when the reply is `listener: started`.

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <new-id>
```

`listener: started <id>` means you claimed the slot. Launch exactly one Agent with the `warp-listen` skill and that same agent id. Ticket workers are not Subagents of that listener.

`listener: already running <id>` means a listener is already running. Do not launch a second, unless this start printed `listener: replace` for that same id and you have not launched it yet. Launch that one id once.

For each `shuttle: replace <id>` line, start exactly one new Agent for that ticket (`checkout.py launch`, then `IMPLEMENT <id>` on that branch). Do not start a Subagent. Do not use the Task tool. Keep its branch, pull request, `jira.startedAt`, and locks. Do not queue a duplicate. Do not start it on a fresh clone of main. `shuttle: alarm <id> worker-died` means the recovery cap is reached. Do not start another. Herald posts `<id> worker died. A new Shuttle started.` once.

Then run one Warp tick. That tick runs the watchdog again. A second tick must not launch a second replacement. Plugin hooks do not run on cloud runners. The listener is not a hook. It must not keep reading while paused or stopped. A dead turn does not notify Warp. Cursor does not restart it.
