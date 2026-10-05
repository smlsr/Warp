# State

`.warp/` is the checkpoint. `/warp-init` gitignores it. Warp does not commit it. Copy the folder aside if you need to keep it. Do not commit `.env` or MCP tokens; Warp never writes those.

```
.warp/config.yaml     caps, model, messenger, connector names
.warp/beam.json       tickets, gates, agents, metrics
.warp/journal.jsonl   append-only events
.warp/BOARD.md        generated
.warp/board.html      generated
.warp/outbox.md       Herald fallback if a messenger is down or no channel is set. Also a failed Jira move.
.warp/notify-post.json  Last Herald payload (init, scan, or a failed Jira move) and where to post it
.warp/jira-todo.json  Actions the current turn must do: transitions and comment bodies
.warp/version        One line, the plugin version init recorded from .cursor/plugins/warp
```

## Ticket status

`queued` → `claimed` → `planning` → `coding` → `review` → (`fix` → `review`)* → `awaiting_approval`? → `merging` → `merged` → `done`

Side exits: `blocked` (human hold), `alarm` (needs a human), `skipped`.

Jira keys: `jiraKey` is the real issue key (`WAR-1`), never the plan id (`WV-01`), unless that id's project prefix matches `jiraProject` or `jiraKeyPrefixes`. It is set from a Jira export `key`, a `schedule.json` `jiraKey`, a markdown `Jira:` line, `Jira Key` column, or `[WAR-1]` heading, `.warp/jira-map.json`, `jiraKeyMap`, or `beam.py set --jira`. `jiraMapping` is `mapped` or `needs mapping`. `jiraKeySource` is `plan`, `export`, `map`, `manual`, `external`, or `inferred`. `external` is an exact single match on `jiraExternalIdField` during scan or claim. A rescan keeps a manual key and reapplies the map file. `jira.startedAt` and `jira.previousStatus` are set when Warp moves an issue to In Progress, `jira.qaReadyAt` when a manual-path ticket reaches QA Ready, `jira.doneAt` when an auto-merge ticket reaches Done, and `jira.lastSync` holds the last result. `jira.comments` and `pr.comments` map an event (`claim`, `pr-opened`, `qa-ready`, `merged`, `bugbot`, `ci`, `alarm`, `blocked`) to `{at, id}` so a retry does not post it again. `autoMerge` on the ticket, set at ingest from size and `autoMergeSizes`, chooses QA Ready or Done. `/warp-jira-check` (`jira_sync.py verify`) prints, per ticket, what should have happened and which of these fields are empty.

`merged` and `done` both unblock dependents. `done` means Jira was transitioned. Reed sets `merged` then `done`. If Jira fails, the ticket stays `merged` and the next tick retries the transition.

## Fields that matter

- `deps`, `locks`, `gate`, `critical`, `rankDays` — from the schedule, not edited live.
- `autoMerge` — true for S/M.
- `pr.bugbot`, `pr.bugbotEvidence`, `pr.ci`, `pr.approvedAt`, `pr.url`.
- `attempts`, `tokens`, `minutes`, `alarm`, `jiraKey`.

## Restart

1. Session start hook prints done/total and pause flag.
2. Warp reconciles PRs before `ready()`.
3. A Shuttle whose branch exists continues that branch.
4. A claim with no branch and a dead agent goes back to `queued` on reconcile if `updatedAt` is older than `stuckAfterMinutes`.

Pause sets `paused: true`. `ready()` returns nothing. In-flight status is kept.

## Invariants `beam.py check` enforces

- Every dep exists.
- No dependency cycle.
- No two active tickets with overlapping locks.

A failed check aborts the tick.
