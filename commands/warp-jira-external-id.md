---
name: warp-jira-external-id
description: Write existing plan-id mappings into the Jira External ID field
---

Run the `warp-jira-external-id` skill. Dry-run is the default. It lists each mapped ticket as `WV-01 -> WAR-1: External ID currently <value|empty|unknown|field missing> -> would set WV-01`. `--apply --yes` prints one MUST DO block. The agent calls `editJiraIssue` for each line, then `jira_sync.py record-external-id`. `jiraWriteExternalId: true` makes `/warp-scan` and `/warp-jira-check` print that same block without `--yes`. `jira_external_id.py ?` prints the options (`help`, `-h`, and `--help` do the same).
