---
name: warp-resume
description: Resume Warp dispatch from the beam
---

Resume the beam with `scripts/scan.py resume`, which fetches origin, loads the beam from main when the local one is missing or empty, prints the resume hint, and runs `beam.py watchdog`. Do not start from an empty beam if main has one. Close the window, open a new one, and `/warp-start` or `/warp-resume`: state comes from main plus each ticket folder. The watchdog fetches each in-flight ticket branch and patches from `.warp/tickets/<id>/`. Reconcile, then start the one channel listener and run one Warp tick. Follow the hint. Do not dispatch when the beam is still paused. `watchdog: skipped` means do not recover Shuttles and do not start a listener.

```bash
python3 <plugin>/scripts/scan.py resume --beam .warp/beam.json
```

`beam: loaded from origin/<base>` means the fresh checkout took main's beam. `beam: kept local` means this checkout already had tickets. Either way, patch in-flight work from the ticket folders before dispatch.

One listener for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. The listener is a Subagent of the parent. It shares this session and this checkout. It is not a separate Agent. Do not start a separate Agent. Each ticket is a new Agent.

Pick one `--turn` for this parent turn.

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <new-id> --turn <turn>
```

`listener: started <id>`: start exactly one `warp-listen` Subagent with that id. It reads, acknowledges, and returns `warp:` commands. You apply them with `inbound.py apply`. It does not merge.

`listener: already running <id>`: this turn already started the listener. Do not start a second. A new `--turn` takes a slot the previous turn left running.

For each `shuttle: replace <id>` line, start exactly one new Agent for that same ticket (`checkout.py launch`, then the Agent on that branch). Do not start a Subagent. Do not use the Task tool. Do not start it on a fresh clone of main. Keep the branch. Do not queue a duplicate. A second tick must not launch another Shuttle for that id. `shuttle: alarm <id> worker-died` does not start a Shuttle.

Plugin hooks do not run on cloud runners. The listener must not keep reading while paused or stopped. A dead turn does not notify Warp. Cursor does not restart it. The end of the tick runs `session_note.py --type session-stop`, which commits the live beam onto main and pushes it.
