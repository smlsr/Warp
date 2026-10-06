---
name: warp
description: Start or tick the Warp master loop
---

Run the `warp` skill for one tick. If the user said "start", loop ticks until paused, capped with nothing ready, or they say stop. Re-read the beam every tick. Do not implement tickets in this command. Before the tick, run `scripts/resume_hint.py` and follow it. At the end of the tick, run `scripts/session_note.py --type session-stop`. Plugin hooks do not run on cloud runners.

Run `scripts/beam.py watchdog` on this tick, before `ready()`. A fresh heartbeat is left alone. `watchdog: skipped` means do not recover. `listener: replace <id>` means the listener died while the run is running: launch exactly one `warp-listen` agent with that id. Do not launch a second listener for any other reason. `listener: alive`, `listener: fresh`, and `listener: already running` mean do not launch a second. `shuttle: replace <id>` means launch exactly one Shuttle for that same ticket and keep its branch. A second tick must not launch another. `/warp-pause` and `/warp-stop` stop the listener. It must not keep reading while paused or stopped.
