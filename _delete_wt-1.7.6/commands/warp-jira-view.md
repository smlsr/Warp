---
name: warp-jira-view
description: Print every field on a Jira issue by key or external id
---

Run the `warp-jira-view` skill. The argument is a Jira issue key (`WAR-1`) or an external id / plan id (`WV-01`). Print the field list. This command does not call Jira by itself: the script writes `.warp/jira-view.json` and renders the transcript you save after the Atlassian tools. Two matches are listed and neither issue is printed. `jira_view.py ?` prints the options (`help`, `-h`, and `--help` do the same).
