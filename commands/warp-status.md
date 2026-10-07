---
name: warp-status
description: Show the beam board, ETA, tokens, and blockers
---

Run the `warp-status` skill. `scan.py status` prints the resume hint, then the open-work list, then rewrites the board. Lead with alarms and stalls, then the count of each state. Include `in progress N/maxInProgress`, or `holding new launches (N/maxInProgress), N fix workers running` when that cap is full. Include `live shuttles N/cap`, how many fix workers are running, and the dead tickets waiting for a replacement. Include the last repair sweep (`sweep: at=...` or `sweep: none`). Do not dispatch.
