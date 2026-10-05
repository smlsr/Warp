# Guide

Commands, flags, and config keys are in [COMMANDS.md](COMMANDS.md) and [CONFIG.md](CONFIG.md). Install, upgrade, and troubleshooting are in the README.

## What Warp replaces

The manual plan is 3 people, 6 Cursor windows each, merge windows at 08:30, 13:00, and 17:00. Warp keeps the caps and the gates, and drops the calendar as a blocker. Dispatch is continuous. Windows are digest times unless `respectMergeWindows` is true.

The speed plan's lever still holds: an L ticket that gets a review within the hour does not wait for the next window. Reed polls APPROVED instead of waiting for a human to paste a preamble.

## Graph

Source of truth is `HOS/plan/schedule.json` (deps, locks, gates, size, owner, critical path). `CURSOR_PLAN.md` is the wave narrative. `BUILD_MAP.html` is the human board. Ingest reads the JSON.

Critical path: L-01, M-01, M-02, M-04, M-07, C-01, C-02, C-07, C-10, C-30, C-31, C-32, C-51, C-49, C-60, C-62, C-66.

Sizes map to complexity:

| Size | Hours | Class | Merge |
|---|---|---|---|
| S | 4 | LOW | auto after Bugbot + CI |
| M | 7 | MEDIUM | auto after Bugbot + CI |
| L | 11 | HIGH | wait for APPROVED |
| XL | 16 | CRITICAL | wait for APPROVED |

Counts in the generated graph: S 73, M 109, L 111, XL 73.

## Tick

```
reconcile in-flight → advance gates → ready() → claim → Shuttle → Reed → Herald if needed → board
```

`ready()` refuses a ticket when:

- the beam is paused
- a dep is not merged
- a blocking upstream gate is not green
- a lock path overlaps an active ticket
- `maxAgents` is full. There is no per-person cap.

## Gates

G0–G11 from the schedule. A gate stays pending until every member is merged and someone records check evidence. Red blocks dependents. Members of the gate may still run; that is how G0 gets built.

Do not mark a gate green because the member PRs merged. The schedule's `checks` are the evidence.

## Model

Coding steps use `config.model`, default `claude-sonnet-5-5-high` (Claude Sonnet 5.5 High). The slug must match the Cursor model picker. Change the yaml, then the next Shuttle picks it up. In-flight tickets keep the model they started with; the branch records it in the PR body.

## 24/7 board

`.warp/BOARD.md` and `.warp/board.html` regenerate every tick. Leave a cloud agent on `/warp` overnight, or run a tick from a scheduled Cursor automation. The beam is local to the repo, so a second machine resumes by pulling `.warp/` (commit the beam and journal; they contain no secrets).
