---
name: warp-start
description: Start Warp dispatch after a scan
---

On a fresh checkout, fetch origin and load the beam from main before anything else. Do not start from an empty beam if main has one. Close the window, open a new one, and `/warp-start`: state comes from main plus each ticket folder.

```bash
python3 <plugin>/scripts/scan.py start --beam .warp/beam.json
```

`scan.py start` prints `beam: fetched origin/<base>` and, when the local beam is missing or empty, `beam: loaded from origin/<base>`. Then it runs the watchdog, which fetches each in-flight ticket branch and patches the live beam from `.warp/tickets/<id>/`. It does not copy that branch's beam. Then it dispatches.

Set runState to running with that command, tell Herald, then start the one channel listener. Refuse if there is no beam on the checkout and main has none. If start prints `refuse:` and names missing MCP tools, do not start and do not claim. Post that list (Herald already has it). If it warns that there is no cloud environment (`.cursor/environment.json` or `cloudSnapshot`) while `subagentVm` is true, do not start and do not claim. `scan.py start --force` starts anyway and still warns. Cloud subagents use the MCP servers at cursor.com/agents, not this session. A local runner does not block. `scan.py start` prints the resume hint and runs `beam.py watchdog`. Follow it. Do not dispatch when it says the beam is missing or paused. `watchdog: skipped` means do not recover Shuttles and do not start a listener. A later claim runs the same check unless this start was `--force`.

One listener for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. The listener is a Subagent of the parent. It shares this session and this checkout. It is not a separate Agent. Do not start a separate Agent. Start one subagent per ticket in its own worktree, in parallel up to maxAgents. One listener Subagent per parent turn. If this turn ends, the subagent ends with it. The next tick starts one again.

Pick one `--turn` id for this parent turn and keep it. Claim, then start the subagent only when the reply is `listener: started`.

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <new-id> --turn <turn>
```

`listener: started <id>` means you claimed the slot. Start exactly one `warp-listen` Subagent with that id. It reads Slack and Teams, acknowledges commands, and returns them. You apply them. It does not merge and does not implement tickets.

`listener: already running <id>` means this turn already started the listener. Do not start a second. A running flag left by the previous turn does not block this tick: a new `--turn` takes the slot (`listener: started`).

`listener: subagent` from the watchdog is the same instruction. It does not reserve an id. Do not launch a separate Agent because of it.

For each `shuttle: replace <id>` line, start exactly one subagent for that ticket (`checkout.py launch`, then the prompt it prints). Reuse the surviving worktree and branch. Keep its pull request, `jira.startedAt`, and locks. Do not queue a duplicate. `shuttle: alarm <id> worker-died` means the recovery cap is reached. Do not start another. Herald posts `<id> worker died. A new Shuttle started.` once. A failed subagent result raises `worker-died` without waiting for the heartbeat.

Then run one Warp tick. That tick runs the watchdog again. A second claim with this same `--turn` must not start a second listener. Plugin hooks do not run on cloud runners. The listener must not keep reading while paused or stopped. A dead turn does not notify Warp. Cursor does not restart it.

Do not end the turn while `orchestrator.py parent-exit` prints `parent: stay`. Each pass starts one listener, reads `warp:` commands, fetches `.warp/tickets/<id>/`, patches the beam, dispatches, pushes the beam, and passes again. `parent: exit` is paused, stopped, or nothing claimed, nothing queued, and nothing awaiting checks. Then `scripts/session_note.py --type session-stop` clears the listener flag and commits the live beam, journal, and board onto main, then pushes when origin exists. `.warp/config.yaml` is not committed. Tokens, cost, API keys, and webhook URLs are stripped. Pause and stop still sync state, and then the listener may stop.
