---
name: warp-stop
description: Stop Warp until an explicit start
---

Set runState to stopped with `scripts/scan.py stop`. That appends a `session-stop` journal line. In-flight Shuttles checkpoint and do not take a new ticket. Tell Herald. A later tick must not dispatch until `/warp-start`. Plugin hooks do not run on cloud runners.
