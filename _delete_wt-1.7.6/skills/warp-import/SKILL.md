---
name: warp-import
description: "Replace the live Warp plan with an uploaded WARP_PLAN.json or schedule.json. Use after the user edits an export and wants Warp to run the new graph."
---

# Warp import

```bash
python3 <plugin>/scripts/scan.py import --plan path/to/WARP_PLAN.json --beam .warp/beam.json
```

Replaces deps, locks, gates, and sizes. Keeps status, PR, tokens, and minutes for ids that still exist (`--keep-status` is the default). New ids start `queued`. Removed ids drop off. `runState` returns to `stopped`. `scan.py import ?` prints the options. Quote `?` if the shell expands it.

Confirm the ticket count, then wait for `/warp-start`. Do not start in the same turn unless the user asked.
