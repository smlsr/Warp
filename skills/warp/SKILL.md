---
name: warp
description: "Run the Warp master loop on any repo that has a plan. Use when the user says start, stop, pause, resume, scan, or supervise the build."
---

# Warp — master loop

Scan builds the plan. Start dispatches. Stop and pause do not.

Plugin hooks do not run on cloud runners. Before this tick, run `python3 <plugin>/scripts/resume_hint.py --root .` and follow it. Do not dispatch from the hint. If it says there is no beam, tell the user to run `/warp-ingest`. At the end of the tick, run `python3 <plugin>/scripts/session_note.py --type session-stop --root .`. When a Shuttle returns, run `python3 <plugin>/scripts/session_note.py --type subagent-stop --root .`, then reconcile that ticket from the beam. Call an MCP tool only when it is in `scripts/mcp_tools.py` (plus `notifyAllow`). For any other tool, `python3 <plugin>/scripts/mcp_allow.py --server SERVER --tool TOOL --root .` must print `allow`. `ask` means do not call it.

## Control

| Command | runState | Dispatch | In-flight |
|---|---|---|---|
| `/warp-scan [folder]` | stopped | no | none yet |
| `/warp-start` | running | yes | claims up to maxAgents |
| `/warp-pause` | paused | no | finish the current step, checkpoint |
| `/warp-resume` | running | yes | reconcile, then claim |
| `/warp-stop` | stopped | no | checkpoint, do not claim again until start |

```bash
python3 <plugin>/scripts/scan.py start --beam .warp/beam.json
python3 <plugin>/scripts/scan.py pause --beam .warp/beam.json --reason "hold"
python3 <plugin>/scripts/scan.py resume --beam .warp/beam.json
python3 <plugin>/scripts/scan.py stop --beam .warp/beam.json --reason "end of day"
```

`scan.py ?` prints these subcommands and their flags. Quote `?` if the shell expands it.

`ready` returns nothing unless `runState` is `running`. A tick on a stopped or paused beam only reconciles and writes status.

## Listener

One `warp-listen` sub-agent for the beam. Not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. `/warp-start` and `/warp-resume` launch it when `inbound.py claim` prints `listener: started`, or when `beam.py watchdog` prints `listener: replace <id>` for a dead listener. `listener: already running` or `listener: alive` means do not launch a second. `/warp-pause` and `/warp-stop` run `inbound.py release`. The listener must not keep reading while paused or stopped. This tick does not read Slack. It launches a listener only for that one `listener: replace` line. Plugin hooks do not run on cloud runners. A dead turn does not notify Warp. Cursor does not restart it.

## Heartbeat and watchdog

`beam.py watchdog` runs on this tick, before `ready()`. `scan.py start` and `scan.py resume` run it too. It does not look at a process table. A Shuttle records `lastSeenAt` and its agent id with `beam.py heartbeat` at claim, at each status change, and at least every 5 minutes while the turn is alive. The listener does the same with `inbound.py heartbeat` on every read. Both stay inside `staleMinutes` (default 15).

A worker is dead when `lastSeenAt` is older than `staleMinutes`, or it never heartbeated and the claim or listener start is older than `staleMinutes`. A fresh heartbeat (`alive` or `fresh`) is left alone. `watchdog: skipped` means the run is paused or stopped: do not recover and do not start a replacement.

- `listener: replace <id>`: the stale `running` flag is already cleared and this id owns the slot. Launch exactly one `warp-listen` agent with that id. Do not claim a different id. Herald posts one line: `Listener died. A new one started.`
- `shuttle: replace <id>`: status is `recovering`. Branch, pull request, `jira.startedAt`, and locks stayed. Dispatch exactly one Shuttle for that same ticket. Do not queue a duplicate and do not release the lock. Herald posts one line: `<id> worker died. A new Shuttle started.`
- `shuttle: alarm <id> worker-died`: `maxRecoveries` (default 3) is spent. Do not start another.

A second tick must not launch a second replacement for the same worker. The first write reserved it.

## One running tick

1. `beam.py check`
2. `beam.py watchdog`. Launch the replacements above. Herald posts each `herald:` line once.
3. Reconcile in-flight PRs and Jira.
4. Advance gates only with evidence.
5. `beam.py ready` — that list only. No person cap. A `recovering` ticket is not in this list.
6. Claim and spawn a Shuttle per id. Then `beam.py heartbeat` for that id. Herald posts each claim.
7. `scan.py status` and `beam.py board`.
8. Herald posts the tick digest: done, working, left, next ready.

## Status file

`.warp/STATUS.md` and `.warp/status.json` are the files to open. Done, working, left. Regenerated every tick and on `/warp-status`.

## Notify

`notify: verbose` is the default. Herald posts scan, start, pause, resume, stop, claim, PR opened, Bugbot result, approval wait, merge, alarm, and gate change. See `agents/herald.md`.
