---
name: warp-pause
description: Pause Warp. End every agent, keep the work, resume later
---

Pause the beam with `scripts/scan.py pause` or `scripts/beam.py pause` and the user's reason.

```bash
python3 <plugin>/scripts/scan.py pause --beam .warp/beam.json --reason "hold"
```

Pause and stop are the same teardown. The one difference is the completion report: stop writes it, pause does not.

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

Pause is a full teardown of agents, not of work. Its steps:

1. `runState` becomes `paused` and a `session-stop` journal line is appended.
2. Every agent in `.warp/agents.json` is ended (`stop: <id>`) and marked halted, every Shuttle slot is freed, and the listener's poll is closed. The line is `halt: paused agents=<n>`. `inbound.py release` sets `listener.state` to `stopped`.
3. `scripts/update_state.py` syncs: it loads origin/main, patches `.warp/tickets/<id>/`, puts this pause back on top, and pushes the beam and the registry to main before the command returns. A Shuttle on its own VM reads the pause from there.
4. With `CURSOR_API_KEY` in the environment, every cloud agent in the registry, and every agent in this repo whose name carries this run's `[warp:<instance>]` tag, has its run cancelled and is archived (`cleanup: cancelled <id>`, `cleanup: archived <id>`). The agent running this command and this run's parent are skipped. Without the key the command prints `cleanup: link https://cursor.com/agents/<id>` for each one: archive those in the Cursor UI.

Then tell Herald, run `orchestrator.py parent-exit --beam .warp/beam.json`, which prints `parent: exit`, and end the turn. Do not start a listener, a Shuttle, or anything else on pause. Do not wait for the agents: each one reads `reap: exit paused` at its next check and returns `result: <id> stopped paused agent=<agent>`. A subagent that shares this session cannot be cancelled from outside. It exits at that check.

Branches, worktrees, pull requests, locks, and `jira.startedAt` stay. `/warp-resume` or `/warp-start`, which are the same command, starts a new Shuttle on the same branch for each ticket that had one out (`fix: <id> resume after halt`), before any new ticket. That is not a recovery, and nothing from before the pause is resumed. Work a cancelled VM had not pushed stays in that archived agent.

`cleanup: cut short` means the teardown hit its time limit: run `/warp-cleanup`. The listener must not keep reading while paused or stopped. Plugin hooks do not run on cloud runners. Nothing here depends on one.
