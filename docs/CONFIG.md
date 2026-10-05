# Configuration

One file: `.warp/config.yaml`. `/warp-init` copies `assets/config.example.yaml` on a fresh install, including the comments. On a later run it appends any key the file does not have yet, with that key's default and comments, and it prints the names it added. It does not change or remove a value or a comment you already have. A line that starts with `#` is not a key, so the commented local-only hints (`# pushMerge: false`, `# runner: local`, `# baseBranch: "develop"`, `# gitProvider: github`, `# ghCli: false`) do not count as the setting and do not block adding the real line.

An old file that never got the new keys still behaves as the defaults in the table. `jira_sync.py` fills a missing Jira key from its defaults, then the beam copy, then the yaml. `herald_fmt.read_config` does the same for the keys it knows, with one exception: a missing `slackChannel` or `teamsChannel` is empty, so notify writes `.warp/outbox.md` until `/warp-init` writes `warp`. Re-run init when you want the keys written down.

Agents re-read the yaml. The beam keeps a copy taken at scan time; change the yaml, then restart, so the next tick picks it up.

`/warp-init` also writes `.warp/version`, one line, the version of the project plugin copy. That file is state, not a config key, so it is not in the example and init does not treat it as one. `/warp-version` prints it next to the source copy.

| Key | Default | Meaning |
|---|---|---|
| stateDir | `.warp` | Where beam, status, and exports are written. Gitignore this. Absolute path keeps it outside the repo. |
| model | `claude-sonnet-5-5-high` | Coding slug (Claude Sonnet 5.5 High). Must match the Cursor model picker. Overridden by this file. |
| maxAgents | `18` | Concurrent Shuttles. Only cap. |
| autoMergeSizes | `S, M` | Sizes that merge without a human approval. |
| messenger | `both` | `slack`, `teams`, or `both`. |
| notify | `verbose` | `verbose` posts every claim and tick, and the `/warp-init` and `/warp-scan` messages. `quiet` posts alarms, approval waits, a red gate, and pause/stop. A `warp:status` request is always answered. |
| runner | `cloud` | `cloud` uses a Cursor cloud agent VM. `local` uses this machine. |
| jiraProject | empty | Jira project key, for example `WAR`. A plan id is sent to Jira only when its prefix matches this or `jiraKeyPrefixes`. `WV-01` is not `WAR-1`. `/warp-init` and `/warp-scan` write it when one prefix is clear from the plan, branches, or recent commits, or when Jira shows one matching project. A value already set is left alone. |
| jiraTransition | `true` | On claim, move the Jira issue to `jiraInProgressStatus`. Only tickets with a Jira key. No Jira connector, or no matching transition: a note goes to `.warp/outbox.md` and the claim goes on. |
| jiraInProgressStatus | `In Progress` | Target status name. Warp reads the transitions Jira offers and matches the transition name, then the target status name, then an in-progress status category. It never uses a fixed transition id. |
| jiraQaReadyStatus | `QA Ready` | Manual path (ticket `autoMerge` false, L and XL by default): Jira moves here when the ticket is awaiting review. Warp does not move it to Done afterwards. |
| jiraDoneStatus | `Done` | Auto-merge path (`autoMerge` true, S and M by default): Jira moves here after the merge, connected or local. |
| jiraRestoreOnRelease | `false` | `true`: when a claim is released back to `queued`, move the issue back to the status it had before Warp moved it. `false`: leave it where it is. |
| bugbotRequired | `true` | Reed will not merge without a Bugbot pass. |
| maxFixAttempts | `3` | Then the ticket alarms. |
| stuckAfterMinutes | `90` | No beam update in this window, and not waiting on approval, raises stuck. |
| respectMergeWindows | `false` | If true, new claims wait for a window. |
| mergeWindows | `08:30, 13:00, 17:00` | Digest times, or claim gates if the flag above is true. |
| pollSeconds | `300` | How often a running loop reconciles PRs. |
| jiraKeyPrefixes | empty | Extra project prefixes, comma-separated, that confirm a Jira key besides `jiraProject`. When a project is detected and this is empty, it is set to that project. |
| jiraKeyMap | empty | Optional map of plan id to Jira issue key, for example `{"WV-01": "WAR-1"}`. `.warp/jira-map.json` is merged on top and wins. |
| jiraExternalIdField | `externalId` | Field name or `customfield_NNNNN` compared to the plan id. Scan and claim try this first, then External ID, External Id, ExternalId, External Key, Plan ID, and Ticket ID. A `warp:<id>` label and a remote-link id are next. One exact hit is stored. A summary match waits for confirmation. Two matches are reported. |
| jiraWriteExternalId | `false` | When true, `/warp-scan` and `/warp-jira-check` print a MUST DO block that writes the plan id for every mapped ticket whose Jira value is not yet confirmed equal to the plan id. The flag is the consent, so `--yes` is not required. A key whose source is `external` is already equal and is skipped. A ticket with no confirmed key is skipped. When false, scan and catchup never queue that write. `/warp-jira-external-id --apply --yes` and `/warp-jira-match --write-external-id --yes` still can. A different non-empty value is left alone unless `--force-external-id`. The edit does not add a comment. `jira.externalIdWritten` skips a second write and records the method (`field`, `label`, or `remote-link`). |
| jiraExternalIdFieldName | `External ID` | Name of the single-line text field to create when creation is allowed. |
| jiraCreateExternalIdField | `false` | When true, a missing External ID field makes scan and `/warp-jira-external-id` probe for `createJiraField`, `createCustomField`, or `createJiraCustomField`. Creating a field is a site-wide admin change. The Rovo catalog does not include that tool. A missing tool or a denied call does not fail the command. `/warp-jira-external-id --create-field --yes` is the same consent when this key is false. |
| jiraExternalIdFallback | `label` | Used when the External ID field is missing. `label` adds `warp:<id>` with `editJiraIssue` `update.labels` add and does not remove other labels. `remote-link` calls `createJiraIssueRemoteIssueLink` with globalId `warp:<id>`. `none` writes nothing. `.warp/jira-field.json` remembers the miss so the next scan prints one summary. `--recheck` looks again. |
| jiraMcp | `atlassian` | Connected Jira server name. This is not a tool name. The tools in `scripts/mcp_tools.py` are `getAccessibleAtlassianResources`, `getJiraIssue`, `getTransitionsForJiraIssue` (or `listJiraIssueTransitions`), `transitionJiraIssue`, `addOrEditJiraIssueComment` (or `addCommentToJiraIssue`), `searchJiraIssuesUsingJql`, `getJiraProjectIssueTypesMetadata`, `getJiraIssueRemoteIssueLinks`, `getVisibleJiraProjects`, `editJiraIssue`, `getJiraIssueEditmeta`, `getJiraScreen`, `updateJiraScreen`, `createJiraIssueRemoteIssueLink`, and the create-field probes `createJiraField`, `createCustomField`, and `createJiraCustomField` (not in the Rovo catalog; allowed so a future tool is not blocked). |
| jiraSite | empty | Site URL passed as `cloudId` when you have more than one Atlassian site. Empty: the agent calls `getAccessibleAtlassianResources`. |
| gitProvider | `auto` | `auto`, `github`, or `bitbucket`. `auto` reads the `origin` host. `/warp-init` writes the one it finds and leaves a custom value alone. Other hosts run local-only. |
| githubMcp | `github` | Connected GitHub server name. |
| bitbucketMcp | `bitbucket` | Connected Bitbucket server name. |
| ghCli | `true` | GitHub only: also try the `gh` CLI when it is installed and logged in. Warp never installs it or logs in. |
| pushMerge | `true` | `true` pushes and opens pull requests. `false` is local-only: no push, no pull request, no provider call; Reed merges the ticket branch into `baseBranch` with local git. No `origin` remote also means local-only, and `/warp-init` sets this to `false` in that case. |
| baseBranch | empty | Branch tickets start from and merge into. Empty uses origin's default, then `main` or `master`. |
| slackMcp | `slack` | Connected Slack server name. |
| teamsMcp | `teams` | Connected Teams server name. |
| slackChannel | `warp` | One shared channel for every repo, to post into and to watch for `warp:status`. Lowercase: Slack channel names are lowercase, so uppercase letters here are lowercased when posting, and `/warp-init` warns. Warp does not create it. `/warp-init` sets an empty value (or the old `Warp` default) to `warp`; any other value you set is kept. |
| teamsChannel | `warp` | Same, for Teams. Used as written. |
| projectName | empty | Shown after the repo in message headers: `Warp \| repo / project`. Empty uses `jiraProject`, then the workspace folder name. Hidden if it equals the repo name. |
| notifyAllow | empty | Extra `server:tool` pairs for `/warp-allow-notify`. No wildcards. Slack, Teams, Jira, and GitHub tool names Warp already uses come from `scripts/mcp_tools.py`; this list is only for a connector whose tool name is different. `lookupJiraAccountId` is not called. |
| autoAllowTools | `true` | `/warp-init` writes the project MCP allowlist (Slack, Teams, Jira, and the GitHub comment tool when that provider is in use). `false` skips it. `/warp-init --no-allow` skips one run. User-level files stay opt-in. Undo with `/warp-allow-notify --revoke`. |

There is no person cap and no people list.
