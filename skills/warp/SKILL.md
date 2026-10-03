---
name: warp
description: "Run the Warp master loop on any repo that has a plan. Use when the user says start, stop, pause, resume, scan, or supervise the build."
---

# Warp — master loop

Scan builds the plan. Start dispatches. Stop and pause do not.

## Control

| Command | runState | Dispatch | In-flight |
|---|---|---|---|
| `/warp-scan` | stopped | no | none yet |
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

`ready` returns nothing unless `runState` is `running`. A tick on a stopped or paused beam only reconciles and writes status.

## One running tick

1. `beam.py check`
2. Reconcile in-flight PRs and Jira.
3. Advance gates only with evidence.
4. `beam.py ready` — that list only. No person cap.
5. Claim and spawn a Shuttle per id. Herald posts each claim.
6. `scan.py status` and `beam.py board`.
7. Herald posts the tick digest: done, working, left, next ready.

## Status file

`.warp/STATUS.md` and `.warp/status.json` are the files to open. Done, working, left. Regenerated every tick and on `/warp-status`.

## Notify

`notify: verbose` is the default. Herald posts scan, start, pause, resume, stop, claim, PR opened, Bugbot result, approval wait, merge, alarm, and gate change. See `agents/herald.md`.
