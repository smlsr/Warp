# State

`.warp/` is the checkpoint. Commit it. Do not commit `.env` or MCP tokens; Warp never writes those.

```
.warp/config.yaml     caps, model, messenger, connector names
.warp/beam.json       tickets, gates, agents, metrics
.warp/journal.jsonl   append-only events
.warp/BOARD.md        generated
.warp/board.html      generated
.warp/outbox.md       Herald fallback if a messenger is down
```

## Ticket status

`queued` → `claimed` → `planning` → `coding` → `review` → (`fix` → `review`)* → `awaiting_approval`? → `merging` → `merged` → `done`

Side exits: `blocked` (human hold), `alarm` (needs a human), `skipped`.

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
