# Export, edit, import

Live state is not source control. Shuttles commit product code on `warp/<id>` branches. Warp does not commit `.warp/` on each tick. Add the snippet in `assets/gitignore-snippet.txt` (`/warp-init` does it) to the repo `.gitignore`.

## Where the files are

| Run | Working copy | How you open files |
|---|---|---|
| Local agent | Your clone on disk | Finder/Explorer, or the Cursor file tree, path `.warp/` |
| Cloud agent | A Cursor VM clone | Cursor file tree for that agent. Download before the VM is discarded. |

`stateDir` in `.warp/config.yaml` defaults to `.warp`. Set an absolute path to keep state outside the repo.

## Files you edit

Edit only the export. Do not hand-edit `beam.json`.

| File | Edit? | Role |
|---|---|---|
| `.warp/WARP_PLAN.json` | Yes, this is the import | Tickets, deps, locks, gates, autoMerge |
| `.warp/WARP_PLAN.md` | Read, optional notes | Brief for another model. Not imported. |
| `.warp/config.yaml` | Yes | Caps, model, messenger. Not replaced by import. |
| `.warp/beam.json` | No | Live status. Import rewrites the graph and keeps status for surviving ids. |
| `.warp/STATUS.md` | No | Generated. Download to review. |

## Steps

1. Pause or stop so a tick cannot claim mid-import. `/warp-pause`
2. Export. `/warp-export` writes `.warp/WARP_PLAN.json` and `.warp/WARP_PLAN.md`.
3. Download those two files from the Cursor file tree (cloud) or copy them (local).
4. Edit `WARP_PLAN.json`. Safe edits: `deps`, `locks`, `size`, `autoMerge`, `critical`, gate `members` and `checks`. Do not rename an id if you want its PR status kept.
5. Upload the edited JSON: drag it into the agent chat, or drop it in the repo as `WARP_PLAN.edited.json`. Do not commit it.
6. Import. `/warp-import` with that path. The run returns to stopped.
7. Read `.warp/STATUS.md`. Confirm counts. `/warp-start` or `/warp-resume`.

Import keeps status, agent, branch, attempts, tokens, minutes, alarm, the pull request, and `jira` for ids that still exist. Heartbeats (`lastSeenAt`) are not in the plan file and are not copied. A `recovering` status is kept with the other status fields. The run is stopped, so the watchdog does not replace anyone until the next start. New ids start queued. Removed ids drop off.
