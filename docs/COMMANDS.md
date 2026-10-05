# Commands

One reference for the chat commands and the scripts they run. Config keys and defaults are in [CONFIG.md](CONFIG.md). Every script below accepts `?`, `help`, `-h`, and `--help`. `?` is always help, including where a value would go. A bare `help` is help unless it is the value of an option (`--folder help` is a folder name). Quote `?` if the shell expands it.

`<plugin>` is the plugin Cursor loaded (often `.cursor/plugins/warp` after init).

## Quick map

| Chat | What it does | Script and common flags |
|---|---|---|
| `/warp-init` | Copy the plugin and backfill missing config keys | `install.py init` `--channel` `--dry-run` |
| `/warp-scan` | Read a plan into the beam and resolve Jira keys | `scan.py scan` `--folder` |
| `/warp-start` | Start dispatch | `scan.py start` `--reason` |
| `/warp-pause` | Stop new claims | `scan.py pause` `--reason` |
| `/warp-resume` | Resume a paused beam | `scan.py resume` `--reason` |
| `/warp-stop` | Stay stopped until the next start | `scan.py stop` `--reason` |
| `/warp` | One tick of the master loop | the `warp` skill. Not a script flag. |
| `/warp-status` | Rewrite status and the board | `scan.py status`, `beam.py board` |
| `/warp-status-post` | Post that digest | `status_post.py` `--beam` `--out` |
| `/warp-export` | Write the plan files for an outside edit | `scan.py export` `--beam` `--out` |
| `/warp-import` | Replace the plan. The run stays stopped | `scan.py import` `--plan` `--keep-status` |
| `/warp-ingest` | Build the beam from a schedule you name | `beam.py ingest` |
| `/warp-jira-check` | Keyed or unmapped, what is missing, one fix | `jira_sync.py verify`, `catchup` |
| `/warp-jira-view <key-or-id>` | Every field on one issue | `jira_view.py` `--comments` `--links` `--all` `--full` `--verbose` `--json` |
| `/warp-jira-match` | Match summaries. Optionally write External ID | `jira_match.py` `--apply` `--yes` `--write-external-id` |
| `/warp-jira-map` | Review or set plan id to issue key | `jira_sync.py map` `--set` `--import` `--from-jira` |
| `/warp-allow-notify` | Allow specific MCP tools | `allow_notify.py` `--dry-run` `--with-jira` `--with-git` |
| `/warp-proceed <id>` | Merge one green manual ticket | Reed. Not a script flag. |
| `/warp-version` | Installed copy, source copy, `.warp/version` | `version.py` |
| `/warp-uninstall` | Delete `.cursor/plugins/warp` and `.warp/` after you confirm | `install.py uninstall` `--yes` `--remove-gitignore` |

`jira_sync.py` subcommands, each printed by `?`: `verify`, `catchup`, `map`, `resolve`, `project`, `external-id`, `plan`, `pick`, `record`, `record-comment`.

## Run control

`/warp-start`, `/warp-pause`, `/warp-resume`, and `/warp-stop` take `--beam` and `--reason`. Pause keeps in-flight work. Stop stays stopped until the next start. `/warp` is one tick of that loop (reconcile, ready, claim) and does not implement a ticket. `/warp-status` rewrites `.warp/STATUS.md`, `.warp/status.json`, `.warp/BOARD.md`, and `.warp/board.html`. `/warp-status-post` posts the digest. `/warp-export` writes `.warp/WARP_PLAN.md` and `.warp/WARP_PLAN.json`. `/warp-import` replaces the graph from `--plan`, keeps status for ids that still exist (`--keep-status`, the default), and leaves the run stopped. `/warp-ingest` builds a beam from a schedule and does not dispatch. `/warp-proceed <id>` is `warp:proceed` for one green manual ticket. Reed refuses a red pull request.

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

Jira keys already on the plan, in a Jira export, or in `.warp/jira-map.json` / `jiraKeyMap` are stored during the scan. The script then prints a line like `12 tickets: 9 keyed, 3 need mapping`. For the ones still open it writes `.warp/jira-resolve.json`. When Jira is connected, the agent looks up each plan id in this order: `jiraExternalIdField` (a name or `customfield_NNNNN`), then External ID, External Id, ExternalId, External Key, Plan ID, and Ticket ID, then a label `warp:<id>`, then a remote-link id. A Jira import file (`externalId`, `h2. Size`, `h2. Locks`, `h2. Blocked by`, `h2. Acceptance`, labels `size:S`, `auto-merge`, `area:*`) uses `externalId` as the plan id. It is not stored as `jiraKey`. `jira_sync.py resolve --ticket WV-01 --key WAR-1 --issue-id 10001 --cloud-id <cloudId>` records the lookup before any transition. `jira_sync.py resolve --apply` stores an exact single match and where it came from. A summary match is only a proposal. That is part of `/warp-scan`, not a separate command. `/warp-jira-map` is only for tickets that stay unmapped or ambiguous.

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
| `getAccessibleAtlassianResources` | `cloudId`, unless `jiraSite` is set. One site is stored in `jiraSite` |
| `getVisibleJiraProjects` | Project list used to fill an empty `jiraProject` |
| `getJiraIssue` | current status. `/warp-jira-view` asks for every field (`fields ["*all"]`, `expand names`) |
| `getTransitionsForJiraIssue` | transitions offered now. Some servers call this `listJiraIssueTransitions` |
| `transitionJiraIssue` | the id `jira_sync.py pick` chose |
| `addOrEditJiraIssueComment` | comment, argument `commentBody`. Older servers call this `addCommentToJiraIssue` |
| `searchJiraIssuesUsingJql` | Scan and claim: exact external-id match, then a `warp:<id>` label. One hit is stored. Summary search waits for `--yes` |
| `getJiraProjectIssueTypesMetadata` | Field names and ids, so `jiraExternalIdField` or External ID can be queried as `cf[NNNNN]` |
| `getJiraIssueRemoteIssueLinks` | Remote-link ids, checked after the external-id field and the label |
| `editJiraIssue` | Write the plan id into the External ID field. Only `/warp-jira-match --write-external-id` or `jira_sync.py external-id`, and only after `--yes`. Not a comment. |
| `getJiraIssueEditmeta` | Whether that field exists and is editable on the issue. A missing or read-only field is skipped. |

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

A plan id is not a Jira issue key. `WV-01` is not sent as `WV-01`. A key is confirmed when it is on the plan or export, in the map file, its project prefix matches `jiraProject` or `jiraKeyPrefixes`, or Jira has exactly one issue whose external id, `warp:<id>` label, or remote-link id equals the plan id. Otherwise `jiraKey` stays empty and the ticket is `needs mapping`. `/warp-scan` prints `N tickets: K keyed, U need mapping` and, when Jira is connected, resolves those matches before that summary is final. A claim on an unmapped ticket tries that search first. Only a miss or an ambiguous result writes `.warp/outbox.md` and a Herald payload, and it does not call `transitionJiraIssue`. Two Jira issues are reported and neither key is stored. A key set by hand is never replaced.

`/warp-jira-check` runs `jira_sync.py verify` and `catchup`. `verify` with no flags only prints. It does not call Jira, and `needs mapping` on this report does not mean the external-id search already ran. Each ticket has `status: keyed` or `status: unmapped`, `source` (`plan`, `export`, `map`, `manual`, `external`, `label`, `link`, `summary`, `inferred`, or none), and when unmapped a `reason` plus one `fix` command. When every ticket is unmapped the report ends with `why nothing linked:` (`jiraProject is empty`, map keys never copied onto the beam, or no ticket has a Jira key in the plan, map file, or external id). The same line names `jiraMcp`, which must match the Atlassian server Cursor shows. The script cannot tell whether that server is connected.

```bash
python3 <plugin>/scripts/jira_sync.py verify --beam .warp/beam.json
python3 <plugin>/scripts/jira_sync.py verify --link
python3 <plugin>/scripts/jira_sync.py verify --link --dry-run
python3 <plugin>/scripts/jira_sync.py verify --apply results.json
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json
```

| Flag | Effect |
|---|---|
| (none) | Print the report. Write nothing. |
| `--link` | Copy a key already in `.warp/jira-map.json` or `jiraKeyMap` onto a ticket with no confirmed key. Write `.warp/jira-resolve.json` (same JQL as `map --from-jira`). Do not store a Jira search. |
| `--results FILE` | Store one exact match from a saved transcript. `--apply FILE` is the same. |
| `--dry-run` | Print the map copy and the Jira match. Write nothing. |
| `--id ID` | One ticket. |

`catchup` writes `.warp/jira-todo.json` for tickets that have a key, and refuses an active ticket that still has none. `catchup --write` stores an inferred key only when the prefix matches. After a key is mapped, `catchup` asks for the transitions and comments the current beam status still owes (In Progress for a claim, QA Ready or Done for a merge). It does not move the ticket backwards. Linking itself happens when `/warp-scan` or the first claim prints `jira: RESOLVE` and the agent runs that search, or when this check is run with `--link` and then `--apply`.

## /warp-jira-view

Print every field on one Jira issue. The argument is an issue key (`WAR-1`) or an external id / plan id (`WV-01`). The script does not call Jira. It writes `.warp/jira-view.json`. The agent calls `getJiraIssue`, `getTransitionsForJiraIssue`, and `searchJiraIssuesUsingJql` on `jiraMcp`, then `jira_view.py --results` renders the transcript. Those read tools are already included in `/warp-allow-notify --with-jira`.

A value is an issue key when its prefix is `jiraProject` or `jiraKeyPrefixes`, or when those are empty and it matches `PROJECT-123`. Otherwise it is an external id. A key is fetched with `getJiraIssue`. If that call fails, the same text is looked up as an external id. An external id is taken from the beam and `.warp/jira-map.json` first, then the claim lookup: `jiraExternalIdField`, then the other external-id names, then label `warp:<id>`, then a remote link. The search is limited to `jiraProject`. When `jiraProject` is empty, each visible project is probed. Two matches are listed and neither issue is printed.

```bash
python3 <plugin>/scripts/jira_view.py WAR-1
python3 <plugin>/scripts/jira_view.py WV-01
python3 <plugin>/scripts/jira_view.py WV-01 --results view.json
python3 <plugin>/scripts/jira_view.py WAR-1 --results view.json --comments --links --verbose --all --full --json
```

| Flag | Effect |
|---|---|
| (none) | Classify the argument and write `.warp/jira-view.json`. Print the tool calls. Write no issue fields. |
| `--results FILE` | Render a saved transcript. |
| `--comments` | Comment count and the latest comments. |
| `--links` | Issue links and remote links. |
| `--all` | Include empty and null fields. |
| `--full` | Do not truncate long text. |
| `--verbose` | Add the account id on a user field. |
| `--json` | Print the issue as JSON. |
| `--beam` | Beam path. Default `.warp/beam.json`. |

The report starts with `resolved: WAR-1 (external id)` or `resolved: WAR-1 (issue key)`, the status, the external-id field name and id, and `beam:` / `map:` lines for the stored key. `discovered field` is the id to set as `jiraExternalIdField` when the configured name is different. Custom fields print as `External ID (customfield_10050): WV-01`. Description and comments are plain text. Users are the display name. Labels, components, and versions are comma-joined. Dates are ISO. A field whose name is a token, password, or secret is `<redacted>`.

## /warp-jira-match

Link Warp tickets to Jira issues by summary, then optionally write the plan id into the External ID field. Dry-run is the default. The script does not call Jira. `summary ~` in Jira is fuzzy, so the agent fetches a bounded candidate list (50 issues, at most 2 pages) and the script matches locally.

Order for each unmapped ticket: exact summary (case-insensitive, punctuation and whitespace ignored), then the first `--chars` characters (default 60) when both summaries are at least that long, then a token/ratio score at or above `--min-score` (default 0.9) as a proposal only. `--all` includes tickets that already have a key; those keys are not replaced. A manual key is never overwritten. Done and closed issues are skipped unless `--include-done`. An issue already mapped to another ticket is skipped. Two matches, or one issue claimed by two tickets, are listed and not stored.

```bash
python3 <plugin>/scripts/jira_match.py
python3 <plugin>/scripts/jira_match.py --results candidates.json
python3 <plugin>/scripts/jira_match.py --results candidates.json --apply
python3 <plugin>/scripts/jira_match.py --results candidates.json --apply --yes
python3 <plugin>/scripts/jira_match.py --all --chars 60 --min-score 0.9 --include-done
python3 <plugin>/scripts/jira_match.py --write-external-id --yes
python3 <plugin>/scripts/jira_match.py --set-external-id WV-01=WAR-1 --yes --results edits.json
python3 <plugin>/scripts/jira_sync.py external-id --ticket WV-01 --key WAR-1 --yes
```

| Flag | Effect |
|---|---|
| (none) | Write `.warp/jira-match.json` with JQL. Print the searches. Store nothing. |
| `--results FILE` | Match a saved candidate list, or record an External ID edit. |
| `--apply` | Store one exact or prefix match on the beam and in `.warp/jira-map.json` (source `summary`). |
| `--yes` | Also store one fuzzy proposal. Confirms an External ID write. |
| `--all` | Report tickets that already have a key. Do not replace them. |
| `--chars N` | Prefix length. Default 60. |
| `--min-score N` | Fuzzy threshold. Default 0.9. |
| `--include-done` | Match Done and closed issues. |
| `--id ID` | One plan id. |
| `--write-external-id` | Prepare or record an External ID write for mapped tickets. |
| `--set-external-id ID=KEY` | One pair, for example `WV-01=WAR-1`. |
| `--force-external-id` | Replace a different non-empty External ID. |
| `--dry-run` | Print the match or the before/after and write nothing. |

`--apply` uses the same recording as `resolve --ticket`: the beam and `.warp/jira-map.json` are written together. `/warp-jira-map --from-jira` still tries the external id first, then this same summary comparison, and still waits for `--yes` before storing a summary hit.

`--write-external-id` is a write to Jira through `editJiraIssue`. It needs the connector permission from `/warp-allow-notify --with-jira` (`editJiraIssue` and `getJiraIssueEditmeta` are on that list). The field name comes from `jiraExternalIdField` and the field catalog. A missing or read-only field is skipped and the command still succeeds. Dry-run shows before and after. A different non-empty value is left alone unless `--force-external-id`. A second run sees `jira.externalIdWritten` and does not send the edit again. No Jira comment is added. `jiraWriteExternalId` defaults to false, so scan and claim never write the field. These flags work even when that key is false, once `--yes` is set.

## /warp-jira-map

Not a required step. `/warp-scan` and the first claim already store a key that is on the plan, in the map file, or an exact single Jira match on the external-id field, a `warp:<id>` label, or a remote link. Use this command to review the list, to set a leftover or an ambiguous ticket, or to override a key.

Resolution order for each ticket that is still open: explicit key in the plan or map file, then the external-id field in Jira, then a label or remote link, then a summary match (confirm before it is stored).

```bash
python3 <plugin>/scripts/jira_sync.py map
python3 <plugin>/scripts/jira_sync.py map --set WV-01=WAR-1
python3 <plugin>/scripts/jira_sync.py map --import mappings.csv
python3 <plugin>/scripts/jira_sync.py map --from-jira
python3 <plugin>/scripts/jira_sync.py map --auto --results results.json --dry-run
python3 <plugin>/scripts/jira_sync.py map --from-jira --results results.json --yes
python3 <plugin>/scripts/jira_sync.py map --search
python3 <plugin>/scripts/jira_sync.py map --match results.json --yes
python3 <plugin>/scripts/beam.py set --beam .warp/beam.json --id WV-01 --jira WAR-1
```

| Flag | Effect |
|---|---|
| (none) | Print each ticket id and its key, or `unmapped`. |
| `--set ID=KEY` | Store one pair on the ticket and in `.warp/jira-map.json`. Repeatable. |
| `--import FILE` | CSV (`id,jiraKey`), JSON (`{"WV-01": "WAR-1"}`), or a markdown table with `id` and `jira key`. |
| `--from-jira` | Look up unmapped tickets in a saved Jira search. `--auto` is the same flag. With no `--results`, print the JQL and write no keys. |
| `--auto` | Same as `--from-jira`. |
| `--results FILE` | Transcript `{"fields":[...],"searches":[{"jql":"...","issues":[...]}],"remoteLinks":[...]}`. One exact match is stored with its source and confidence. |
| `--dry-run` | With `--from-jira`, print the match and write nothing. |
| `--search` | Write `.warp/jira-search.json` with JQL for `searchJiraIssuesUsingJql`. Changes nothing. |
| `--match FILE` | Propose pairs whose Jira summary equals the ticket summary. |
| `--yes` | Store the proposals from `--match`, or a unique summary hit from `--from-jira`. Without it, a summary match is not written. |
| `--force` | Store a key whose prefix is not in `jiraProject` or `jiraKeyPrefixes`. |

`jiraProject` does not have to be typed by hand. `/warp-init` and `/warp-scan` write it when one prefix is clear. Plan ids such as `WV` are ignored when they never appear as a Jira key. Several plausible prefixes are not guessed.

```bash
python3 <plugin>/scripts/jira_sync.py project --list
python3 <plugin>/scripts/jira_sync.py project --set WAR
python3 <plugin>/scripts/jira_sync.py project --apply results.json
```

| Flag | Effect |
|---|---|
| (none) | Detect from plan files, branches, and recent commit subjects. Write `jiraProject` only when it is empty and one prefix wins. |
| `--list` | Print candidates. Write nothing. |
| `--set KEY` | Store that project key. Also fills an empty `jiraKeyPrefixes`. Replaces a previous value. |
| `--apply FILE` | Transcript from `getAccessibleAtlassianResources` and `getVisibleJiraProjects`. One project, or one match, is stored. One site is stored in `jiraSite`. Several projects write a probe instead of guessing. |
| `--probe` | Write JQL that searches each visible project for the first plan ids (`externalId`, then `External ID`, then label `warp:<id>`). Writes nothing to `jiraProject`. |
| `--record FILE` | Probe transcript. Exactly one project with a hit is stored, with how it was found. Several hits are listed with issue keys and `project --set`. None lists every project. |

When it stays empty, init, scan, and `/warp-jira-check` print `jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC)`.

`beam.py set --jira KEY` is the same check. It rejects a prefix that is not configured unless `--force`. A rescan keeps a key set this way, and reapplies `.warp/jira-map.json` and `jiraKeyMap`.

Plans can carry the real key: `schedule.json` field `jiraKey`, a markdown line `Jira: WAR-1`, a table column `Jira Key` or `jiraKey`, or `[WAR-1]` on the ticket heading.

```bash
python3 <plugin>/scripts/jira_sync.py resolve --ticket WV-01 --key WAR-1 --issue-id 10001 --cloud-id cloud-1
python3 <plugin>/scripts/jira_sync.py resolve --apply results.json
python3 <plugin>/scripts/jira_sync.py record --id WV-01 --event claim --result failed --error "transition rejected"
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
| `--with-jira` | Also every Atlassian tool in the Jira table above, including `editJiraIssue` and `getJiraIssueEditmeta`. |
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

`beam.py` subcommands: `ingest`, `ready`, `set`, `spend`, `gate`, `pause`, `resume`, `board`, `check`, `eta`. `set` takes `--status`, `--agent`, `--branch`, `--jira`, `--pr`, `--sha`, `--via local|connected`, `--bugbot`, `--ci`, `--alarm`, `--attempts`, `--force`. `--jira` is rejected when the prefix is not in `jiraProject` or `jiraKeyPrefixes` unless `--force` is set. A plan id is not a Jira key. Do not hand-edit `beam.json`.

## Channel verbs

`warp:proceed <id>`, `warp:retry <id>`, `warp:pause`, `warp:resume`, `warp:stop`, `warp:start`, `warp:status`. There is no `warp:hold`.
