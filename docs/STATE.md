# State

`.warp/` is the checkpoint. Commit it. Do not commit `.env` or MCP tokens; Warp never writes those.

```
.warp/config.yaml     caps, model, messenger, connector names
.warp/beam.json       tickets, gates, agents, metrics
.warp/journal.jsonl   append-only events
.warp/BOARD.md        generated
.warp/board.html      generated
.warp/outbox.md       Herald fallback if a messenger is down or no channel is set. Also a failed Jira move.
.warp/notify-post.json  Last Herald payload (init, scan, or a failed Jira move) and where to post it
.warp/jira-todo.json  Actions the current turn must do: transitions and comment bodies
```

## Ticket status

`queued` → `claimed` → `planning` → `coding` → `review` → (`fix` → `review`)* → `awaiting_approval`? → `merging` → `merged` → `done`

Side exits: `blocked` (human hold), `alarm` (needs a human), `skipped`.

Jira keys: `jiraKey` is set from a Jira export, a `schedule.json` field, `beam.py set --jira KEY`, or inferred from the ticket id, summary, or branch when that text contains a key like `ABC-123` (two or more letters, then a number; `T-3` is not one). A markdown plan is included. `jira.startedAt` and `jira.previousStatus` are set when Warp moves an issue to In Progress, `jira.qaReadyAt` when a manual-path ticket reaches QA Ready, `jira.doneAt` when an auto-merge ticket reaches Done, and `jira.lastSync` holds the last result. `jira.comments` and `pr.comments` map an event (`claim`, `pr-opened`, `qa-ready`, `merged`, `bugbot`, `ci`, `alarm`, `blocked`) to `{at, id}` so a retry does not post it again. `autoMerge` on the ticket, set at ingest from size and `autoMergeSizes`, chooses QA Ready or Done. `/warp-jira-check` (`jira_sync.py verify`) prints, per ticket, what should have happened and which of these fields are empty.

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
