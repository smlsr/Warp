---
name: warp
description: Start or tick the Warp master loop
---

Run the `warp` skill for one tick. If the user said "start", loop ticks until paused, capped with nothing ready, or they say stop. Re-read the beam every tick. Do not implement tickets in this command. Before the tick, run `scripts/resume_hint.py` and follow it. At the end of the tick, run `scripts/session_note.py --type session-stop`. Plugin hooks do not run on cloud runners.
