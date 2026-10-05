# Runbook

## Pause overnight but keep the board

`/warp-pause` with a reason. In-flight Shuttles finish the current step and checkpoint. The HTML board stays valid. `/warp-resume` reconciles, then ticks.

## A PR will not go green

After `maxFixAttempts` (default 3) Reed sets `alarm` / `bugbot-failed` and Herald posts. Warp will not retry it. Reply `warp:retry <id>` after adding a note on the Jira issue, or `warp:hold <id>` to leave it out of the ready set.

## L/XL waiting on you

Herald posts the pull request, or the local branch when `pushMerge` is false, and `warp:proceed <id>`. An approval on the provider (GitHub review or Bitbucket APPROVED) is enough. The chat command is the override, and it is the only signal in local-only mode. Jira is at QA Ready while this waits.

Do not proceed a red PR. Reed will refuse.

## Gate red

Fix on the member branch (G0 is L-01, M-01, D-01). Dependents stay out of `ready()` until `beam.py gate --status green --evidence "..."`. A red critical-path gate is the one case to pause the program: later agents will only pile up lock-free work that cannot ship.

## Stuck agent

Reconcile marks `stuck` when `updatedAt` is older than `stuckAfterMinutes` (default 90) and the ticket is not `awaiting_approval`. Reattach by spawning a Shuttle on the same id; it must reuse the branch.

## Cap change

Edit `.warp/config.yaml` `maxAgents`. Next tick picks it up if the skill re-reads yaml. Also set the copy inside the beam if a tick reads only the beam: re-ingest is wrong for a live run; edit `beam.config.maxAgents` with a journal note, or set it in both places.

## Reinstall or reset

`/warp-init` is idempotent. It never overwrites `.warp/config.yaml` or existing plugin files, so it will not upgrade an installed copy. To start clean, copy `.warp/` aside if you want the journal, run `/warp-stop`, then `/warp-uninstall` and confirm, reload Cursor, and run `/warp-init`.

## Scan one folder

`/warp-scan <folder>` limits the search to that folder. If the name matches several folders the scan stops and lists them; re-run with the full path. The beam and `scan.json` record the folder.

## Rebuild the graph

Only when `schedule.json` is regenerated. Copy `.warp/` aside first. Ingest overwrites live status.
