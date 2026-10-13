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

A ticket id belongs to one run. When a scan finds an id another run already owns, it refuses and writes nothing (exit 4). `--skip-owned` leaves those tickets out and rewrites each dependency on one of them to `<owner>:<id>` (`deps: these tickets depend on a ticket another run owns. They now wait for it as <run>:<id>: ...`). Those tickets wait until the owner merges it, as in "Depending on another run's ticket" below.

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

A ticket's own folder travels on its ticket branch. The shared files (beam, journal, status, stats) are published by the parent to the base branch when a ticket merges, and at a checkpoint: when status or run state changes, or every `checkpointMinutes` (default 10). That keeps three runs from flooding the base branch. A run's branch names are `warp/<name>/<id>-<jira>` (`warp/<name>/<id>` when the ticket has no Jira key), so one run's branches never collide with another's.

Each process finds its run in this order: an explicit `--run <name>` (`/warp-start` and `/warp-resume`), then `WARP_RUN`, then `run:` in this checkout's config, then the beam's `run`, then a ticket branch named `warp/<name>/...`. A fresh VM with no config, such as a Shuttle on its own VM, finds it from `WARP_RUN`, the beam, or its branch.

A checkout that runs a named run adds its live `.warp/` files (beam, journal, status and board files, agents, scan, stats, `checkpoint.json`, and `tickets/`) to `.git/info/exclude`, so `git status` does not list a second copy of what the run folder holds. `.warp/checkpoint.json` records when this checkout last published (`at`) and a hash of run state and ticket statuses (`sig`). A publish with no change in `sig` inside `checkpointMinutes` prints `state: <name> is current on <base>` and commits nothing.

## Running together

### Who owns a run

`/warp-start` records the owner on the beam: who (git `user.name`), the machine, and when. A run that another machine is running, with a parent that saved state in the last 30 minutes, is not started by a second person by accident:

```
refuse: admin is running under Dev Two on devtwo-mbp, since 2026-10-12 14:02 (parent last seen 4m ago). /warp-start --takeover takes it over. /warp-init admin shows its state.
```

`/warp-start --takeover` takes it over. The earlier parent finds the new owner at its next `parent-exit` and ends its turn (`parent: exit superseded`), and its publish is skipped (`state: not published. admin was taken over by ...`), so it cannot write over the new owner's state. The same machine, a stopped run, and a run with an old checkpoint start without it.

### Waiting for another run

```yaml
startAfter: core:G2        # in admin's .warp/config.yaml
```

`/warp-start` reads core's published beam and refuses until core's G2 and every block before it are cleared: `refuse: admin starts after core:G2. /warp-start --now starts it anyway.` The lines before it say where core is (`core is at G1 (3 of 8 merged). It must clear G2 first`). A run that does not exist yet, or has no such gate, is named. `/warp-runs` shows `startAfter=core:G2(waits)` and then `(met)`, so a developer knows when to start. Several runs: `startAfter: core:G2, admin:G1`.

### Depending on another run's ticket

Write the dependency as `<run>:<id>`: `deps: ["core:P-014"]` in `schedule.json`, or `core:P-014` in the deps cell of a table or an `after` list. The ticket waits until core's P-014 is merged in core's published beam. The parent reads the other runs' published beams every few minutes, and at start, and keeps what it needs under `external` on its own beam. `/warp-scan` warns when the run is not published yet, or does not have that ticket. `/warp-scan --skip-owned` leaves out the tickets another run owns and rewrites the tickets that depended on them to `core:A-1`.

### Runs in other repos

A build-schedule/2 schedule.json can cover several repos (HOS-Admin: `HumanifyOS` and `HumanifyOS-UI`). Each repo gets its own run, and each run scans the same file:

```yaml
# HumanifyOS checkout
run: admin
repoRuns: {HumanifyOS-UI: admin-ui}
externalRepos: {admin-ui: https://github.com/acme/HumanifyOS-UI.git}

# HumanifyOS-UI checkout
run: admin-ui
repoRuns: {HumanifyOS: admin}
externalRepos: {admin: https://github.com/acme/HumanifyOS.git}
```

`/warp-scan` keeps the tickets whose `repo` is this checkout (`repo:` in the config, else origin's name) and prints `repo: HumanifyOS — 72 of 120 tickets` and `deps: blockers in another repo become run:id`. A blocker in the other repo becomes `admin-ui:AU-07` and waits exactly like a ticket in another run of this repo, except the parent fetches the other repo's base branch to read its published beam. Locks written `HumanifyOS/src/...` lose the prefix. Both runs must be named. `python3 scripts/buildsched.py show plan/schedule.json --repo HumanifyOS` shows the split without scanning.

### Paths and locks

```yaml
paths: HOS/UI/Admin, packages/admin
```

A ticket whose locks leave those paths does not start, `/warp-start` lists them (`hold: 2 tickets, locks outside this run's paths ...`), and `/warp-scan` warns about them. Widen `paths` to let them run. Locks of the tickets that other runs have in flight also hold a ticket back, so two runs never edit the same path at once. They are read with the dependencies above.

### The base branch

Every run publishes to the same base branch, so origin moves under it. A publish fetches first, fast-forwards the local base, builds its commit on origin's tip, and pushes that commit. It touches only `.warp/runs/<name>/`, so runs never conflict with each other.

When the base branch goes red, every run holds merging. `baseFixOwner: core` makes one run dispatch the fix, so three runs do not start three. The others print `basefix: core owns the fix ... This run waits for it.` With no owner every run fixes its own, as before.

## Commands for several runs

```
/warp-runs                    runs on the base branch: state, owner, folder, progress, what each waits for
/warp-runs admin              one run: what /warp-init admin would show (runs.py show admin)
/warp-delete admin            a dry run: what would be removed, what is kept
/warp-delete admin --apply    remove .warp/runs/admin/ from the base branch in one commit, and push
```

`/warp-delete` refuses a run whose parent saved state in the last 30 minutes, and a run that another run waits on (`startAfter`, or a ticket dependency), unless `--force`. It keeps merged code, Jira, and merged pull requests. Branches `warp/<name>/*` stay unless `--branches`, which deletes them on origin (`git push origin --delete`). Deleting a branch closes its open pull request on GitHub. If this checkout runs `<name>`, its local files move to `.warp/previous-<name>/` and `run:` is cleared. The deletion is a commit on the base branch, so it merges like any other change.

## Slack and Teams

Every message header names the run: `Warp | <repo> / <project> | run admin | warp:<instance>`. Commands name the run too: `warp:pause admin`. See `docs/COMMANDS.md`, "Channel verbs".

## What does not change

Unnamed runs (no `run:`) behave exactly as before: `.warp/` on the base branch, `warp/<id>-<jira>` branches (`warp/<id>` with no Jira key).
