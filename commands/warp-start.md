---
name: warp-start
description: Start Warp dispatch after a scan
---

Set runState to running with `scripts/scan.py start`, tell Herald, then run one Warp tick. Refuse if there is no beam. `scan.py start` prints the resume hint. Follow it. Do not dispatch when it says the beam is missing or paused.
