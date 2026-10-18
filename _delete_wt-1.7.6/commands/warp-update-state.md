---
name: warp-update-state
description: Sync main, ticket folders, and parent state, then push the beam
---

Insurance sync. Run it when the checkout and main may have diverged, and after `/warp-pause` or `/warp-stop` (those commands already run it).

```bash
python3 <plugin>/scripts/update_state.py --beam .warp/beam.json
```

The slash command can be missing from Cursor's plugin command index. This script is already on a 1.4.12 install and does not need it:

```bash
python3 .cursor/plugins/warp/scripts/update_state.py --beam .warp/beam.json
python3 .cursor/plugins/warp/scripts/update_state.py ?
```

`update_state.py ?` prints the options. Quote `?` if the shell expands it.

1. Fetch origin/main and load that beam.
2. Fetch each in-flight ticket branch and patch from `.warp/tickets/<id>/` only. Do not replace the beam with a ticket branch's `beam.json`.
3. Apply local parent state on top: pause, stop, claims, and runState the parent has that the remote beam does not.
4. Write the reconciled beam, journal, and board. Strip tokens, cost, API keys, and webhook URLs. Do not commit `.warp/config.yaml`.
5. Push that beam to main.
6. Read the short summary: `pulled:`, `local won:`, and `commit:`.

The listener is a Subagent of the parent. Each ticket is a subagent in its own git worktree, or on its own VM when `subagentVm` is true. This command does not start a listener and does not dispatch.
