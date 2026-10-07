---
name: warp-pause
description: Stop new Warp dispatch without losing state
---

Pause the beam with `scripts/beam.py pause` or `scripts/scan.py pause` and the user's reason. That appends a `session-stop` journal line and stops the one channel listener. `inbound.py release` sets `listener.state` to `stopped`. Then `scripts/update_state.py` syncs: it loads origin/main, patches `.warp/tickets/<id>/`, puts this pause back on top, and pushes the beam to main before the command returns. Tell Herald. Do not kill in-flight work. The listener must not keep reading while paused or stopped. Do not launch a listener on pause. Plugin hooks do not run on cloud runners.
