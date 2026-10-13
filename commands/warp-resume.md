---
name: warp-resume
description: Resume Warp dispatch from the beam
---

`/warp-resume` and `/warp-start` are the same command. `commands/warp-start.md` is the full text. It works from a paused run, a stopped run, and a run that is already running, and it says which (`run: was paused (<reason>). Continuing.`). It runs the same prompt gate as start, and `--force` means the same. It takes the same gate range (`--gates=G0-G3`), `--takeover`, and `--now`.

Resume the beam with `scripts/scan.py resume`, which fetches origin, loads the beam from main when the local one is missing or empty, prints the resume hint, and runs `beam.py watchdog`. Do not start from an empty beam if main has one. Close the window, open a new one, and `/warp-start` or `/warp-resume`: state comes from main plus each ticket folder. The watchdog fetches each in-flight ticket branch and patches from `.warp/tickets/<id>/`. Reconcile, then run Warp passes. Follow the hint. Do not dispatch when the beam is still paused. `watchdog: skipped` means do not recover Shuttles and do not start a listener.

```bash
python3 <plugin>/scripts/scan.py resume --beam .warp/beam.json
```

`beam: loaded from origin/<base>` means the fresh checkout took main's beam. `beam: kept local` means this checkout already had tickets. Either way, patch in-flight work from the ticket folders before dispatch.

It prints `unfinished: <n> open. ...` and launches that work first: open pull requests that need a fix, then tickets whose Shuttle was halted or died, then new tickets, all under `maxAgents`. `maxInProgress` holds only new tickets.

It also puts every ticket in alarm back on a path that can finish, so no one has to post `warp:retry`. Each one prints a `heal:` line:

- `heal: <id> lock-escape cleared, back to review on <url>.` Every escaped path is already inside the ticket's locks (a repair widened them, or the alarm was left over from an earlier event). Paths still outside the locks are added when no ticket in flight holds them (`Added <paths> to its locks.`). Without a pull request the ticket is queued and its next claim reuses the branch. `... its <step> Shuttle is still out.` means only the alarm went.
- `heal: <id> lock-escape kept: <paths> held by <id>.` The repair count starts again, and the parent's repair widens the locks when the holder is done.
- `heal: <id> retried: was alarm (<reason>), ...` Any other alarm (worker-died, stuck, spawn-cap, ci-red, bugbot-failed, failed) is retried the way `warp:retry` does it: an open pull request goes to `fix` on its branch, otherwise `queued`, and the counts start again (old ones on `retryLog`).
- `heal: <id> left in alarm (gate-red).` The gate's own check is red. Fix that; the gate reruns it.

Parked tickets stay parked. `warp:retry` still works on them. `python3 <plugin>/scripts/heal.py --beam .warp/beam.json --dry-run` shows what a resume would do.

It also prints `parent: session <id>`. Keep that id. You are now the parent of this run: the only agent that starts another agent, and the only agent that waits. An earlier parent, here or in another window, ends its turn when its `parent-exit --session` prints `parent: exit superseded`.

Nothing from before a pause or a stop is resumed. `/warp-pause` and `/warp-stop` ended every agent. A ticket that had a Shuttle out prints `start <id> ... step=restart` and `fix: <id> resume after halt`: launch one new Shuttle on that ticket's surviving worktree and branch. It keeps the pull request, `jira.startedAt`, and locks. It is not a recovery and no one is told a worker died.

One listener for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. The listener is a Subagent of the parent. It shares this session and this checkout. It is not a separate Agent. Do not start a separate Agent for it. `subagentVm` does not apply. You do not claim it. When `orchestrator.py supervise` prints `listener: poll` and the prompt (`LISTEN`), start exactly one `warp-listen` Subagent in the background with those lines and do not wait for it. `listener: hold reason=live` and `listener: hold reason=await-return` mean do not start a second. `listener: idle` and `listener: none` mean do not start one. It does not merge.

Start one subagent per ticket in its own worktree, in parallel up to maxAgents. For each `start <id>` line run `checkout.py launch --id <id>` and start one subagent, named as the `name: [warp:<instance>] <ticket> <step>` line says, with the lines below `prompt: pass every line below this one to the subagent, and nothing above it`. `/warp-resume` reuses surviving worktrees and branches. For each `shuttle: replace <id>` line, start exactly one subagent for that same ticket (`checkout.py launch`, then the prompt). Do not queue a duplicate. A second tick must not launch another Shuttle for that id. `shuttle: alarm <id> worker-died` does not start a Shuttle. A failed subagent result raises `worker-died`. `spawn: closed <id>` means no prompt was printed: start nothing.

`scan.py resume` prints the open-work list, alarms and stalls first, including `in progress N/maxInProgress`, or `holding new launches (N/maxInProgress), N fix workers running` when that cap is full, and `live shuttles N/cap` plus dead tickets waiting for a replacement. It releases Shuttles whose heartbeat is stale, then prints fix `start` lines and `shuttle: replace` lines up to the live caps: open pull requests that need a fix first, then dead Shuttle replacements. Launch those. `maxInProgress` does not hold them. `holding new launches` does not stop a fix. `supervise` adopts a ticket that already has an open pull request into `reviewing` or `fixing`. Do not relaunch that ticket from scratch.

End every pass with the one wait:

```bash
python3 <plugin>/scripts/orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>
```

Do not end the turn while it prints `parent: stay`. That is while any ticket is not merged or parked. Do not sleep, set a timer, or subscribe to a pull request anywhere else. `parent: exit` is when every ticket is merged or parked, or the run is paused or stopped. Then run `session_note.py --type session-stop`, which commits the live beam onto main and pushes it, and start nothing.

Plugin hooks do not run on cloud runners. Every check above is a script you run. The listener must not keep reading while paused or stopped. A dead turn does not notify Warp. Cursor does not restart it. You do, with this command.
