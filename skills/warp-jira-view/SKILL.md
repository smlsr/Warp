---
name: warp-jira-view
description: "Print every field on a Jira issue. The argument is an issue key such as WAR-1 or an external id such as WV-01."
---

# Jira view

Warp has no Jira credentials. The script classifies the argument and renders a transcript. You call the Atlassian MCP server named `jiraMcp` (default `atlassian`).

```bash
python3 <plugin>/scripts/jira_view.py WV-01
python3 <plugin>/scripts/jira_view.py WV-01 --results view.json
python3 <plugin>/scripts/jira_view.py WAR-1 --results view.json --comments --links
```

`jira_view.py ?` prints every flag (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it.

## Steps

1. Run `jira_view.py <XXX>` with no `--results`. Read the lines. It writes `.warp/jira-view.json` and does not call Jira.
2. `getAccessibleAtlassianResources` on `jiraMcp` for `cloudId`, unless `jiraSite` is set. Pass that URL as `cloudId`.
3. If the line says `as an issue key`, call `getJiraIssue` with `issueIdOrKey` set to that key. Request every field: `fields: ["*all"]` and `expand: "names"` when the tool has those arguments, otherwise the arguments the tool lists for all fields and display names. Then `getTransitionsForJiraIssue` (or `listJiraIssueTransitions`).
4. If `getJiraIssue` fails, or the line says `as an external id` and no key was resolved from the beam or `.warp/jira-map.json`, run the queries in `.warp/jira-view.json`. When `jiraProject` is empty, call `getVisibleJiraProjects` and search each visible project. Call `getJiraProjectIssueTypesMetadata` once, then `searchJiraIssuesUsingJql` for each external-id field, then `labels = "warp:<id>"`, then `getJiraIssueRemoteIssueLinks` when you have a candidate issue. Stop when one issue matches. Two matches are ambiguous: do not pick one and do not call `getJiraIssue`.
5. On one match, call `getJiraIssue` for that key with all fields and names, then `getTransitionsForJiraIssue`. Pass `--comments` through and include the comment field. Pass `--links` through and include `issuelinks` plus `getJiraIssueRemoteIssueLinks`.
6. Save a transcript and render it:

```json
{"query":"WV-01","direct":{"issue":null,"error":null},"projects":["WAR"],"fields":[{"id":"customfield_10050","name":"External ID"}],"searches":[{"jql":"...","issues":[{"key":"WAR-1"}]}],"remoteLinks":[],"issue":{"key":"WAR-1","id":"10001","names":{},"fields":{}},"transitions":[{"id":"11","name":"In Progress"}]}
```

```bash
python3 <plugin>/scripts/jira_view.py WV-01 --results view.json
```

Add `--comments`, `--links`, `--all`, `--full`, `--verbose`, or `--json` when the user asked for them. The same flags belong on the render command. `--all` shows empty fields. `--full` does not truncate. `--verbose` adds account ids. `--json` is the raw issue. A field named like a token or password is `<redacted>`.

Read the report to the user. `== warp ==` has the resolved key and how it was found, the status, the external-id field name and id, and whether the beam and `.warp/jira-map.json` store that key. `discovered field` is the id to set as `jiraExternalIdField` when it differs. `== fields ==` is `name: value`, with `customfield_NNNNN` in parentheses. Then transitions, and comments or links when those flags were set.

Do not write the beam or the map from this command. Do not pass a plan id to `transitionJiraIssue`.
