---
name: warp-jira-match
description: "Link unmapped Warp tickets to Jira issues by summary. Store an exact or prefix match with --apply. A fuzzy score is a proposal until --yes. Optionally write the plan id into the External ID field."
---

# Jira match

Warp has no Jira credentials. The script builds JQL, matches a saved candidate list locally, and records a confirmed pair the same way `resolve` does. You call the Atlassian MCP server named `jiraMcp` (default `atlassian`).

```bash
python3 <plugin>/scripts/jira_match.py
python3 <plugin>/scripts/jira_match.py --results candidates.json
python3 <plugin>/scripts/jira_match.py --results candidates.json --apply
python3 <plugin>/scripts/jira_match.py --write-external-id --yes --results edits.json
```

`jira_match.py ?` prints every flag (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it.

## Match

1. Run `jira_match.py` with no `--results`. It writes `.warp/jira-match.json` and does not call Jira. Dry-run is the default.
2. When `jiraProject` is empty, call `getVisibleJiraProjects` and run each query in every visible project. Otherwise the JQL is `project = X AND summary ~ "..."`.
3. `summary ~` is fuzzy in Jira. Call `searchJiraIssuesUsingJql` for each `jql`. Request `summary`, `status`, and the external-id field. `maxResults` is 50. Follow `nextPageToken` at most once (2 pages). If `isLast` is still false, set `truncated` true on the transcript.
4. Save one transcript and match it locally:

```json
{"issues":[{"key":"WAR-1","summary":"Build the widget","status":"To Do"}],"searches":[{"jql":"project = WAR AND summary ~ \"build the widget\"","issues":[],"isLast":true,"total":1}],"truncated":false}
```

```bash
python3 <plugin>/scripts/jira_match.py --results candidates.json
```

5. Read each ticket: plan id, Warp summary, candidate keys, their summaries, match type, and score. Order is exact (case, punctuation, and whitespace ignored), then the first 60 characters when both summaries are at least that long (`--chars`), then a token/ratio score at or above 0.9 (`--min-score`) as a proposal only.
6. `--apply` stores one exact match (confidence high) or one prefix match (confidence medium) on the beam and in `.warp/jira-map.json`, source `summary`. Add `--yes` to also store one fuzzy proposal (confidence low). Do not store an ambiguous row: two issues with the same or near-identical summary, or one issue claimed by two tickets. A manual key is left as is. Done and closed issues are skipped unless `--include-done`. An issue already mapped to another ticket is skipped.
7. Pass `--all` only to report tickets that already have a key. Do not replace those keys. Pass `--id` to limit the run.

## Write the External ID

This is a write to Jira. It needs the connector permission from `/warp-allow-notify --with-jira` (`editJiraIssue` and `getJiraIssueEditmeta`). Do it only when the user passed `--write-external-id` or `--set-external-id`, or when `jiraWriteExternalId` is true during scan or claim. The explicit flags work even when that config key is false, and they still need `--yes`.

1. `python3 <plugin>/scripts/jira_match.py --write-external-id` (or `--set-external-id WV-01=WAR-1`, or `jira_sync.py external-id`). Without `--yes` this is a dry-run: it shows before and after and writes nothing.
2. Call `getJiraProjectIssueTypesMetadata` and `getJiraIssueEditmeta` for the issue. The field is `jiraExternalIdField`, or External ID when that name is what the catalog has.
3. If the field is missing or not editable, skip it and say so. The command still succeeds. Do not invent a field.
4. If the current value is a different non-empty id, leave it unless `--force-external-id`.
5. Otherwise call `editJiraIssue` and set that field to the plan id. Do not add a Jira comment.
6. Save `{"fields":[...],"editmeta":{"fields":{"customfield_10050":{"name":"External ID"}}},"edits":[{"id":"WV-01","key":"WAR-1","before":"","result":"updated"}]}` and run:

```bash
python3 <plugin>/scripts/jira_match.py --write-external-id --yes --results edits.json
```

A second run sees `jira.externalIdWritten` and does not send the edit again.
