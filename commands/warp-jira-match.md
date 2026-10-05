---
name: warp-jira-match
description: Link Warp tickets to Jira issues by summary, and write the plan id into External ID
---

Run the `warp-jira-match` skill. Dry-run is the default. `--apply` stores one exact or 60-character prefix match. A fuzzy hit stays a proposal until `--apply --yes`. Ambiguous matches are listed and not stored. `--write-external-id --yes` writes the plan id into the Jira External ID field. That is a write and needs the connector permission. Scan and claim do not write that field unless `jiraWriteExternalId` is true. `jira_match.py ?` prints the options (`help`, `-h`, and `--help` do the same).
