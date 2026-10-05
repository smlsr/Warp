# Runbook

## Pause overnight but keep the board

`/warp-pause` with a reason. In-flight Shuttles finish the current step and checkpoint. The HTML board stays valid. `/warp-resume` reconciles, then ticks.

## A PR will not go green

After `maxFixAttempts` (default 3) Reed sets `alarm` / `bugbot-failed` and Herald posts. Warp will not retry it. Reply `warp:retry <id>` after adding a note on the Jira issue. There is no `warp:hold` command. `/warp-pause` stops new claims.

## L/XL waiting on you

Herald posts the pull request, or the local branch when `pushMerge` is false, and `warp:proceed <id>`. An approval on the provider (GitHub review or Bitbucket APPROVED) is enough. The chat command is the override, and it is the only signal in local-only mode. Jira is at QA Ready while this waits.

Do not proceed a red PR. Reed will refuse.

## Gate red

Fix on the member branch (G0 is L-01, M-01, D-01). Dependents stay out of `ready()` until `beam.py gate --status green --evidence "..."`. A red critical-path gate is the one case to pause the program: later agents will only pile up lock-free work that cannot ship.

## Jira status did not move

`/warp-jira-check` runs `jira_sync.py verify`. For each ticket it prints the key, the beam status, `startedAt`, `qaReadyAt`, `doneAt`, the comment ids on file, and what is missing.

A blank `jiraKey` means the plan never had one and the id, summary, and branch did not contain a key like `ABC-123`. `catchup --write` stores an inferred key when there is one. An already-merged auto-merge ticket is asked for Done, not for In Progress.

The move itself is not done by the script. The agent that sees `jira: MUST DO` has to call `getTransitionsForJiraIssue` and `transitionJiraIssue` on the `jiraMcp` server, then `record`. If that server is not connected, the same failure is in `.warp/outbox.md` and in the Herald payload.

An existing `.warp/config.yaml` is not rewritten when the plugin updates. Missing keys still default correctly. Re-run `/warp-init` to append them. Check that `jiraTransition` is not `false`, that `jiraMcp` is the server name you connected, and that `jiraInProgressStatus`, `jiraQaReadyStatus`, and `jiraDoneStatus` match the names in your workflow.

## Stuck agent

Reconcile marks `stuck` when `updatedAt` is older than `stuckAfterMinutes` (default 90) and the ticket is not `awaiting_approval`. Reattach by spawning a Shuttle on the same id; it must reuse the branch.

## Cap change

Edit `.warp/config.yaml` `maxAgents`. Next tick picks it up if the skill re-reads yaml. Also set the copy inside the beam if a tick reads only the beam: re-ingest is wrong for a live run; edit `beam.config.maxAgents` with a journal note, or set it in both places.

## Reinstall or reset

`/warp-init` is idempotent. It never overwrites `.warp/config.yaml` values or existing plugin files, so it will not upgrade an installed copy. It does append config keys the file is missing, and it records the installed version in `.warp/version`. When that copy is older than the plugin you ran, it prints `plugin is vOLD, repo copy is vNEW: run /warp-uninstall then /warp-init`. To start clean, copy `.warp/` aside if you want the journal, run `/warp-stop`, then `/warp-uninstall` and confirm, reload Cursor, and run `/warp-init`. See the README upgrade section.

## Scan one folder

`/warp-scan <folder>` limits the search to that folder. If the name matches several folders the scan stops and lists them; re-run with the full path. The beam and `scan.json` record the folder.

## Rebuild the graph

Only when `schedule.json` is regenerated. Copy `.warp/` aside first. Ingest overwrites live status.
