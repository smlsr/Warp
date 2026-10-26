---
name: warp-watch
description: Create, check, or re-create the Warp watch timer that keeps this run moving every few minutes
---

The watch timer is one recurring timer (a subscription) per run. It wakes the parent every `watchSeconds` (default 300) and each wake runs one `wake.py run`: the heartbeat, pending commands, the unstick step, the supervise pass, and new work for blockers nobody owns.

1. Print where it stands:

```bash
python3 <plugin>/scripts/wake.py status --beam .warp/beam.json
python3 <plugin>/scripts/wake.py setup --beam .warp/beam.json
```

2. If no timer with that name exists in your agent's timers or schedules, create it: recurring, every `watchSeconds`, with the prompt this prints. Delete any older Warp timer for this run first (one timer per run).

```bash
python3 <plugin>/scripts/wake.py prompt --beam .warp/beam.json
```

3. Record it:

```bash
python3 <plugin>/scripts/wake.py armed --beam .warp/beam.json --name <timer name>
```

`/warp-watch now` runs one wake now (`python3 <plugin>/scripts/wake.py run --beam .warp/beam.json`); act on its lines as on a pass. `watchSeconds: 0` turns the timer off. `wake.py ?` prints the help.
