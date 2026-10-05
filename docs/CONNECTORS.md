# Connectors

Warp uses Cursor's native MCP connections. Names in `.warp/config.yaml` are hints for which connected server to call. If a server is missing, say so and write Herald text to `.warp/outbox.md`. Do not mint tokens.

## Jira

Server name: `jiraMcp` (default `atlassian`). That name is the Cursor MCP server, not a tool. Project `jiraProject`, if you set one.

`jiraMcp: atlassian` does not by itself call Jira. `beam.py set` writes `.warp/jira-todo.json` and prints `jira: MUST DO`. The Shuttle or Reed in that turn has to call the tools and then `jira_sync.py record` / `record-comment`. `/warp-jira-check` prints what is still missing. A missing tool or a failed call writes `.warp/outbox.md` and a Herald message. The ticket is not stopped.

Every Jira call needs `cloudId`. Get it from `getAccessibleAtlassianResources`. If `jiraSite` is set (a site URL such as `https://yoursite.atlassian.net`), pass that as `cloudId`.

| Tool | Use |
|---|---|
| `getAccessibleAtlassianResources` | cloudId for this user. One site is stored in `jiraSite`. |
| `getVisibleJiraProjects` | Project keys. One project, or one match to the repo or an offline candidate, is stored in `jiraProject`. |
| `getJiraIssue` | current status name and status category. `/warp-jira-view` asks for every field and the names map. |
| `getTransitionsForJiraIssue` | transitions offered right now. Some servers list this as `listJiraIssueTransitions`. |
| `transitionJiraIssue` | apply the id `jira_sync.py pick` chose. Pass `transition.id`, or `transitionId` if that is the field in the tool schema. |
| `addOrEditJiraIssueComment` | comment, argument `commentBody`. Older servers call this `addCommentToJiraIssue`. |
| `searchJiraIssuesUsingJql` | Scan and claim look up `jiraExternalIdField` (name or `customfield_NNNNN`), then the other external-id names, then a label `warp:<id>`. One exact match is stored. Summary search waits for `--yes`. `/warp-jira-match` fetches a bounded candidate list (`summary ~` is fuzzy in Jira) and matches locally. |
| `getJiraProjectIssueTypesMetadata` | Field catalog. Used to turn `jiraExternalIdField` or `External ID` into a `cf[NNNNN]` clause, and to find the External ID field before a write. |
| `getJiraIssueRemoteIssueLinks` | Remote-link ids, after the external-id field and the label. |
| `getJiraIssueEditmeta` | Whether the External ID field is editable on that issue. A missing or read-only field is skipped. |
| `editJiraIssue` | Write the plan id into the External ID field. `/warp-jira-match --write-external-id` and `jira_sync.py external-id`, after `--yes`. This is a write and needs the connector permission. Scan and claim call it only when `jiraWriteExternalId` is true. No comment is added. |

Those names, and the Slack, Teams, and GitHub names below, are defined once in `scripts/mcp_tools.py`. `/warp-allow-notify` reads that module. It does not invent a second list.

`pick` matches the transition name, then the target status name, then (for In Progress and Done only) the status category. QA Ready is name-only. Never send a fixed transition id. An issue already in progress is left alone, and a Done issue is not reopened.

Who moves the issue:

| When | Who | What |
|---|---|---|
| Scan | `scan.py` | Stores a Jira export `key`, a plan `jiraKey`, a map-file key, or one exact external-id, label, or remote-link match. A plan id such as `WV-01` stays unmapped until that match. No transition. |
| Claim | dispatch or the Shuttle, from the `jira:` lines `beam.py set` prints | In Progress, plus a Jira comment (started, shuttle, branch). |
| PR opened | Shuttle, after `beam.py set --pr` | Jira comment with the link. Connected mode also comments on the pull request (ticket and Jira key). Local mode does not. |
| Bugbot / CI | Reed, after `beam.py set --bugbot` / `--ci` | Jira comment, and a pull-request comment in connected mode. |
| Waiting (L/XL, `autoMerge` false) | Reed, on `awaiting_approval` | QA Ready (`jiraQaReadyStatus`) and a comment that says to review or reply `warp:proceed <id>`. |
| Merge, auto (`autoMerge` true) | Reed. Connected: `beam.py set --status merged --sha`. Local: `provider.py merge-local`, which sets merged itself. | Done (`jiraDoneStatus`) and a merged comment (PR link, sha, local or connected). |
| Merge, manual | Reed, same commands | Merged comment only. The issue stays at QA Ready. |
| Release back to queued | dispatch | No move, unless `jiraRestoreOnRelease` is true. |
| Pause / stop | nobody | No Jira change. |
| Blocked / alarm | whoever sets that status | Jira comment. A failed transition is the Herald message above. |

Comments start with `Warp | <repo> / <project> / <key>`, the same header Herald uses plus the key. The beam stores `jira.comments.<event>` and `pr.comments.<event>` (`at`, `id`). A recorded event is not posted again.

Do not create tickets.

## GitHub and Bitbucket

`scripts/provider.py resolve` chooses the mode from `.warp/config.yaml`.

| Config | Effect |
|---|---|
| `gitProvider: auto` | GitHub if `origin` is a GitHub host, Bitbucket if it is Bitbucket. `/warp-init` writes the detected value and leaves a custom one alone. |
| `gitProvider: github` | Pull requests through `githubMcp`, then the `gh` CLI if `ghCli` is true and `gh` is already logged in. |
| `gitProvider: bitbucket` | Pull requests through `bitbucketMcp`. |
| `pushMerge: false` | Local-only. No push, no pull request, no provider call. |
| no `origin`, or another host | Local-only. `/warp-init` sets `pushMerge: false` when there is no remote. |

Connected mode, in order:

- Open the pull request from `warp/<id>-<jiraKey>` into `baseBranch` (empty detects origin's default, then `main` or `master`).
- Read CI and approvals. A GitHub approving review, or a Bitbucket participant status APPROVED, is the L/XL merge signal, along with `warp:proceed <id>`.
- Comment Bugbot evidence and acceptance-criterion ids.
- Squash-merge only from Reed, only on the policy in MERGE-POLICY.md.

Warp does not install `gh`, create SSH keys, or connect an app. If the connector and the CLI are both missing or error, do not raise an alarm and do not scrape credentials: run `scripts/provider.py note` and take the local path for that ticket.

Local-only: stay on the ticket branch. Reed squash-merges it into `baseBranch` with `scripts/provider.py merge-local`, which also writes `.warp/outbox.md` and asks Herald to post that it was local. A conflict leaves the branches untouched.

## Slack and Teams

`messenger: slack | teams | both`.

There is no webhook or token in Warp. Herald posts through these connected servers only. `/warp-init` and `/warp-scan` write `.warp/notify-post.json` and tell Herald to post it; with no channel or no server the text goes to `.warp/outbox.md`. `notify: quiet` skips both. Warp does not create channels. The shared `warp` channel must exist, and the connected app must be able to post to it.

Every message starts with the header `Warp | <repo> / <project>` because one channel serves many repos. Build messages with `scripts/herald_fmt.py` so the format stays the same; see `agents/herald.md`.

Cursor asks for a Run approval each time Herald calls a Slack or Teams tool, until that tool is on the MCP allowlist. `/warp-allow-notify` writes the allowlist. It allows `slack_post_message` and `slack_send_message` on `slackMcp`, and `send_channel_message` and `teams_send_message` on `teamsMcp`. Those are specific tools from `scripts/mcp_tools.py`, not a wildcard. If the prompt names a different tool, add `server:tool` to `notifyAllow` and run the command again.

The file that skips the IDE prompt is `.cursor/permissions.json` (`mcpAllowlist`), and it applies when Run Mode is Auto-review, Allowlist, or Run Everything. The CLI uses a different file (`.cursor/cli.json`, entries `Mcp(server:tool)`). A `beforeMCPExecution` hook is also installed; a hook `allow` does not currently skip the prompt. Cloud agents do not ask for approval, and this command does not change them. See the `warp-allow-notify` skill.

Post alarms, approval requests, gate flips, pause/resume, and digests. Command loopback is a message the next tick reads:

- `warp:proceed HOS-14` or `warp:proceed L-01`
- `warp:retry L-01`
- `warp:pause`
- `warp:resume`
- `warp:stop`
- `warp:start`
- `warp:status`

A PR comment with the same verb counts. Record the source.

## Bugbot

Use Cursor Bugbot on the PR. Reed stores pass/fail and the evidence string. A pass with no AC ids is not a pass.
