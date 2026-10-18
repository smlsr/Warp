---
name: warp-jira-check
description: Show why a Jira issue did not move, and what comment or transition is still missing
---

Run the `warp-jira-check` skill. Print the report, including `status`, `source`, `reason`, `fix`, and `why nothing linked`. `verify` with no flags does not write keys and does not call Jira. If any ticket is unmapped, run `verify --link`, then the Jira searches in `.warp/jira-resolve.json` when that server is connected, then `verify --apply`. Do not pass a plan id to `transitionJiraIssue`. `jira_sync.py ?` prints the options (`help`, `-h`, and `--help` do the same).
