---
name: warp-scan
description: "Scan any repo for CURSOR_PLAN.md, schedule.json, a build map, or a Jira ticket export and build the Warp plan. Use as the first step on a project, or to rebuild after the spec changes."
---

# Warp scan

First step on every repo. Do not assume HumanifyOS paths.

## Run

```bash
python3 <plugin>/scripts/scan.py scan --root . --out .warp/beam.json
```

The scan writes `.warp/scan.json` (what it found) and `.warp/beam.json` (the plan). `runState` is `stopped`. Nothing dispatches until `/warp-start`.

## What it looks for

| File | How it is read |
|---|---|
| `WARP_PLAN.json` | Warp exchange format. Wins if present. |
| `schedule.json` with a `tickets` array | Deps, locks, gates, size. Preferred when there is no Warp plan. |
| `CURSOR_PLAN.md`, `CURSOR_PLAN_FAST.md` | Wave headings, ticket ids, `after` blockers, `Gates before` checks. |
| `*tickets*.json`, `jira/*.json` | Jira export: `blockedBy`, `blockedByTempIds`, or Blocks links. |
| Markdown table | Columns `id` and `deps` or `blockers`. |

A connected Jira plugin is the live source after the scan. If the repo has no plan file, ask the user to export the Jira filter to JSON or to connect the Atlassian MCP, then scan again. Do not invent tickets.

## After the scan

Tell the user the format, ticket count, gate count, and that the plan is stopped. Offer `/warp-export` if they want another model to critique the order before `/warp-start`.
