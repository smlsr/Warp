---
name: warp-stop
description: Stop Warp until an explicit start
---

Set runState to stopped. In-flight Shuttles checkpoint and do not take a new ticket. Tell Herald. A later tick must not dispatch until `/warp-start`.
