---
name: warp-stop
description: Stop Warp until an explicit start
---

Set runState to stopped with `scripts/scan.py stop`. That appends a `session-stop` journal line and stops the one channel listener. `inbound.py release` sets `listener.state` to `stopped` (`scripts/scan.py stop` does this). In-flight Shuttles checkpoint and do not take a new ticket. Tell Herald. A later tick must not dispatch until `/warp-start`. The listener must not keep reading while paused or stopped. Plugin hooks do not run on cloud runners.
