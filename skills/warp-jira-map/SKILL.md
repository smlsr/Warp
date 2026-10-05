---
name: warp-jira-map
description: "Map plan ticket ids to real Jira issue keys. Use when Jira transitions fail, a ticket is unmapped, or the user gives pairs like WV-01 = WAR-1."
---

# Jira map

A plan id such as `WV-01` is not a Jira issue key. `jiraKey` must be the real key, such as `WAR-1`. This command is not required. `/warp-scan` and the first claim already store a key from the plan, the map file, or one exact Jira match. The order is: explicit key in the plan or map file, then an external-id field (`jiraExternalIdField`, or External ID, External Id, ExternalId, External Key, Plan ID, Ticket ID), then a label `warp:<id>` or a remote-link id, then a summary match that waits for confirmation. Use this command to review mappings, to set a ticket that stayed unmapped or ambiguous, or to override a key. A key set with `--set` or `beam.py set --jira` is never replaced by a later lookup. Two Jira issues are reported and neither is stored.

`python3 <plugin>/scripts/jira_sync.py map ?` prints every option (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it.

## List

```bash
python3 <plugin>/scripts/jira_sync.py map --beam .warp/beam.json
```

Each line is `id` and the key, or `unmapped`.

## Set

```bash
python3 <plugin>/scripts/jira_sync.py map --beam .warp/beam.json --set WV-01=WAR-1
```

Repeat `--set` for more than one ticket. This updates the beam and `.warp/jira-map.json`. A rescan keeps it. `jiraProject` or `jiraKeyPrefixes` must match the key's prefix, unless the user said to force it (`--force`).

The same check applies to `beam.py set --id WV-01 --jira WAR-1`.

CSV, JSON (`{"WV-01": "WAR-1"}`), or a markdown table with `id` and `jira key`:

```bash
python3 <plugin>/scripts/jira_sync.py map --import mappings.csv
```

Plans can also carry the key: `schedule.json` `jiraKey`, a line `Jira: WAR-1`, a `Jira Key` column, or `[WAR-1]` in the heading.

## From Jira

When the user wants the external-id, label, and remote-link lookup again, or a dry run:

```bash
python3 <plugin>/scripts/jira_sync.py map --from-jira --beam .warp/beam.json
```

`--auto` is the same. This prints the JQL and writes no keys. Call `getJiraProjectIssueTypesMetadata`, then `searchJiraIssuesUsingJql` for each external and label query, then `getJiraIssueRemoteIssueLinks` when you have a candidate issue. Save:

```json
{"fields":[{"name":"External ID","id":"customfield_10050"}],"searches":[{"jql":"project = WAR AND cf[10050] = \"WV-01\"","issues":[{"key":"WAR-1"}]}],"remoteLinks":[]}
```

```bash
python3 <plugin>/scripts/jira_sync.py map --from-jira --results results.json --dry-run
```

`--dry-run` prints `WV-01 -> WAR-1 (external, high)` and writes nothing. Drop `--dry-run` to store the key, the source, and the confidence on the ticket and in `.warp/jira-map.json`. Add `--yes` only to store a unique summary proposal. Without `--yes` a summary match is printed and not written. That summary step uses the same normalization as `/warp-jira-match` (case, punctuation, and whitespace) and still only accepts an exact summary. Prefix and fuzzy matches belong to `/warp-jira-match`.

## Search, then confirm

Only when the user wants Jira searched by summary alone. This writes JQL and changes no keys.

```bash
python3 <plugin>/scripts/jira_sync.py map --search --beam .warp/beam.json
```

Call `searchJiraIssuesUsingJql` on the `jiraMcp` server (default `atlassian`) for each `jql` in `.warp/jira-search.json`. Save the issues as JSON keyed by ticket id: `{"WV-01": [{"key": "WAR-1", "summary": "Build the widget"}]}`.

```bash
python3 <plugin>/scripts/jira_sync.py map --match results.json
```

Read the proposals to the user. Store them only after an explicit yes:

```bash
python3 <plugin>/scripts/jira_sync.py map --match results.json --yes
```

## After

Tickets already claimed or merged are not moved backward. Run catch-up so the pending transition and comments are printed. Then do those Jira calls with the real key, not the plan id.

```bash
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json
```
