# Connectors

Warp uses Cursor's native MCP connections. Names in `.warp/config.yaml` are hints for which connected server to call. If a server is missing, say so and write Herald text to `.warp/outbox.md`. Do not mint tokens.

## Jira

Server name: `jiraMcp` (default `atlassian`). That name is the Cursor MCP server, not a tool. Project `jiraProject`, if you set one.

`jiraMcp: atlassian` does not by itself call Jira. `beam.py set` writes `.warp/jira-todo.json` and prints `jira: MUST DO`. The Shuttle or Reed in that turn has to call the tools and then `jira_sync.py record` / `record-comment`. `/warp-jira-check` prints what is still missing. A missing tool or a failed call writes `.warp/outbox.md` and a Herald message. The ticket is not stopped, except the first claimed ticket of a run (no ticket has `jira.startedAt` yet) when Jira says the issue was not found or no real key can be resolved: that claim is released and the run stops. A later miss does not. `jiraTransition: false` does not stop the run.

Every Jira call needs `cloudId`. Get it from `getAccessibleAtlassianResources`. If `jiraSite` is set (a site URL such as `https://yoursite.atlassian.net`), pass that as `cloudId`.

| Tool | Use |
|---|---|
| `getAccessibleAtlassianResources` | cloudId for this user. One site is stored in `jiraSite`. |
| `getVisibleJiraProjects` | Project keys. One project, or one match to the repo or an offline candidate, is stored in `jiraProject`. Scan also writes `jiraProject` when every stored issue key is in one project, even if this tool is missing. The line is `jira: set jiraProject to WAR (every stored key is in project WAR)`. Do not run `project --set` for that line. Mixed keys stay empty. A different existing value is not overwritten. |
| `getJiraIssue` | current status name and status category. `/warp-jira-view` asks for every field and the names map. |
| `getTransitionsForJiraIssue` | transitions offered right now. Some servers list this as `listJiraIssueTransitions`. |
| `transitionJiraIssue` | apply the id `jira_sync.py pick` chose. Pass `transition.id`, or `transitionId` if that is the field in the tool schema. |
| `addOrEditJiraIssueComment` | comment, argument `commentBody`. Older servers call this `addCommentToJiraIssue`. |
| `searchJiraIssuesUsingJql` | Scan and claim look up `jiraExternalIdField` (name or `customfield_NNNNN`), then the other external-id names, then a label `warp:<id>`. One exact match is stored. Summary search waits for `--yes`. `/warp-jira-match` fetches a bounded candidate list (`summary ~` is fuzzy in Jira) and matches locally. |
| `getJiraProjectIssueTypesMetadata` | Field catalog. Used to turn `jiraExternalIdField` or `External ID` into a `cf[NNNNN]` clause, and to find the External ID field before a write. |
| `getJiraIssueRemoteIssueLinks` | Remote-link ids, after the external-id field and the label. |
| `getJiraIssueEditmeta` | Whether the External ID field is editable on that issue. A missing field is remembered and the fallback runs. A read-only field is skipped. |
| `getJiraScreen` | Read a company-managed screen. Does not create a field. |
| `updateJiraScreen` | Add an existing field to a screen. Does not create a field. |
| `createJiraIssueRemoteIssueLink` | Add a remote link with globalId `warp:<id>` when the fallback is `remote-link`. |
| `createJiraField` | Probe only. Not in the Rovo catalog. Also `createCustomField` and `createJiraCustomField`. |
| `editJiraIssue` | Write the plan id into the External ID field, or add `warp:<id>` with `update.labels` add. `/warp-jira-external-id --apply --yes`, `/warp-jira-match --write-external-id --yes`, and `jira_sync.py external-id --yes`. `/warp-scan` and `/warp-jira-check` queue the same write when `jiraWriteExternalId` is true, without `--yes`. This is a write and needs the connector permission. No comment is added. Do not pass `fields.labels`. |

Those names, and the Slack, Teams, and GitHub names below, are defined once in `scripts/mcp_tools.py`. `/warp-allow-notify --with-jira` allows every name in that Jira list, including `editJiraIssue`. That tool writes the External ID field. Scan and claim call it only when `jiraWriteExternalId` is true. `/warp-allow-notify` reads the module. It does not invent a second list.

Key resolution, in order: a key already on the plan or in `.warp/jira-map.json`, then `jiraExternalIdField` and the other external-id names, then label `warp:<id>`, then a remote link, then `/warp-jira-match` for the summary. `/warp-jira-view` reads one issue. `/warp-jira-check` prints what was recorded. Details are in [GUIDE.md](GUIDE.md).

`pick` matches the transition name, then the target status name, then (for In Progress and Done only) the status category. QA Ready is name-only. Never send a fixed transition id. An issue already in progress is left alone, and a Done issue is not reopened.

Who moves the issue:

| When | Who | What |
|---|---|---|
| Scan | `scan.py` | Stores a Jira export `key`, a plan `jiraKey`, a map-file key, or one exact external-id, label, or remote-link match. A plan id such as `WV-01` stays unmapped until that match. No transition. |
| Claim | dispatch or the Shuttle, from the `jira:` lines `beam.py set` prints | In Progress, plus a Jira comment (started, shuttle, branch). |
| PR opened | Shuttle, after `beam.py set --pr` | Jira comment with the link. Connected mode also comments on the pull request (ticket and Jira key). Local mode does not. |
| Bugbot / CI | Reed, after `beam.py set --bugbot` / `--ci` | Jira comment, and a pull-request comment in connected mode. |
| Waiting (sizes not in `autoMergeSizes`, `autoMerge` false) | Reed, on `awaiting_approval` after Bugbot pass and CI green | QA Ready (`jiraQaReadyStatus`) and a comment: Bugbot clean, ready for manual review, plus `warp:proceed <id>`. |
| Merge, auto (`autoMerge` true) | The orchestrator. Connected: `provider.py merge-pr`. A merge queue (`mergeQueue` or a GitHub ruleset) enqueues and does not mark the ticket merged. A direct merge that branch protection rejects is not marked merged. Local: `provider.py merge-local`, which sets merged only when the squash commit lands. | Done (`jiraDoneStatus`) and a merged comment (PR link, sha, local or connected) after a real merge. |
| Merge, manual | The orchestrator, after `warp:proceed` or a provider approval. Connected: `provider.py merge-pr`, then `beam.py set --status merged --sha` only when the provider reports the pull request merged. Local: `provider.py merge-local`. | Done (`jiraDoneStatus`) and a merged comment, unless `jiraDoneOnManualMerge` is false. The set prints one post-merge MUST DO. |
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
- Read CI and approvals. A GitHub approving review, or a Bitbucket participant status APPROVED, is the merge signal for a size not in `autoMergeSizes`, along with `warp:proceed <id>`.
- Comment Bugbot evidence and acceptance-criterion ids.
- Squash-merge only from Reed, only on the policy in MERGE-POLICY.md.

Warp does not install `gh`, create SSH keys, or connect an app. If the connector and the CLI are both missing or error, do not raise an alarm and do not scrape credentials: run `scripts/provider.py note` and take the local path for that ticket.

Local-only: stay on the ticket branch. Reed squash-merges it into `baseBranch` with `scripts/provider.py merge-local`, which also writes `.warp/outbox.md` and asks Herald to post that it was local. A conflict leaves the branches untouched.

## Slack and Teams

`messenger: slack | teams | both`.

There is no webhook or token in Warp. Herald posts through these connected servers only. `/warp-init` and `/warp-scan` write `.warp/notify-post.json` and tell Herald to post it; with no channel or no server the text goes to `.warp/outbox.md`. `notify: quiet` skips both. Warp does not create channels. The shared `warp` channel must exist, and the connected app must be able to post to it.

Every message starts with the header `Warp | <repo> / <project>` because one channel serves many repos. Build messages with `scripts/herald_fmt.py` so the format stays the same; see `agents/herald.md`.

Herald posts with `slack_send_message` or `slack_post_message`. The one `warp-listen` listener reads the channel with `slack_read_channel`, `slack_read_thread`, and `slack_search_channels`. Teams uses `send_channel_message` or `teams_send_message`, and `teams_read_channel`, `teams_read_thread`, `teams_search_channels`. Those names are `scripts/mcp_tools.py`. `/warp-init` writes them into the project allowlist, including the Jira tools. If the prompt names a different tool, add `server:tool` to `notifyAllow` and run `/warp-allow-notify` again. Do not start a second reader.

The file that skips the IDE prompt is `.cursor/permissions.json` (`mcpAllowlist`), and it applies when Run Mode is Auto-review, Allowlist, or Run Everything. The server id in the dialog is often `user-slack` or `plugin-slack-slack`, not the mcp.json key `slack`. The CLI uses a different file (`.cursor/cli.json`, entries `Mcp(server:tool)`). A local `beforeMCPExecution` hook is also installed; a hook `allow` does not currently skip the prompt, and plugin hooks do not run on cloud runners. Cloud agents do not ask for approval. They run `scripts/mcp_allow.py` and call a tool only when it prints `allow`. See the `warp-allow-notify` skill.

Post alarms, approval requests, gate flips, pause/resume, digests, and the one-line recovery notices. Inbound commands are read by one listener for the beam, not one per awaiting_approval ticket, Shuttle, or Reed. The slot is `listener.state`, `listener.agentId`, optional `listener.pid`, and `listener.lastSeenAt` (the heartbeat from `inbound.py heartbeat`). `/warp-start` and `/warp-resume` launch it. `/warp-pause` and `/warp-stop` stop it. It must not keep reading while paused or stopped. Cursor cannot deliver a Slack message into an ended turn. There is no webhook, and plugin hooks do not run on cloud runners. `beam.py watchdog` runs on every Warp tick and on start and resume. The listener is a Subagent of the parent. Start one subagent per ticket in its own worktree, in parallel up to maxAgents. Optional `launch: agent` prints an IMPLEMENT prompt for one new Agent. The listener is not a separate Agent. A running flag from the previous turn does not block the next tick. Close the window, open a new one, `/warp-start`. State comes from main plus ticket folders. A fresh Shuttle heartbeat is left alone. A Shuttle is dead when `lastSeenAt` is older than `staleMinutes`. Pause and stop do not replace a Shuttle. A dead Shuttle becomes `recovering` on the same branch, pull request, and locks, and exactly one new Shuttle starts. Past `maxRecoveries` (default 3) the ticket is `alarm` / `worker-died` and no Shuttle starts. The parent tick repairs `lock-escape` by widening that ticket's locks to the paths that escaped, one ticket at a time. The listener does not start the repair Agent. Herald posts the repair lines from `alarm_repair.py` (started, including the paths added; failed and the next ticket taken; given up). Other alarm reasons are not repaired. Pause and stop do not. Each recognized command is acknowledged in the channel before it runs:

- `warp:proceed HOS-14` or `warp:proceed L-01` (plan id or Jira key). Ack, then merge that one waiting ticket. A bad id merges nothing else.
- `warp:retry L-01`
- `warp:pause`
- `warp:resume`
- `warp:stop`
- `warp:start`
- `warp:status`

The proceed ack looks like `Received warp:proceed XV-01. Merging and moving Jira to Done.` Unknown `warp:` lines ack the accepted forms. A PR comment with the same verb counts. Record the source.

## Dispatch, repair, and upgrade

The orchestrator is the only merger. The cap is `maxAgents`. A project may set 18. Ready means every blocker is merged on the base branch and no lock overlaps an in-flight ticket, including a parent folder. The slot frees when the GitHub or Bitbucket check rollup is green. Locks stay until merge or park. The merge queue is serial. `mergeQueue` (default false), or a GitHub merge queue, enqueues the pull request. `checkCommand` empty means Bugbot and CI. `make ci` is kept as the full check command. A red result is stored on `pr.check` and the ticket is sent back (`send-back <id> fix` and a `start` line). The third red parks. The name alone is not a red check. `appendOnlyPaths` conflicts keep both sides. Sizes outside `autoMergeSizes` still wait for `/warp-proceed` or `warp:proceed`. A red base branch stops merging. Restart classifies merged, in flight, and pending from git.

One checkout per ticket. Cloud is one cloud agent. Local is one git worktree. `state_commit.py commit` commits the beam, journal, STATUS, and BOARD and strips tokens, cost, API keys, and webhook URLs. `.warp/config.yaml` stays gitignored. On a cloud runner, `/warp-start` refuses when `transitionJiraIssue`, `addOrEditJiraIssueComment`, or `slack_send_message` is missing from the MCP allow list. `--force` starts anyway. A local runner does not block.

The parent tick repairs `lock-escape` only, one ticket at a time, every `alarmRepairMinutes` (default 15). The listener does not start the repair Agent. The repair widens that ticket's locks to the escaped paths. It waits when an in-flight ticket holds a path. Past `maxAlarmRepairs` (default 3) the alarm stays. `bugbot-failed`, `worker-died`, `stuck`, `ci-red`, and `gate-red` are left alone. Pause and stop do not repair.

A pending or stale-red gate clears when every member is merged or done and no check is actually red, then the tick runs. G1 with members P-002, P-003, P-004, and P-018 stores `members merged: P-002, P-003, P-004, P-018`. Herald posts `G1 pending cleared. Members merged. Tick ran.`

`/warp-upgrade` replaces `.cursor/plugins/warp`. Run it in the repo that has Warp installed. When the source is a git checkout, it fetches the default branch. If the fetch fails, it prints `fetch failed` and `keeping the installed copy`. It does not overwrite `.warp/config.yaml` or the beam. It prints `Warp vX.Y.Z`. Reload Cursor, then confirm with `/warp-version`. `/warp-init` does not upgrade an existing copy. This command does. `--force` is not an upgrade flag. The flags are `--root` and `--source`. Init and upgrade write `.cursor/commands/warp-upgrade.md` from `commands/warp-upgrade.md` so a reload lists `/warp-upgrade` when the plugin command index still omits that path.

## Bugbot

Use Cursor Bugbot on the PR. Reed stores pass/fail and the evidence string. A pass with no AC ids is not a pass.
