# Commands

One reference for the chat commands and the scripts they run. Config keys and defaults are in [CONFIG.md](CONFIG.md). Every script below accepts `?`, `help`, `-h`, and `--help`. `?` is always help, including where a value would go. A bare `help` is help unless it is the value of an option (`--folder help` is a folder name). Quote `?` if the shell expands it.

`<plugin>` is the plugin Cursor loaded (often `.cursor/plugins/warp` after init).

## Quick map

| Chat | Script |
|---|---|
| `/warp-init` | `install.py init` |
| `/warp-scan` | `scan.py scan` |
| `/warp-scan <folder>` | `scan.py scan --folder <folder>` |
| `/warp-start` | `scan.py start` |
| `/warp-pause` | `scan.py pause` |
| `/warp-resume` | `scan.py resume` |
| `/warp-stop` | `scan.py stop` |
| `/warp-status` | `scan.py status` and `beam.py board` |
| `/warp-status-post` | `status_post.py` |
| `/warp-export` | `scan.py export` |
| `/warp-import` | `scan.py import` |
| `/warp-jira-check` | `jira_sync.py verify` and `catchup` |
| `/warp-jira-map` | `jira_sync.py map` |
| `/warp-allow-notify` | `allow_notify.py` |
| `/warp-version` | `version.py` |
| `/warp-uninstall` | `install.py uninstall` |
| `/warp-proceed <id>` | Reed merges a green manual ticket. Not a script flag. |

## /warp-init

```bash
python3 <plugin>/scripts/install.py init
python3 <plugin>/scripts/install.py init --dry-run
python3 <plugin>/scripts/install.py init --channel eng-builds
python3 <plugin>/scripts/install.py ?
```

Idempotent. A second run on a complete install changes nothing.

| Flag | Effect |
|---|---|
| `--root` | Repo root. Default is the git top level, else the current directory. |
| `--channel NAME` | Channel for a fresh config, or for an empty channel. Lowercased. Letters, digits, `-`, `_`, max 80. |
| `--dry-run` | Print the steps. Write nothing. |

Steps: create `.cursor/plugins` and `.warp` if missing; copy plugin files that are not already there (existing files are never overwritten); copy `assets/config.example.yaml` on a fresh config, or append missing keys with their defaults and comments; set an empty `slackChannel` or `teamsChannel` (or the old `Warp` default) to `warp`; detect `gitProvider` from `origin` unless it is already `github` or `bitbucket`; set `pushMerge: false` when there is no remote; append the gitignore snippet once; write `.warp/version` from the project plugin copy.

If `.cursor/plugins/warp` is older than the plugin this command ran from, it prints `plugin is vOLD, repo copy is vNEW: run /warp-uninstall then /warp-init` and does not replace plugin files.

Commented lines in the example (`# pushMerge: false`, `# runner: local`, `# baseBranch: "develop"`, `# gitProvider: github`, `# ghCli: false`) are hints. A line that starts with `#` is not a key.

When init changes something and `notify` is `verbose`, it writes `.warp/notify-post.json`. The footer starts with `Warp v<version>`.

## /warp-scan

```bash
python3 <plugin>/scripts/scan.py scan --root . --out .warp/beam.json
python3 <plugin>/scripts/scan.py scan --folder HOS/spec
python3 <plugin>/scripts/scan.py scan --folder spec
```

| Flag | Default | Effect |
|---|---|---|
| `--root` | `.` | Repo root. |
| `--folder` | whole repo | Path or name under the root. |
| `--out` | `.warp/beam.json` | Beam path. |
| `--max-agents` | `18` | Stored on the beam. The live cap is `maxAgents` in config. |
| `--model` | `claude-sonnet-5-5-high` | Stored on the beam. The live slug is `model` in config. |

Folder rules:

| Argument | Result |
|---|---|
| An existing path under the repo | Used as given. |
| A name that matches one folder | That folder. |
| A name that matches several folders, only one with plan files | That folder, and a note listing the others. |
| A name that matches several folders, none or more than one with plan files | Nothing is scanned. The matches are listed (exit 3). |
| No such folder, or a path outside the repo | Error. Nothing is written. |
| A folder with no plan files | `no plan found` (exit 2). |

With no folder, plans in more than one folder are reported and the richest plan is used.

Looks for `WARP_PLAN.json`, `schedule.json`, `CURSOR_PLAN.md`, a Jira ticket JSON export, or a markdown table with `id` and `deps`. Writes `.warp/beam.json` and `.warp/scan.json`. `runState` is `stopped`. A successful scan writes `.warp/notify-post.json` with the header `Warp | <repo> / <project>` and a footer that starts with `Warp v<version>`. `notify: quiet` posts nothing. No channel, or no server: the text goes to `.warp/outbox.md`. A failed scan posts nothing.

Jira keys already on the plan, in a Jira export, or in `.warp/jira-map.json` / `jiraKeyMap` are stored during the scan. The script then prints a line like `12 tickets: 9 keyed, 3 need mapping`. For the ones still open it writes `.warp/jira-resolve.json`. When Jira is connected, the agent searches `jiraExternalIdField` (default `externalId`) and `jira_sync.py resolve --apply` stores an exact single match. That is part of `/warp-scan`, not a separate command. `/warp-jira-map` is only for tickets that stay unmapped or ambiguous.

Other `scan.py` subcommands: `export` (`--beam`, `--out`), `import` (`--plan`, `--beam`, `--keep-status`), `status` (`--beam`), `bundle` (`--beam`, `--out` default `.warp/warp-review.zip`), `start`, `stop`, `pause`, `resume` (`--beam`, `--reason`).

## /warp-uninstall

```bash
python3 <plugin>/scripts/install.py uninstall
python3 <plugin>/scripts/install.py uninstall --remove-gitignore
python3 <plugin>/scripts/install.py uninstall --remove-gitignore --yes
```

Without `--yes` it only lists. With `--yes` it deletes `.cursor/plugins/warp` and `.warp/`, and the project allow-notify entries recorded in `.cursor/warp-allow.json`. `--remove-gitignore` removes only the snippet init added. A hand-written `.warp/` line is left alone. It does not touch product code, branches, pull requests, a `stateDir` outside the repo, a user-level plugin, or `~/.cursor` allow-notify files.

## Messages

Herald posts through Slack or Teams MCP. There is no webhook. The header from `scripts/herald_fmt.py` is `Warp | <repo> / <project>`. `<project>` is `projectName`, else `jiraProject`, else the folder name, and it is omitted when it equals the repo. Init and scan footers start with `Warp v<version>`.

`notify: verbose` posts init, scan, claims, and ticks. `notify: quiet` posts alarms, approval waits, a red gate, and pause/stop. `warp:status` is always answered. `messenger` is `slack`, `teams`, or `both`.

If the channel is empty or the server is missing, the text is appended to `.warp/outbox.md` and the command still succeeds.

## GitHub, Bitbucket, and local-only

`scripts/provider.py resolve` reads `gitProvider` (`auto`, `github`, `bitbucket`). `auto` uses the `origin` host. `/warp-init` writes the host it finds and leaves a custom value alone. Another host, no `origin`, or `pushMerge: false` is local-only: no push, no pull request, no provider call. Reed merges the ticket branch into `baseBranch` with `provider.py merge-local`.

| Key | Default | Role |
|---|---|---|
| `githubMcp` | `github` | GitHub server name. |
| `bitbucketMcp` | `bitbucket` | Bitbucket server name. |
| `ghCli` | `true` | GitHub only: try `gh` when it is already installed and logged in. Warp never installs it. |
| `pushMerge` | `true` | `false` forces local-only. |
| `baseBranch` | empty | Branch tickets start from and merge into. Empty uses origin's default, then `main` or `master`. |
| `runner` | `cloud` | `cloud` is a Cursor cloud agent VM. `local` is this machine. |

`provider.py merge-local --id --branch` squash-merges locally. `provider.py note --reason` writes the outbox line when the run went local.

## Jira

Tool names are in `scripts/mcp_tools.py`. Server name is `jiraMcp` (default `atlassian`), not a tool.

| Tool | Use |
|---|---|
| `getAccessibleAtlassianResources` | `cloudId`, unless `jiraSite` is set |
| `getJiraIssue` | current status |
| `getTransitionsForJiraIssue` | transitions offered now. Some servers call this `listJiraIssueTransitions` |
| `transitionJiraIssue` | the id `jira_sync.py pick` chose |
| `addOrEditJiraIssueComment` | comment, argument `commentBody`. Older servers call this `addCommentToJiraIssue` |
| `searchJiraIssuesUsingJql` | Scan and claim: exact `externalId` match, stored with no confirm step. `/warp-jira-map --search` is the optional summary search, and those proposals wait for `--yes` |

| When | Jira | Pull request |
|---|---|---|
| Claim | In Progress (`jiraInProgressStatus`) and a comment, if `jiraTransition` is true and the ticket has a key | none |
| PR opened | comment with the link | connected mode only: GitHub `add_issue_comment` or `gh pr comment`. Bitbucket uses the comment tool on `bitbucketMcp` (unnamed here). |
| Bugbot / CI | comment | connected mode only |
| Waiting (`autoMerge` false, L and XL by default) | QA Ready (`jiraQaReadyStatus`) and a comment | connected mode only |
| Merged, auto | Done (`jiraDoneStatus`) and a comment | connected mode only |
| Merged, manual | comment only. Stays at QA Ready | connected mode only |
| Release to queued | no move, unless `jiraRestoreOnRelease` is true | none |
| Pause / stop | no change | none |

A plan id is not a Jira issue key. `WV-01` is not sent as `WV-01`. A key is confirmed when it is on the plan or export, in the map file, its project prefix matches `jiraProject` or `jiraKeyPrefixes`, or Jira has exactly one issue whose external id equals the plan id. Otherwise `jiraKey` stays empty and the ticket is `needs mapping`. `/warp-scan` prints `N tickets: K keyed, U need mapping` and, when Jira is connected, resolves the external-id matches before that summary is final. A claim on an unmapped ticket tries that search first. Only a miss or an ambiguous result writes `.warp/outbox.md` and a Herald payload, and it does not call `transitionJiraIssue`.

`/warp-jira-check` runs `jira_sync.py verify` (print only) and `catchup` (writes `.warp/jira-todo.json` for tickets that have a key). `catchup --write` stores an inferred key only when the prefix matches. The script does not call Jira. After a key is mapped, `catchup` asks for the transitions and comments the current beam status still owes (In Progress for a claim, QA Ready or Done for a merge). It does not move the ticket backwards.

## /warp-jira-map

Not a required step. `/warp-scan` and the first claim already store a key that is on the plan, in the map file, or an exact single external-id match in Jira. Use this command to review the list, to set a leftover or an ambiguous ticket, or to override a key.

```bash
python3 <plugin>/scripts/jira_sync.py map
python3 <plugin>/scripts/jira_sync.py map --set WV-01=WAR-1
python3 <plugin>/scripts/jira_sync.py map --import mappings.csv
python3 <plugin>/scripts/jira_sync.py map --search
python3 <plugin>/scripts/jira_sync.py map --match results.json --yes
python3 <plugin>/scripts/beam.py set --beam .warp/beam.json --id WV-01 --jira WAR-1
```

| Flag | Effect |
|---|---|
| (none) | Print each ticket id and its key, or `unmapped`. |
| `--set ID=KEY` | Store one pair on the ticket and in `.warp/jira-map.json`. Repeatable. |
| `--import FILE` | CSV (`id,jiraKey`), JSON (`{"WV-01": "WAR-1"}`), or a markdown table with `id` and `jira key`. |
| `--search` | Write `.warp/jira-search.json` with JQL for `searchJiraIssuesUsingJql`. Changes nothing. |
| `--match FILE` | Propose pairs whose Jira summary equals the ticket summary. |
| `--yes` | Store the proposals from `--match`. Without it, nothing is written. |
| `--force` | Store a key whose prefix is not in `jiraProject` or `jiraKeyPrefixes`. |

`beam.py set --jira KEY` is the same check. It rejects a prefix that is not configured unless `--force`. A rescan keeps a key set this way, and reapplies `.warp/jira-map.json` and `jiraKeyMap`.

Plans can carry the real key: `schedule.json` field `jiraKey`, a markdown line `Jira: WAR-1`, a table column `Jira Key` or `jiraKey`, or `[WAR-1]` on the ticket heading.

```bash
python3 <plugin>/scripts/jira_sync.py resolve --apply results.json
python3 <plugin>/scripts/jira_sync.py verify --beam .warp/beam.json
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json
python3 <plugin>/scripts/jira_sync.py catchup --write --id T-9
python3 <plugin>/scripts/jira_sync.py pick --target "In Progress" --transitions-file transitions.json
python3 <plugin>/scripts/jira_sync.py record --id T-9 --event claim --result moved
python3 <plugin>/scripts/jira_sync.py record-comment --id T-9 --where jira --event claim --comment-id 10001
```

`plan --event` is `claim`, `release`, `qa-ready`, or `done`. `record --result` is `moved`, `already`, `skipped`, `unavailable`, `no-transition`, or `failed`.

## /warp-allow-notify

Full behavior is also in the README. Summary:

```bash
python3 <plugin>/scripts/allow_notify.py ?
python3 <plugin>/scripts/allow_notify.py --dry-run
python3 <plugin>/scripts/allow_notify.py
python3 <plugin>/scripts/allow_notify.py --with-jira --with-git
python3 <plugin>/scripts/allow_notify.py --user --yes
python3 <plugin>/scripts/allow_notify.py --revoke
```

| Flag | Effect |
|---|---|
| (default) | Project files. Slack and Teams post tools only. |
| `--dry-run` | Unified diff. Writes nothing, including no backup. |
| `--user` | `~/.cursor/permissions.json`, `cli-config.json`, and `hooks.json`. Refuses to write without `--yes`. `--dry-run` does not need `--yes`. |
| `--with-jira` | Also the seven Atlassian tools above. |
| `--with-git` | Also GitHub `add_issue_comment`. No Bitbucket name. |
| `--revoke` | Remove only what `.cursor/warp-allow.json` recorded. |
| `--root` | Repo root. |
| `--cursor-home` | Stand-in for `~/.cursor`. Tests. |

Default tool names: `slack_post_message`, `slack_send_message`, `send_channel_message`, `teams_send_message`. Extras go in `notifyAllow` as `server:tool`. The Run prompt shows the tool name when it is different.

IDE: `permissions.json` `mcpAllowlist` skips the prompt when Run Mode is Auto-review, Allowlist, or Run Everything. Ask Every Time ignores it. The key replaces the in-app MCP allowlist. CLI entries do not change the IDE button. The hook returns allow for this list only; a hook allow does not currently skip the prompt. Cloud agents do not prompt.

A second run with the same flags writes nothing. Changed files get a `.bak`. `/warp-uninstall` removes the project entries only.

## /warp-version

```bash
python3 <plugin>/scripts/version.py
python3 scripts/bump_version.py
python3 scripts/bump_version.py --minor
python3 scripts/bump_version.py --major
python3 scripts/check_version.py
python3 scripts/check_version.py --against origin/main
```

`VERSION` is the source of truth. The manifest must match, and `CHANGELOG.md` must have a `## <version>` heading. Every pull request bumps the patch. `bump_version.py` updates all three and inserts a stub (`--note` sets the bullet). CI fails when the pull request version is not newer than `main`.

## Beam

`beam.py` subcommands: `ingest`, `ready`, `set`, `spend`, `gate`, `pause`, `resume`, `board`, `check`, `eta`. `set` takes `--status`, `--agent`, `--branch`, `--jira`, `--pr`, `--sha`, `--via local|connected`, `--bugbot`, `--ci`, `--alarm`, `--attempts`. Do not hand-edit `beam.json`.

## Channel verbs

`warp:proceed <id>`, `warp:retry <id>`, `warp:pause`, `warp:resume`, `warp:stop`, `warp:start`, `warp:status`. There is no `warp:hold`.
