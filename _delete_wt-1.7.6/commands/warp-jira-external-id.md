---
name: warp-jira-external-id
description: Write existing plan-id mappings into the Jira External ID field
---

Run the `warp-jira-external-id` skill. Dry-run is the default and names the method on each line. `--apply --yes` prints one MUST DO block. If the External ID field is missing, the default is to add label `warp:<id>` (`update.labels` add). `--create-field --yes` probes for a create-field tool (off unless you pass it or set `jiraCreateExternalIdField`). `--recheck` looks the field up again. The agent calls the tool for each line, then `jira_sync.py record-external-id`. `jiraWriteExternalId: true` makes `/warp-scan` and `/warp-jira-check` print that same block without `--yes`. `jira_external_id.py ?` prints the options (`help`, `-h`, and `--help` do the same).
