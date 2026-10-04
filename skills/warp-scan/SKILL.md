---
name: warp-scan
description: "Scan any repo, or one folder of it, for CURSOR_PLAN.md, schedule.json, a build map, or a Jira ticket export and build the Warp plan. Use as the first step on a project, or to rebuild after the spec changes."
---

# Warp scan

First step on every repo. Do not assume HumanifyOS paths.

## Run

```bash
python3 <plugin>/scripts/scan.py scan --root . --out .warp/beam.json
```

## Scope to a folder

`/warp-scan` takes an optional folder, as a path or a name. Without it the whole repo is scanned, as before.

```bash
python3 <plugin>/scripts/scan.py scan --root . --folder HOS/spec --out .warp/beam.json
```

Only that folder is searched for plans, schedules, and exports. `.warp/` stays at the repo root, and `scan.json` and the beam record the folder.

| Argument | Result |
|---|---|
| An existing path under the repo, such as `HOS/spec` | Used as given. |
| A name or trailing path, such as `spec`, matching one folder | That folder. |
| A name matching several folders, only one with plan files | That folder, with a note listing the others. |
| A name matching several folders, none or more than one with plan files | Nothing is scanned. The script lists the matches (exit 3). Ask the user which one, then run again with the full path. |
| No such folder, or a path outside the repo | Error. Nothing is written. |
| A folder with no plan files | "no plan found in <folder>" (exit 2). |

With no argument, if plans sit in more than one folder, the scan prints which folders and still uses the richest plan. Suggest `/warp-scan <folder>` if that is not the one the user wants.

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

## Message to Slack or Teams

A successful scan writes `.warp/notify-post.json`: format, ticket count, gate count, estimate, and links to the plan used and the other plan, schedule, and export files found. Links use the `origin` remote and the current branch when it is GitHub, GitLab, or bitbucket.org, and work once the branch is pushed. Otherwise the relative path is used. Follow the `herald:` line the scan prints and post as `agents/herald.md` describes. `notify: quiet` posts nothing. With no channel set, or no connected server, the text goes to `.warp/outbox.md`. Tell the user and carry on. A failed scan (no plan found) posts nothing.

## After the scan

Tell the user the format, ticket count, gate count, and that the plan is stopped. Offer `/warp-export` if they want another model to critique the order before `/warp-start`.
