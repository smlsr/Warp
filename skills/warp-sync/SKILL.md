---
name: warp-sync
description: "Read Jira statuses into the beam. Dry-run unless --apply. Does not dispatch or start a listener. Use when the beam should follow Jira, including branch and pull request lookup."
---

# Warp sync

Jira into the beam. Catchup goes the other way. `/warp-update-state` does not read Jira.

```bash
python3 <plugin>/scripts/jira_sync.py sync --beam .warp/beam.json
python3 <plugin>/scripts/jira_sync.py sync --beam .warp/beam.json --jira issues.json --prs pulls.json --apply
python3 <plugin>/scripts/jira_sync.py sync ?
```

`jira_sync.py sync ?` prints the options (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it.

1. Run `sync` with no `--jira`. It prints `getJiraIssue` for each keyed ticket and the branch to look up. It writes nothing.
2. Call `getJiraIssue` on `jiraMcp` for each of those keys. Save the statuses (name and status category) as JSON.
3. Pull requests: GitHub uses `gh pr list --head <branch> --state all --json number,url,state,headRefOid,headRefName`. Bitbucket, or no `gh`, goes in `--prs` with the same fields, keyed by ticket id, issue key, or branch.
4. Rerun with `--jira` and `--prs` when you have that file. Read the lines. Dry-run is the default.
5. `--apply` writes the beam, journal, and board. `--force` is required before a ticket moves to an earlier status. Do not pass `--force` unless the user asked.
6. Tell the user the per-ticket lines and the summary (`sync: N differ, N refused, N unchanged`).

Mapping uses the configured status names. To Do (or category `new`) is `queued`. `jiraInProgressStatus` needs a Shuttle. `jiraInReviewStatus` is `reviewing`. `jiraQaReadyStatus` is `awaiting_approval`. `jiraDoneStatus` (or category `done`) is `merged`. A missing `jiraInReviewStatus` is `In Review`. An empty value maps nothing to In Review; an open pull request stays `reviewing`. A merged pull request wins over Jira.

Do not dispatch. Do not start a listener. Do not merge. After `--apply`, `/warp-start` picks the work up through the unfinished-first pass.
