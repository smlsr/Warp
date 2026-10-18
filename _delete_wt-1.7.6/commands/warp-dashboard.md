---
name: warp-dashboard
description: Write the run dashboard, a single HTML page with every ticket on a slot swimlane colored by status, and say where it is
---

Write the dashboard for this run. Run this one command and print its output as it is. Do not start, pause, or stop anything.

`/warp-dashboard ?` prints the help. Run `python3 <plugin>/scripts/dashboard.py ?` and print that text.

```bash
python3 <plugin>/scripts/dashboard.py write --beam .warp/beam.json
```

It prints `dashboard: .warp/dashboard.html tickets=<n> files=<n>` and an `open: file://...` line. Tell the user to open that file in a browser. Add `--open` when they ask you to open it and this machine has a browser.

- The page has the run's beam, every `.warp/tickets/<id>/state.json`, and the other runs' `.warp/runs/<name>/beam.json` inside it, so it opens with data. After that, each status write rewrites the file, and Reload in the page shows the new state.
- `--empty --out <path>` writes the page with no data. The user opens files with File > Open status files, File > Open .warp folder, or by dropping them on the page.
- `python3 <plugin>/scripts/dashboard.py path` prints where the page is in the plugin (`assets/warp-dashboard.html`). It can be opened straight from there.

What the page shows: a header with run estimate, agent hours, people hours, and tickets, each split into completed, in progress, errored or parked, and to do, with a Utilization panel under the totals (active agents over `maxAgents`, hours spent over the estimate, tokens and minutes by size and by agent); a swimlane of agent slots with gates and the critical chain, like the plan's swimlane PDF, on a Plan clock (estimate hours) or an Actual clock (hours since the run started, each ticket from claim to merge); a board view with lanes by gate block, status, module, Lane, Repo, or size; a Table view of every ticket, sortable on any column; color by status, module, or Lane; status filters, search, light and dark themes, and printing. File > Open also reads a build-schedule/2 `schedule.json` (drawn as planned, before any scan) and a `/warp-export` `WARP_PLAN.json`. Details: README "Dashboard".
