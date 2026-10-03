---
name: warp-status
description: "Write and report the Warp status file: done, working, and left. Use when the user asks what is done, what is left, or what is in flight."
---

# Warp status

```bash
python3 <plugin>/scripts/scan.py status --beam .warp/beam.json
python3 <plugin>/scripts/beam.py board --beam .warp/beam.json
```

Hand the user these paths:

- `.warp/STATUS.md` — done, working now, left
- `.warp/status.json` — the same, machine-readable, full lists
- `.warp/BOARD.md` — gates, alarms, next ready, tokens, ETA
- `.warp/board.html` — the same board in a browser

Lead the reply with the three counts and the working ids. Do not paste the whole left list if it is long; point at the file.

Reconcile live PR and Jira state first if the user asked for current status rather than the last checkpoint.
