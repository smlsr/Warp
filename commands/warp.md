---
name: warp
description: Start or tick the Warp master loop
---

Run the `warp` skill for one tick. If the user said "start", loop ticks until paused, capped with nothing ready, or they say stop. Re-read the beam every tick. Do not implement tickets in this command. Before the tick, run `scripts/resume_hint.py` and follow it. At the end of the tick, run `scripts/session_note.py --type session-stop`. That releases the listener Subagent and commits the live beam, journal, and board onto main, then pushes when origin exists. `.warp/config.yaml` is not committed. Plugin hooks do not run on cloud runners.

The listener is a Subagent of this parent turn. One per turn, not one per ticket. It shares this session and this checkout. It is not a separate Agent. Each ticket is a new Agent. Claim with one `--turn` id. `listener: started` means start that one Subagent. `listener: already running` means do not start a second. A running flag from the previous turn does not block this tick.

Run `scripts/beam.py watchdog` on this tick, before `ready()`. It fetches each in-flight ticket directory (`.warp/tickets/<id>/`) and patches the live beam before it decides anyone died. It does not copy the branch beam. A fresh Shuttle heartbeat is left alone. `watchdog: skipped` means do not fetch and do not recover. `listener: subagent` means this tick starts one listener Subagent if claim says `started`. Do not start a separate Agent for it. `shuttle: replace <id>` means launch exactly one Shuttle for that same ticket and keep its branch. A second tick must not launch another Shuttle. `/warp-pause` and `/warp-stop` stop the listener. It must not keep reading while paused or stopped.

On every merge, the live beam goes onto main in that merge or in the commit pushed immediately after. The ticket branch's beam is not the one that lands. Close the window, open a new one, `/warp-start`: state comes from main plus ticket folders.
