## Multiple named Warp runs in one repo (1.6.0)

One repo can carry several Warp runs (`core`, `admin`, `ui`), each with its own beam, parent, tag, and branches. Runs can be started by gate range, can wait on each other, and can be addressed individually from Slack.

### Named runs
- **`/warp-init <name>`** reads origin first. New name: `next: /warp-scan [folder]`. Existing name: restores the saved config, scan, beam, journal, stats, and ticket folders, and says `next: /warp-start`. Warns if the run looks live. One checkout runs one run (`--switch`, `--adopt`).
- **Run name != scan folder.** `/warp-scan <folder>` saves `scanFolder`; `/warp-scan /` clears it. A named run will not start until it has been scanned.
- **Saved to the repo** under `.warp/runs/<name>/` (config mirror, beam, journal, agents, scan, stats, status, board, ticket folders). Local files stay at `.warp/`. Branches are `warp/<name>/<id>-<jira>`. Publishing is throttled by `checkpointMinutes`.
- A ticket id belongs to one run. A scan that finds an owned id refuses and writes nothing (`--skip-owned` leaves them out and rewrites dependents to `owner:id`).
- `/warp-ingest` removed (replaced by `/warp-scan`). Unnamed runs behave as before.

### Run control
- **`/warp-runs [name]`** lists runs on origin with state, owner, progress, last seen, and blocks.
- **`/warp-delete <name>`**: dry run by default; `--apply` removes `.warp/runs/<name>/` in one commit and pushes. Refuses live runs and runs others depend on unless `--force`; `--branches` deletes origin branches.
- **Owner and `--takeover`.** A start from another machine while the run is live refuses unless `--takeover`. The old parent exits `superseded`; its stale publish is blocked.
- **Slack/Teams addressing:** `warp:pause admin`, `warp:start admin G2-G3`, `@admin`. With several runs in a channel an unaddressed state command is refused; status is answered by all. Header: `Warp | repo / project | run <name> | warp:<id>`.

### Gate ranges and blocks
- `/warp-start G0-`, `G0-G3`, `-G2`, `G5`. When the range is cleared the run pauses with a block summary. A start or resume without a range clears it.
- Blocks finish in order. Block of a ticket: explicit `gate`, else lowest member gate, else latest dependency's block, else the first. A block is cleared when its tickets are merged/done/skipped and its gate is not red; a parked ticket holds later blocks. `gateOrder: deps` restores dependency-only ordering.

### Cross-run coordination
- `startAfter: core:G2` waits until that gate and earlier blocks are cleared in the other run (`--now` overrides).
- Dependencies written `run:id`. Tickets whose locks overlap another run's active locks wait. `paths` keeps a run to its folders. `baseFixOwner` names the one run that dispatches a base fix.
- Other runs are read from their published beams, at most every 5 minutes, so they are as fresh as their last checkpoint.

### Deliberate choices
- A ticket's folder rides on its ticket branch; shared run files are published by the parent after each merge, not refreshed on every ticket branch (identical shared files on every branch would conflict at merge).
- Strict block order is the default.
- Block assignment has no plan "wave" fallback.

### Tests
785 tests pass (`python3 -m unittest discover -s tests`), including new `test_runs.py` (47), `test_runs_team.py` (26), `test_blocks.py` (33), `test_runs_coord.py` (23). Not run against live Cursor.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01AWAPhNnQN6cSTJvH6t3KAH
