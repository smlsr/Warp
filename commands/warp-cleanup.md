---
name: warp-cleanup
description: List registered Warp agents and archive idle cloud agents
---

List every agent in `.warp/agents.json` and, when `CURSOR_API_KEY` is set, archive idle cloud agents.

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
```

The slash command can be missing from Cursor's plugin command index. This script does not need it:

```bash
python3 .cursor/plugins/warp/scripts/agents.py list --beam .warp/beam.json
python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
python3 .cursor/plugins/warp/scripts/agents.py ?
```

`agents.py ?` prints the options. Quote `?` if the shell expands it. `--cloud` reads `GET /v1/agents`. `--apply` archives with `POST /v1/agents/{id}/archive`. The key is the environment variable `CURSOR_API_KEY` and is never written to the repo.

Without the key, the command prints each cloud agent as `https://cursor.com/agents/<id>` so those agents can be archived in the Cursor UI.

A pile of idle agents left by an older run is not in `.warp/agents.json`. `--cloud --apply` still archives every IDLE agent the key can see. That is how to clear about 199 unarchived Shuttles, Bugbot runs, and listeners: export `CURSOR_API_KEY`, then run the cleanup command above.

Merge and park release that ticket's agents. `/warp-stop` marks every registered agent stopped and runs cleanup. When every ticket is merged or parked, the parent runs cleanup and exits. Nothing new is spawned while the run is paused or stopped.
