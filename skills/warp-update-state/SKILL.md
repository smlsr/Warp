---
name: warp-update-state
description: "Fetch main and each ticket folder, keep the parent's pause, stop, and claims, and push that beam to main. Use when state should be reconciled before the window closes."
---

# Warp update state

Insurance sync. `/warp-pause` and `/warp-stop` run this after they set paused or stopped. Run it yourself when main and this checkout may have diverged.

```bash
python3 <plugin>/scripts/update_state.py --beam .warp/beam.json
```

The slash command can be missing from Cursor's plugin command index. This script is already on a 1.4.12 install and does not need it:

```bash
python3 .cursor/plugins/warp/scripts/update_state.py --beam .warp/beam.json
python3 .cursor/plugins/warp/scripts/update_state.py ?
```

`update_state.py ?` prints the options. Quote `?` if the shell expands it.

1. Fetch origin/main and load that beam. Do not start from an empty beam if main has one.
2. Fetch each in-flight ticket branch and patch from `.warp/tickets/<id>/` only. Do not copy that branch's `beam.json`.
3. Apply local parent state on top: pause, stop, claims, and runState this checkout has that the remote beam does not.
4. Write the reconciled beam, journal, and board. Strip tokens, cost, API keys, and webhook URLs. Do not commit `.warp/config.yaml`.
5. Push that beam to main.
6. Tell the user the summary: what was pulled, what local state won, and the commit on main (`pulled:`, `local won:`, `commit:`).

The listener is a Subagent of the parent. Each ticket is a subagent in its own git worktree, or on its own VM when `subagentVm` is true. Do not start a listener. Do not dispatch. Do not merge.
