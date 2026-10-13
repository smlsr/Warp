---
name: warp-from-jira
description: Build the plan files /warp-scan reads from the Jira issues that carry given labels
---

Run the `warp-from-jira` skill. The argument is one or more Jira labels separated by commas: `/warp-from-jira version:1.5.5` or `/warp-from-jira version:1.5.5,ui,api`. An issue with any of them is taken (`--match all` takes only issues with every one). Warp asks Jira for those issues through the Atlassian MCP server (`jiraMcp`), then writes `warp-plans/<labels>/plan/schedule.json` (build-schedule/2), `plan/FROM_JIRA.md`, `jira/issues.json`, and `.warp/jira-map.json`, and prints `next: /warp-scan warp-plans/<labels>`. A folder that already has a plan is refused (`--force` rebuilds it). Nothing is written to Jira. `jira_pull.py ?` prints the options and how each issue field is read.
