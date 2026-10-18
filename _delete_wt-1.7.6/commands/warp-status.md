---
name: warp-status
description: Show the beam board, ETA, tokens, and blockers
---

Run the `warp-status` skill. `scan.py status` prints the resume hint, then the open-work list, then rewrites the board and prints the version block, which includes `instance: warp:<instance> host=<host> machine=<machine id>`. Show the instance line: it is the tag this run's agents and Slack messages carry, and it tells this Warp run from another. Lead with alarms and stalls, then the count of each state. Include `in progress N/maxInProgress`, or `holding new launches (N/maxInProgress), N fix workers running` when that cap is full. Include `live shuttles N/cap`, how many fix workers are running, and the dead tickets waiting for a replacement. Include the last repair sweep (`sweep: at=...` or `sweep: none`). Do not dispatch.
