# Configuration

One file: `.warp/config.yaml`, copied from `assets/config.example.yaml` by `/warp-init`. Agents re-read it. The beam keeps a copy taken at scan time; change the yaml, then restart, so the next tick picks it up.

| Key | Default | Meaning |
|---|---|---|
| stateDir | `.warp` | Where beam, status, and exports are written. Gitignore this. Absolute path keeps it outside the repo. |
| model | `claude-sonnet-5.5` | Coding slug. Must match the Cursor picker. |
| maxAgents | `18` | Concurrent Shuttles. Only cap. |
| autoMergeSizes | `S, M` | Sizes that merge without a human approval. |
| messenger | `both` | `slack`, `teams`, or `both`. |
| notify | `verbose` | `verbose` posts every claim and tick, and the `/warp-init` and `/warp-scan` messages. `quiet` posts alarms and stops only. |
| runner | `cloud` | `cloud` uses a Cursor cloud agent VM. `local` uses this machine. |
| jiraProject | empty | Jira project key, if you want it pinned. |
| jiraTransition | `true` | On claim, move the Jira issue to `jiraInProgressStatus`. Only tickets with a Jira key. No Jira connector, or no matching transition: a note goes to `.warp/outbox.md` and the claim goes on. |
| jiraInProgressStatus | `In Progress` | Target status name. Warp reads the transitions Jira offers and matches the transition name, then the target status name, then an in-progress status category. It never uses a fixed transition id. |
| jiraRestoreOnRelease | `false` | `true`: when a claim is released back to `queued`, move the issue back to the status it had before Warp moved it. `false`: leave it where it is. |
| bugbotRequired | `true` | Reed will not merge without a Bugbot pass. |
| maxFixAttempts | `3` | Then the ticket alarms. |
| stuckAfterMinutes | `90` | No beam update in this window, and not waiting on approval, raises stuck. |
| respectMergeWindows | `false` | If true, new claims wait for a window. |
| mergeWindows | `08:30, 13:00, 17:00` | Digest times, or claim gates if the flag above is true. |
| pollSeconds | `300` | How often a running loop reconciles PRs. |
| jiraMcp | `atlassian` | Connected Jira server name. |
| bitbucketMcp | `bitbucket` | Connected Bitbucket server name. |
| slackMcp | `slack` | Connected Slack server name. |
| teamsMcp | `teams` | Connected Teams server name. |
| slackChannel | `warp` | One shared channel for every repo, to post into and to watch for `warp:status`. Lowercase: Slack channel names are lowercase, so uppercase letters here are lowercased when posting, and `/warp-init` warns. Warp does not create it. `/warp-init` sets an empty value (or the old `Warp` default) to `warp`; any other value you set is kept. |
| teamsChannel | `warp` | Same, for Teams. Used as written. |
| projectName | empty | Shown after the repo in message headers: `Warp \| repo / project`. Empty uses `jiraProject`, then the workspace folder name. Hidden if it equals the repo name. |

There is no person cap and no people list.
