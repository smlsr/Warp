# Named runs

One repo can carry several Warp runs: `core`, `admin`, `ui`. Each has its own beam, its own parent, its own `warp:<instance>` tag, its own branches, and its own folder in the repo. A run is named once, with `/warp-init <name>`.

## The name, the folder, and the scan

Three different things:

| | What it is | Where it is kept |
|---|---|---|
| Run name | Who owns the work. `core`, `admin`, `ui`: 1 to 32 lowercase letters, digits, or dashes. Not `g3`, `runs`, `all`, or other reserved words. | `run:` in `.warp/config.yaml` |
| Scan folder | Optional. Where `/warp-scan` looks for plans. Only used to segment a repo by folder. | `scanFolder:` in `.warp/config.yaml` |
| Scan | The tickets read from the plan. | `.warp/beam.json` and `.warp/scan.json` |

The run name is not the folder name. A run may scan the whole repo and have no folder.

## Commands

```
/warp-init admin           name this checkout's run, or take up a run that exists
/warp-scan HOS/UI/Admin    scan one folder and save it as scanFolder
/warp-scan                 scan again; reuses the saved folder
/warp-scan /               clear the saved folder and scan the whole repo
/warp-start                run it
```

`/warp-start` on a named run with no scan is refused: `refuse: admin has no scan. Run /warp-scan [folder] first.`

### /warp-init <name>

Warp reads origin first.

- **New name.** `run: admin is new.` then `next: /warp-scan [folder]`. The name is written to `run:` in the config.
- **A name the repo already has.** The run is being taken up. Init prints what it found so you know you are assuming a previous run:

  ```
  run: admin already exists on origin/main. You are taking up a previous run.
  config: .warp/runs/admin/config.yaml, last saved 3 hours ago by Dev Two
  folder: HOS/UI/Admin
  scan: 2026-10-12, 41 tickets, 6 gates
  state: stopped, parent last seen 3 hours ago
  progress: 12 merged, 2 open, 27 queued
  next: /warp-start
  ```

  Then it restores the run's `beam.json`, `journal.jsonl`, `scan.json`, `stats.json`, and ticket folders into `.warp/`, and overlays the saved config. Machine keys (`runner`, `launch`, `worktreeRoot`, `stateDir`, `subagentVm`, `memoryCheck`, `cloudSnapshot`) stay as they are on this machine. Secrets and webhooks are never in the saved config. Then `/warp-start` carries on where the run stopped. This is how a new agent picks up after a lost one.
- If the run was `running` and its last checkpoint is 30 minutes old or less, init warns that another agent may still be running it. Starting a second parent on one run makes the earlier parent end its turn (`parent: exit superseded`).
- With no name, `/warp-init` prints this checkout's run, or lists the runs on origin and asks which one.

One checkout runs one run. Asking for a different name in a checkout that already runs one is refused. `--switch` moves the checkout to the new name and keeps the old files in `.warp/previous-<old>/`. `--adopt` names a run this checkout already has unnamed, and keeps its state. `--dry-run` writes nothing.

### /warp-scan [folder | /]

| Argument | Result |
|---|---|
| none | Uses the saved `scanFolder`, or the whole repo if none is saved. |
| `HOS/UI/Admin` | Scans that folder and saves it as `scanFolder`. |
| `/` | Clears the saved folder: `folder: was HOS/UI/Admin, now whole repo`. Scans the root. |

A ticket id belongs to one run. When a scan finds an id another run already owns, it refuses and writes nothing (exit 4). `--skip-owned` leaves those tickets out and warns that tickets depending on them will not start until a cross-run dependency exists.

## What is saved to the repo

Local files stay at `.warp/` on each machine, as before. The repo gets a mirror under `.warp/runs/<name>/`, on the base branch and on every ticket branch of that run:

```
.warp/runs/admin/
  config.yaml      the run's settings, without machine keys, secrets, or webhooks
  beam.json        the plan and state (tokens and cost removed), plus run and checkpointAt
  journal.jsonl
  agents.json
  scan.json        what the scan found (the machine's root path removed)
  stats.json       tokens, cost, minutes, attempts, and recoveries per ticket, and totals
  STATUS.md  status.json  BOARD.md  board.html
  tickets/<id>/state.json  log.jsonl
```

A ticket's own folder travels on its ticket branch. The shared files (beam, journal, status, stats) are published by the parent to the base branch when a ticket merges, and at a checkpoint: when status or run state changes, or every `checkpointMinutes` (default 10). That keeps three runs from flooding the base branch. A run's branch names are `warp/<name>/<id>-<jira>`, so one run's branches never collide with another's.

A fresh VM with no config finds its run from the branch name, the beam, or `WARP_RUN`, in that order after the config's `run:`.

## What does not change

Unnamed runs (no `run:`) behave exactly as before: `.warp/` on the base branch, `warp/<id>` branches.
