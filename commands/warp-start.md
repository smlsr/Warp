---
name: warp-start
description: Start Warp dispatch after a scan
---

`/warp-start` and `/warp-resume` are the same command. Use either. It works from a stopped run, a paused run, and a run that is already running, and it says which: `run: was stopped. Starting.`, `run: was paused (<reason>). Continuing.`, or `run: was already running. This session takes it over.` There is nothing to refuse and nothing to choose.

On a fresh checkout, fetch origin and load the beam from main before anything else. Do not start from an empty beam if main has one. Close the window, open a new one, and `/warp-start`: state comes from main plus each ticket folder.

```bash
python3 <plugin>/scripts/scan.py start --beam .warp/beam.json
```

`scan.py start` prints `beam: fetched origin/<base>` and, when the local beam is missing or empty, `beam: loaded from origin/<base>`. Then it runs the watchdog, which fetches each in-flight ticket branch and patches the live beam from `.warp/tickets/<id>/`. It does not copy that branch's beam. Then it dispatches.

It prints `instance: warp:<instance> host=<host> machine=<id>`. The instance is six characters that name this Warp run. It is set once, kept on the beam, and shown in every Slack and Teams header, in `/warp-status`, and in `/warp-version`. Every agent this run starts carries it in its name, so this run's agents are told apart from every other agent and from another Warp run's.

It prints `unfinished: <n> open. <n> need a fix, <n> need a Shuttle, <n> have a Shuttle out, <n> wait on review or checks.` That is the check for work an earlier session left. Unfinished work starts first: open pull requests that need a fix, then tickets whose Shuttle was halted or died, then new tickets from the ready queue. There are two caps. `maxAgents` is how many agents run at once, of any kind, so a new ticket starts only in a slot unfinished work did not need. `maxInProgress` is how many tickets are open at once, and it holds only new tickets. `unfinished: none` means the run starts from the ready queue.

It also prints `parent: session <id>`. Keep that id. You are the parent of this run: the only agent that starts another agent, and the only agent that waits. One parent per run. A later `/warp-start` or `/warp-resume`, here or in another window, takes the run over, and the earlier parent ends its turn when `parent-exit --session <id>` prints `parent: exit superseded`.

Set runState to running with that command and tell Herald. Refuse if there is no beam on the checkout and main has none. If start prints `refuse:` and names missing MCP tools, do not start. Post that list (Herald already has it). If it warns that there is no cloud environment (`.cursor/environment.json` or `cloudSnapshot`) while effective `subagentVm` is true, do not start. `scan.py start --force` (or `scan.py resume --force`) starts anyway and still warns. `runner: local` forces `subagentVm` false and `launch` to `worktree`, skips that check, and prints one note. Start, `/warp-version`, and status print the effective values. Cloud subagents use the MCP servers at cursor.com/agents, not this session. A local runner does not block. `scan.py start` prints the resume hint, the open-work list (alarms and stalls first, then `in progress N/maxInProgress`, or `holding new launches (N/maxInProgress), N fix workers running` when that cap is full, then `live shuttles N/cap` and any dead tickets waiting for a replacement, then a count of each state), and runs `beam.py watchdog`. When no Shuttle is out it prints `start <id> ... step=fix` lines, up to `maxAgents`. Launch those. `holding new launches` does not stop a fix. Follow it. Do not dispatch when it says the beam is missing or paused. `watchdog: skipped` means do not recover Shuttles and do not start a listener.

Nothing from before a pause or a stop is resumed. A ticket that had a Shuttle out when the run halted prints `start <id> ... step=restart` and `fix: <id> resume after halt`: launch one new Shuttle on that ticket's surviving branch. It is not a recovery.

## The listener

One listener for the beam, not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. The listener is a Subagent of the parent. It shares this session and this checkout. It is not a separate Agent. Do not start a separate Agent for it. `subagentVm` does not apply. It is one background subagent: it holds a generation lease, reads Slack and Teams, and `inbound.py wait` sleeps in Python between reads.

You do not claim a listener. Start prints `listener: not started. The next supervise issues it.` `orchestrator.py supervise` prints `listener: poll` when no holder is live and the return stamp is in, or when none has started, followed by the prompt (`LISTEN`). Start exactly one `warp-listen` Subagent in the background with those lines and do not wait for it. It does not merge and does not implement tickets. `listener: hold reason=live` means a holder already has the lease: do not start a second. `listener: hold reason=await-return` means the lease was bumped and the return stamp is not in yet: do not start a second. A stale heartbeat does not start one. `listener: idle` means paused, stopped, or finished. `listener: none` means no `slackChannel` or `teamsChannel` is set, so no listener is started at all. When `listenerModel` is unset the prompt says `model: inherit`. When it is set the prompt says `model: <slug>`.

## Shuttles

Start one subagent per ticket in its own worktree, in parallel up to maxAgents. For each `start <id>` line, run `checkout.py launch --id <id>`. Lines above `prompt: pass every line below this one to the subagent, and nothing above it` are for you. One of them is `name: [warp:<instance>] <ticket> <step>`. Give the subagent exactly that name: it is the tag that tells Warp's agents from every other agent. Start one subagent with the lines below the mark and nothing else. Run them in the background so this pass goes on. `resume <id>` continues the Shuttle that returned: send it the follow-up, do not start a second subagent. `spawn: closed <id>` means the launch printed no prompt: start nothing.

A `shuttle: release <id>` line means that slot is free. Do not launch it unless a later `shuttle: replace` or `start` line names it. For each `shuttle: replace <id>` line, start exactly one subagent for that ticket (`checkout.py launch`, then the prompt it prints). Launch the fix `start` lines before the replacements. `checkout.py launch` refuses when live Shuttles are already at the cap and prints `live N/cap` plus the ids that hold the slots. Do not keep launching past that refusal. Reuse the surviving worktree and branch. Keep its pull request, `jira.startedAt`, and locks. Do not queue a duplicate. `shuttle: alarm <id> worker-died` means the recovery cap is reached. Do not start another. Herald posts `<id> worker died. A new Shuttle started.` once. A failed subagent result raises `worker-died` without waiting for the heartbeat.

## The loop

Then run Warp passes in this turn. Each pass runs `inbound.py apply-pending` and `orchestrator.py supervise`, reads `.warp/tickets/<id>/`, patches the beam, dispatches, pushes the beam, and ends with the one wait:

```bash
python3 <plugin>/scripts/orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>
```

It blocks until a ticket folder changes, the inbound queue changes, the listener writes its return stamp, the run halts, or `pollSeconds` pass, and prints `pass: ticket <id>`, `pass: command`, `pass: return`, `pass: halt`, or `pass: timeout`. `pollSeconds` is that wait cap, not the channel-read interval. Do not sleep, set a timer, or subscribe to a pull request anywhere else. `parent: stay` means run the next pass now. Do not end the turn while it prints `parent: stay`. That stays while any ticket is not merged or parked. `supervise` adopts a ticket that already has an open pull request into `reviewing` or `fixing`. Do not relaunch that ticket from scratch. `parent: exit` is when every ticket is merged or parked, or the run is paused or stopped. Then `scripts/session_note.py --type session-stop` commits the live beam, journal, and board onto main, then pushes when origin exists. Start nothing after it. `.warp/config.yaml` is not committed. Tokens, cost, API keys, and webhook URLs are stripped.

Plugin hooks do not run on cloud runners. Every check above is a script you run. The listener must not keep reading while paused or stopped. A dead turn does not notify Warp. Cursor does not restart it. If this turn dies, run `/warp-resume`.
