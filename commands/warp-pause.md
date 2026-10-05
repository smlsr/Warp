---
name: warp-pause
description: Stop new Warp dispatch without losing state
---

Pause the beam with `scripts/beam.py pause` and the user's reason. That appends a `session-stop` journal line and stops the one channel listener. `inbound.py release` sets `listener.state` to `stopped` (`scripts/beam.py pause` does this). Tell Herald. Do not kill in-flight work. The listener must not keep reading while paused or stopped. Do not launch a listener on pause. Plugin hooks do not run on cloud runners.
