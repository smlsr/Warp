---
name: warp-cleanup
description: List registered Warp agents and dry-run or archive idle cloud agents for this repo
---

List every agent in `.warp/agents.json`. With `CURSOR_API_KEY` set, a dry run lists idle cloud agents for this repo. `--apply` archives that list.

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
```

The slash command can be missing from Cursor's plugin command index. This script does not need it:

```bash
python3 .cursor/plugins/warp/scripts/agents.py list --beam .warp/beam.json
python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud
python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
python3 .cursor/plugins/warp/scripts/agents.py ?
```

`agents.py ?` prints the options. Quote `?` if the shell expands it. `--cloud` reads `GET /v1/agents`. Without `--apply` the command prints each match and changes nothing. `--apply` archives with `POST /v1/agents/{id}/archive`. The key is the environment variable `CURSOR_API_KEY` and is never written to the repo.

A match is IDLE. Its repository is this checkout's origin. It is in `.warp/agents.json`, or its name or prompt is a Warp role: Shuttle, IMPLEMENT, fix, rebase, listener, or Warp-triggered Bugbot. The agent running the command stays. A RUNNING or ACTIVE agent stays. The dry run prints `cloud: id= name= repo= status= updated=` and the link, then `cleanup: count`.

`--all-idle` includes every IDLE agent in this repo. `--all-idle --any-repo` includes every IDLE agent the key can see. Leave those flags off for a pile of Warp agents in this repo.

Without the key, the command prints each ended registry cloud agent as `https://cursor.com/agents/<id>` so those agents can be archived in the Cursor UI.

A pile of about 199 idle Shuttles, Bugbot runs, and listeners from an older run is cleared by the two commands above, from a checkout whose origin is this repo. Export `CURSOR_API_KEY`, run the dry run, then run the same command with `--apply`.

Merge and park release that ticket's agents. `/warp-stop` marks every registered agent stopped and runs cleanup. When every ticket is merged or parked, the parent runs cleanup and exits. Nothing new is spawned while the run is paused or stopped.
