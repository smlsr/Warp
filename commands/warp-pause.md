---
name: warp-pause
description: Stop new Warp dispatch without losing state
---

Pause the beam with `scripts/beam.py pause` and the user's reason. That appends a `session-stop` journal line. Tell Herald. Do not kill in-flight work. Plugin hooks do not run on cloud runners.
