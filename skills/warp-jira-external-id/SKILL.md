---
name: warp-jira-external-id
description: "Write the plan id into Jira's External ID field for tickets that are already mapped. Dry-run lists each pair. --apply --yes is the write."
---

# External ID write-back

Warp has no Jira credentials. The script lists mapped tickets and prints one MUST DO block. You call the Atlassian MCP server named `jiraMcp` (default `atlassian`). Do not add a Jira comment.

```bash
python3 <plugin>/scripts/jira_external_id.py
python3 <plugin>/scripts/jira_external_id.py --ticket WV-01
python3 <plugin>/scripts/jira_external_id.py --apply --yes
python3 <plugin>/scripts/jira_sync.py record-external-id --results edits.json
```

`jira_external_id.py ?` prints every flag (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it.

## Dry-run

1. Run `jira_external_id.py` with no `--apply`. It does not call Jira and it does not change the beam.
2. Read each line to the user: `WV-01 -> WAR-1: External ID currently <value|empty|unknown|field missing> -> would set WV-01`.
3. The last line is `jira: external id: would write N, already equal N, skipped N (...)`.
4. A key whose source is `external` is already equal. A ticket with no confirmed key is skipped. A different non-empty value is skipped unless the user passed `--force-external-id`. `jira.externalId` on the beam is the plan id we want. It is not proof that Jira holds that value. `jira.externalIdWritten` is that proof.

## Apply

`--apply --yes` is the consent for this command. It works even when `jiraWriteExternalId` is false. `--dry-run` wins over `--apply`.

When the output contains `MUST DO write External ID`, do this in the same turn. One block, every line:

1. Call `getJiraProjectIssueTypesMetadata` once. The field is `jiraExternalIdField`, or External ID when that name is what the catalog has. Remember the field id (`customfield_NNNNN`).
2. For each `editJiraIssue KEY field FIELD = PLANID` line, call `getJiraIssue` and `getJiraIssueEditmeta` for that key.
3. If the field is missing or not editable on that issue, skip it. Do not invent a field. Record the skip in the results file.
4. If the live value already equals the plan id, do not edit. Record `result: already`. An equal value is not an error.
5. If the live value is a different non-empty id, leave it unless the user passed `--force-external-id`. Show before and after.
6. Otherwise call `editJiraIssue` and set that field id to the plan id. Do not add a comment.
7. Save one results file and record every row, including skips:

```json
{"fields":[{"id":"customfield_10050","name":"External ID"}],"editmeta":{"fields":{"customfield_10050":{"name":"External ID"}}},"issues":[{"key":"WAR-1","fields":{"customfield_10050":""}}],"edits":[{"id":"WV-01","key":"WAR-1","before":"","result":"updated"}]}
```

```bash
python3 <plugin>/scripts/jira_sync.py record-external-id --beam .warp/beam.json --results edits.json
```

`result` is `updated`, `already`, or a skip with `error` of `field missing` or `read-only`. A failed or skipped row sets `jira.externalIdAttempt`. `/warp-jira-check` prints `externalIdAttempt: skipped — field missing`. It does not overwrite `jira.lastAttempt`. A recorded write sets `jira.externalIdWritten` and clears that attempt.

`--force` queues tickets that were already recorded, including keys that came from the external-id search. `--force-external-id` is the only flag that replaces a different non-empty value. `--ticket WV-01` limits the run.

`/warp-scan` and `/warp-jira-check` print this same MUST DO block when `jiraWriteExternalId` is true. That config flag is the consent. Those commands do not need `--yes`. Execute the block the same way, then `record-external-id`.
