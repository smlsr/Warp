---
name: warp-jira-map
description: "Map plan ticket ids to real Jira issue keys. Use when Jira transitions fail, a ticket is unmapped, or the user gives pairs like WV-01 = WAR-1."
---

# Jira map

A plan id such as `WV-01` is not a Jira issue key. `jiraKey` must be the real key, such as `WAR-1`. This command is not required. `/warp-scan` and the first claim already store a key from the plan, the map file, or one exact Jira external-id match. Use this command to review mappings, to set a ticket that stayed unmapped or ambiguous, or to override a key. It does not call Jira itself except when you ask for the summary search below.

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

## Search, then confirm

Only when the user wants Jira searched by summary. This writes JQL and changes no keys.

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
