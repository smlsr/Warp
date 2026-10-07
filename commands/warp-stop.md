---
name: warp-stop
description: Stop Warp until an explicit start
---

Set runState to stopped with `scripts/scan.py stop`. That appends a `session-stop` journal line and stops the one channel listener. `inbound.py release` sets `listener.state` to `stopped`. Then `scripts/update_state.py` syncs: it loads origin/main, patches `.warp/tickets/<id>/`, puts this stop back on top, and pushes the beam to main before the command returns. In-flight Shuttles checkpoint and do not take a new ticket. Tell Herald. A later tick must not dispatch until `/warp-start`. The listener must not keep reading while paused or stopped. Plugin hooks do not run on cloud runners.
