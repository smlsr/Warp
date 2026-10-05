---
name: warp-resume
description: Resume Warp dispatch from the beam
---

Resume the beam with `scripts/scan.py resume`, which prints the resume hint. Reconcile in-flight tickets, then claim the one channel listener and run one Warp tick. Follow the hint. Do not dispatch when the beam is still paused.

One listener for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed.

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <new-id>
```

`listener: started <id>`: launch exactly one sub-agent with the `warp-listen` skill and that same agent id.

`listener: already running <id>`: do not launch a second.

Plugin hooks do not run on cloud runners. The listener must not keep reading while paused or stopped.
