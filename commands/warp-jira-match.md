---
name: warp-jira-match
description: Link Warp tickets to Jira issues by summary, and write the plan id into External ID
---

Run the `warp-jira-match` skill. Dry-run is the default. `--apply` stores one exact or 60-character prefix match. A fuzzy hit stays a proposal until `--apply --yes`. Ambiguous matches are listed and not stored. `--write-external-id --yes` writes the plan id into the Jira External ID field. That is a write and needs the connector permission. When `jiraWriteExternalId` is true, `/warp-scan` and `/warp-jira-check` queue that write for mappings that already exist, without `--yes`. `/warp-jira-external-id` is the bulk command. `jira_match.py ?` prints the options (`help`, `-h`, and `--help` do the same).
