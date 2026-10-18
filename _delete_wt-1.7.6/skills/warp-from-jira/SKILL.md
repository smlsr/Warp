---
name: warp-from-jira
description: "Build the plan files /warp-scan reads from the Jira issues that carry one or more labels, for example /warp-from-jira version:1.5.5,ui. Use when there is no plan file and the work is already in Jira."
---

# From Jira

Warp has no Jira credentials. The script writes the query and builds the files; you call the Atlassian MCP server named `jiraMcp` (default `atlassian`) in between. Nothing is written to Jira.

```bash
python3 <plugin>/scripts/jira_pull.py plan version:1.5.5,ui
python3 <plugin>/scripts/jira_pull.py build
```

`jira_pull.py ?` prints every flag (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it.

## Steps

1. Run `jira_pull.py plan <labels>` with the user's labels (commas or spaces) and any flags they gave: `--folder <folder>`, `--project <KEY>` or `--any-project` (default: `jiraProject` from the config), `--match all` (every label; default any), `--jql "<clause>"`, `--force`. It writes `.warp/jira-pull.json` and prints `jql:`, `fields:`, `folder:`. If it prints `refuse:`, stop and show that line: the folder already has a plan, and `/warp-scan <folder>` reads it. Rebuild only when the user says so (`--force`).
2. `cloudId`: `jiraSite` when it is set, else `getAccessibleAtlassianResources` on `jiraMcp`.
3. Call `searchJiraIssuesUsingJql` with exactly the printed `jql`, `fields` set to the printed list, and `maxResults: 100`. When the `fields:` line says expand names, also pass `expand: "names"` if the tool has that argument. Repeat with `nextPageToken` (or `startAt` plus the page size, when the tool pages that way) until there is no next page or `isLast` is true. Do not summarize or edit what comes back.
4. Save every response as returned in one file, `.warp/jira-pull/issues.json`: `{"pages": [<first response>, <second response>, ...]}`. If the tool returned field names, add them as `"names": {"customfield_10050": "External ID"}`. If a response left out `description` or `issuelinks` for an issue, call `getJiraIssue` for that key with the same fields and add it as `"issues": [<that issue>]` in the same file; that copy wins.
5. Run `jira_pull.py build`. It prints `jira:`, `note:` (Done issues and epics left out, where plan ids came from), `plan:` (tickets, gates, agent-hours, finish, peak), `warn:` lines, `wrote:`, and `next: /warp-scan <folder>`.
6. Tell the user the `plan:` line, every `warn:` line as it is, and the `next:` line. Typical warnings: issues with no size (planned as M), issues with no locks (Warp cannot keep two of them off the same files), blockers outside these labels that are not Done (left out). These are fixed in Jira (add the `size:M` label, a Locks section, the label on the blocker) and rebuilt with `--force`; or by editing the written files before `/warp-scan`. Do not run `/warp-scan` or `/warp-start` unless the user asked for it.

`refuse: ... dependency cycle: A → B → A` means the links in Jira loop; nothing was written. `refuse: ... holds no issues` means the search found nothing: check the label spelling and `--project` / `--any-project`.

## How an issue is read

Warp's own Jira import (`/build-pack`) writes everything this reads; hand-made issues work with less:

- Plan id: the External ID field (`jiraExternalIdField`, else a field named External ID, External Key, Plan ID or Ticket ID), else label `warp:<id>` or `ext:<id>`, else a `[ID]` summary prefix, else the Jira key itself.
- Size: label `size:S|M|L|XL`, else the `Size` section, else the original estimate, else story points; hours are the original estimate when set.
- Blockers: `is blocked by` / `depends on` links and the `Blocked by` section. `run:id` there stays a cross-run blocker. A blocker that is Done is met.
- Locks: the `Locks` section, one path per line. Gate: label `gate:G1`. Lane, repo, module: labels `lane:`, `repo:`, `area:` (else the first component). Acceptance: the `Acceptance` or `Acceptance criteria` section.
- Sections are `h2. Name` (wiki), `## Name` (markdown), or headings in the rich-text description.

The schedule is planned with `maxAgents` slots, `gateOrder` and `autoMergeSizes` from `.warp/config.yaml`, by the same rule Warp dispatches with (`scripts/plansim.py`). Each ticket keeps its Jira key (`jira`), and `.warp/jira-map.json` gets every plan id, so `/warp-scan` stores the keys without a lookup.
