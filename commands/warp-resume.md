---
name: warp-resume
description: Resume Warp dispatch from the beam
---

Resume the beam with `scripts/scan.py resume`, which prints the resume hint and runs `beam.py watchdog`. Reconcile in-flight tickets, then claim the one channel listener and run one Warp tick. Follow the hint. Do not dispatch when the beam is still paused. `watchdog: skipped` means do not recover and do not start a replacement.

One listener for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed.

Read the watchdog lines before you claim:

- `listener: replace <id>`: launch exactly one `warp-listen` Agent with that id. One Agent for the beam, not one Agent per ticket. The slot is already reserved. Herald posts `Listener died. A new one started.`
- `listener: alive <id>` or `listener: fresh <id>`: do not launch a second.
- `listener: stopped`: claim, and launch only on `listener: started`.

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <new-id>
```

`listener: started <id>`: launch exactly one Agent with the `warp-listen` skill and that same agent id. Ticket workers are not Subagents of that listener.

`listener: already running <id>`: do not launch a second, unless this resume printed `listener: replace` for that same id and you have not launched it yet.

For each `shuttle: replace <id>` line, start exactly one new Agent for that same ticket (`checkout.py launch`, then the Agent on that branch). Do not start a Subagent. Do not use the Task tool. Do not start it on a fresh clone of main. Keep the branch. Do not queue a duplicate. A second tick must not launch another. `shuttle: alarm <id> worker-died` does not start a Shuttle.

Plugin hooks do not run on cloud runners. The listener must not keep reading while paused or stopped. A dead turn does not notify Warp. Cursor does not restart it.
