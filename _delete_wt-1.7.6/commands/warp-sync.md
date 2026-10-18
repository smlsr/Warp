---
name: warp-sync
description: Read Jira statuses into the beam. Dry-run unless --apply
---

Run the `warp-sync` skill. This reads Jira into the beam. It does not dispatch and it does not start a listener. `/warp-start` picks the work up on the unfinished-first pass.

```bash
python3 <plugin>/scripts/jira_sync.py sync --beam .warp/beam.json
python3 <plugin>/scripts/jira_sync.py sync --beam .warp/beam.json --jira issues.json --prs pulls.json
python3 <plugin>/scripts/jira_sync.py sync --beam .warp/beam.json --jira issues.json --apply
python3 <plugin>/scripts/jira_sync.py sync ?
```

`jira_sync.py sync ?` prints the options (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it.

With no `--jira` file the script only prints what to fetch. Call `getJiraIssue` for each issue key, save the transcript, and rerun with `--jira`. GitHub pull requests are looked up with `gh pr list --head <branch>`. Bitbucket, or a machine without `gh`, uses `--prs`. A merged pull request wins over the Jira status.

Dry-run is the default. `--apply` writes the beam, the journal, and the board. `--force` is required to move a ticket backward. One line per ticket that differs, then a summary.
