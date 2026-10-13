---
name: warp-delete
description: Delete a named Warp run from the repo. Dry run unless --apply
---

Delete the run `<name>` from the base branch. This removes the run's folder `.warp/runs/<name>/` in one commit and pushes it, so the deletion merges like any other change. Always run the dry run first, show the user what it printed, and run `--apply` only when the user says yes.

`/warp-delete ?` prints the help. Run `python3 <plugin>/scripts/runs.py ?` and print that text.

```bash
python3 <plugin>/scripts/runs.py delete <name>
python3 <plugin>/scripts/runs.py delete <name> --apply
python3 <plugin>/scripts/runs.py delete <name> --apply --branches
```

The dry run prints how many files are in the run's folder, its state, the branches `warp/<name>/*` on origin, and what is kept. It refuses, and `--force` overrides:

- `refuse: <name> is running (parent last seen ...). /warp-stop it first.` A run whose parent saved state in the last 30 minutes.
- `refuse: <other> waits on <name> (...).` Another run has `startAfter` on it, or a ticket that depends on one of its tickets.

What is kept: merged code, Jira, and pull requests already merged. Branches stay unless `--branches`, which deletes them on origin (`git push origin --delete`). Deleting a branch closes its open pull request on GitHub. If this checkout runs `<name>`, its local files move to `.warp/previous-<name>/` and `run:` is cleared. Details: `docs/RUNS.md`.
