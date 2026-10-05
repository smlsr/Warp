---
name: warp-start
description: Start Warp dispatch after a scan
---

Set runState to running with `scripts/scan.py start`, tell Herald, then claim the one channel listener. Refuse if there is no beam. `scan.py start` prints the resume hint. Follow it. Do not dispatch when it says the beam is missing or paused.

One listener for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. The slot is `listener` on the beam: `state`, `agentId`, and optional `pid`.

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <new-id>
```

`listener: started <id>` means you claimed the slot. Launch exactly one sub-agent with the `warp-listen` skill and that same agent id.

`listener: already running <id>` means a listener is already running. Do not launch a second.

Then run one Warp tick. Plugin hooks do not run on cloud runners. The listener is not a hook. It must not keep reading while paused or stopped.
