---
name: warp-status
description: "Write and report the Warp status file: done, working, and left. Use when the user asks what is done, what is left, or what is in flight."
---

# Warp status

```bash
python3 <plugin>/scripts/scan.py status --beam .warp/beam.json
python3 <plugin>/scripts/beam.py board --beam .warp/beam.json
```

`scan.py status` prints the resume hint first (`scripts/resume_hint.py`). Follow it. Do not dispatch from this command. Plugin hooks do not run on cloud runners.

`scan.py ?` and `beam.py ?` print the options. Quote `?` if the shell expands it.

Hand the user these paths:

- `.warp/STATUS.md` — done, working now, left
- `.warp/status.json` — the same, machine-readable, full lists
- `.warp/BOARD.md` — gates, alarms, next ready, tokens, ETA
- `.warp/board.html` — the same board in a browser
- `.warp/warp-complete.html` — the completion report, when the status footer names it

Lead the reply with alarms and stalls, then `in progress N/maxInProgress`, or `holding new launches (N/maxInProgress), N fix workers running` when that cap is full, then `live shuttles N/cap` with the fix workers that are running and the dead tickets waiting for a replacement, then the count of each state, then every ticket that is not merged. For each ticket include the state, the current step, how long it has been in that state, when it was last active, the pull request link, whether Bugbot, CI, and the acceptance criteria passed, the stall flag, the retry count, any alarm, and the next action Warp will take. The same rows are on the beam as `openWork`. Then report the last repair sweep: `sweep: at=...` with findings, actions, skipped, and escalated, or `sweep: none`. Do not paste the whole left list if it is long; point at the file.

The status script prints the plugin version (`Warp v<version>`, the installed copy, and the source copy). If it says the project copy is older, tell the user to run `/warp-uninstall` then `/warp-init`. Do not uninstall unless they ask.

Reconcile live PR and Jira state first if the user asked for current status rather than the last checkpoint.
