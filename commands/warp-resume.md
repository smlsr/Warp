---
name: warp-resume
description: Resume Warp dispatch from the beam
---

Resume the beam with `scripts/scan.py resume`, which fetches origin, loads the beam from main when the local one is missing or empty, prints the resume hint, and runs `beam.py watchdog`. Do not start from an empty beam if main has one. Close the window, open a new one, and `/warp-start` or `/warp-resume`: state comes from main plus each ticket folder. The watchdog fetches each in-flight ticket branch and patches from `.warp/tickets/<id>/`. Reconcile, then start the one channel listener and run one Warp tick. Follow the hint. Do not dispatch when the beam is still paused. `watchdog: skipped` means do not recover Shuttles and do not start a listener.

```bash
python3 <plugin>/scripts/scan.py resume --beam .warp/beam.json
```

`beam: loaded from origin/<base>` means the fresh checkout took main's beam. `beam: kept local` means this checkout already had tickets. Either way, patch in-flight work from the ticket folders before dispatch.

One listener for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. The listener is a Subagent of the parent. It shares this session and this checkout. It is not a separate Agent. Do not start a separate Agent. Start one subagent per ticket in its own worktree, in parallel up to maxAgents. `/warp-resume` reuses surviving worktrees and branches.

Pick one `--turn` for this parent turn.

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <new-id> --turn <turn>
```

`listener: started <id>`: start exactly one `warp-listen` Subagent with that id. It loops on the channel, posts acks, and returns only for pause, stop, or `recycle`. You apply queued commands with `inbound.py apply-pending`. It does not merge.

`listener: already running <id>`: this turn already started the listener. Do not start a second. A new `--turn` takes a slot the previous turn left running.

For each `shuttle: replace <id>` line, start exactly one subagent for that same ticket (`checkout.py launch`, then the prompt). Reuse the surviving worktree and branch. Do not queue a duplicate. A second tick must not launch another Shuttle for that id. `shuttle: alarm <id> worker-died` does not start a Shuttle. A failed subagent result raises `worker-died`.

Plugin hooks do not run on cloud runners. The listener must not keep reading while paused or stopped. A dead turn does not notify Warp. Cursor does not restart it. You restart it from `orchestrator.py supervise` when it returns or `.warp/listener.json` is stale, unless the loop is paused or stopped. `scan.py resume` prints the open-work list, alarms and stalls first, including `in progress N/maxInProgress`, or `holding new launches (N/maxInProgress), N fix workers running` when that cap is full, and `live shuttles N/cap` plus dead tickets waiting for a replacement. It releases Shuttles whose heartbeat is stale, then prints fix `start` lines and `shuttle: replace` lines up to the live caps: open pull requests that need a fix first, then dead Shuttle replacements. Launch those. `maxInProgress` does not hold them. `holding new launches` does not stop a fix. `supervise` adopts a ticket that already has an open pull request into `reviewing` or `fixing`. Do not relaunch that ticket from scratch. Exactly one listener. Do not end the turn while `orchestrator.py parent-exit` prints `parent: stay`. That is while any ticket is not merged or parked. `parent: exit` is when every ticket is merged or parked, or the run is paused or stopped, and runs `session_note.py --type session-stop`, which commits the live beam onto main and pushes it. Pause and stop still sync state, and then the listener may stop.
