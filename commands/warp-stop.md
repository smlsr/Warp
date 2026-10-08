---
name: warp-stop
description: Stop Warp until an explicit start. End every agent
---

Set runState to stopped with `scripts/scan.py stop`.

```bash
python3 <plugin>/scripts/scan.py stop --beam .warp/beam.json --reason "end of day"
```

Stop is the same teardown as `/warp-pause`, then the completion report. That report is the one difference.

| | `/warp-pause` | `/warp-stop` |
|---|---|---|
| `runState` | `paused` | `stopped` |
| Every agent in `.warp/agents.json` ended, every Shuttle slot freed | yes | yes |
| This run's cloud agents cancelled and archived (with `CURSOR_API_KEY`) | yes | yes |
| Branches, worktrees, pull requests, locks, `jira.startedAt` | kept | kept |
| Completion report `.warp/warp-complete.html` and the Herald totals | no | yes |
| `stoppedAt` recorded as the end of the run | no | yes |
| Use it when | you will carry on: overnight, a meeting, a bad base branch | the run is over, or you want the report |
| To carry on | `/warp-start` or `/warp-resume`. They are the same command | `/warp-start` or `/warp-resume`. They are the same command |

Its steps:

1. `runState` becomes `stopped` and a `session-stop` journal line is appended.
2. Every agent in `.warp/agents.json` is ended (`stop: <id>`) and marked halted, every Shuttle slot is freed, and the listener's poll is closed. The line is `halt: stopped agents=<n>`. `inbound.py release` sets `listener.state` to `stopped`.
3. `scripts/update_state.py` syncs: it loads origin/main, patches `.warp/tickets/<id>/`, puts this stop back on top, and pushes the beam and the registry to main before the command returns. A Shuttle on its own VM reads the stop from there.
4. With `CURSOR_API_KEY` in the environment, every cloud agent in the registry, and every agent in this repo whose name carries this run's `[warp:<instance>]` tag, has its run cancelled and is archived (`cleanup: cancelled <id>`, `cleanup: archived <id>`). The agent running this command and this run's parent are skipped. Without the key the command prints `cleanup: link https://cursor.com/agents/<id>` for each one: archive those in the Cursor UI.
5. `reportOnComplete` writes `.warp/warp-complete.html`.

Then tell Herald, run `orchestrator.py parent-exit --beam .warp/beam.json`, which prints `parent: exit`, and end the turn. Start nothing. Do not wait for the agents: each one reads `reap: exit stopped` at its next check and returns `result: <id> stopped stopped`. A subagent that shares this session cannot be cancelled from outside. It exits at that check.

A later tick must not dispatch until `/warp-start` or `/warp-resume`, which are the same command. It launches new Shuttles on the surviving branches, unfinished work first. Nothing from before the stop is resumed. `cleanup: cut short` means the teardown hit its time limit: run `/warp-cleanup`. The listener must not keep reading while paused or stopped. Plugin hooks do not run on cloud runners. Nothing here depends on one.
