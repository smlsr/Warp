# Commands

One reference for the chat commands and the scripts they run. Config keys and defaults are in [CONFIG.md](CONFIG.md). Every script below accepts `?`, `help`, `-h`, and `--help`. `?` is always help, including where a value would go. A bare `help` is help unless it is the value of an option (`--folder help` is a folder name). Quote `?` if the shell expands it.

`<plugin>` is the plugin Cursor loaded (often `.cursor/plugins/warp` after init).

## Quick map

| Chat | What it does | Script and common flags |
|---|---|---|
| `/warp-init` | Copy the plugin, backfill config, and allow Warp's MCP tools | `install.py init` `--channel` `--dry-run` `--no-allow` |
| `/warp-scan` | Read a plan into the beam and resolve Jira keys | `scan.py scan` `--folder` |
| `/warp-start` | Start dispatch, run the watchdog, and start the one channel listener | `scan.py start` `--reason` (runs `beam.py watchdog`), then `inbound.py claim` `--beam` `--agent-id` unless the watchdog printed `listener: replace` |
| `/warp-pause` | Stop new claims and the listener | `scan.py pause` or `beam.py pause` `--reason`, then `inbound.py release` |
| `/warp-resume` | Resume a paused beam, run the watchdog, and start the listener if it is not running | `scan.py resume` `--reason` (runs `beam.py watchdog`), then `inbound.py claim` `--beam` `--agent-id` unless the watchdog printed `listener: replace` |
| `/warp-stop` | Stay stopped until the next start. The listener stops | `scan.py stop` `--reason`, then `inbound.py release` |
| `/warp` | One tick of the master loop, including the watchdog | the `warp` skill, `beam.py watchdog`. Not a script flag. |
| `/warp-status` | Rewrite status and the board | `scan.py status`, `beam.py board` |
| `/warp-status-post` | Post that digest | `status_post.py` `--beam` `--out` |
| `/warp-export` | Write the plan files for an outside edit | `scan.py export` `--beam` `--out` |
| `/warp-import` | Replace the plan. The run stays stopped | `scan.py import` `--plan` `--keep-status` |
| `/warp-ingest` | Build the beam from a schedule you name | `beam.py ingest` |
| `/warp-jira-check` | Keyed or unmapped, what is missing, one fix | `jira_sync.py verify`, `catchup` |
| `/warp-jira-view <key-or-id>` | Every field on one issue | `jira_view.py` `--comments` `--links` `--all` `--full` `--verbose` `--json` |
| `/warp-jira-match` | Match summaries. Optionally write External ID | `jira_match.py` `--apply` `--yes` `--write-external-id` |
| `/warp-jira-external-id` | Write mappings into External ID, or a label if that field is missing | `jira_external_id.py` `--apply` `--yes` `--create-field` `--recheck` `--force` `--ticket` |
| `/warp-jira-map` | Review or set plan id to issue key | `jira_sync.py map` `--set` `--import` `--from-jira` |
| `/warp-allow-notify` | Allow specific MCP tools | `allow_notify.py` `--dry-run` `--list` `--check` `--server` `--allow-server-tools` `--with-jira` `--with-git` |
| `/warp-proceed <id>` | Merge one green manual ticket and move Jira to Done | `proceed.py` `--beam` `--by` |
| `/warp-report` | Completion report, or a partial snapshot | `report.py` `--beam` `--out` `--open` `--partial` |
| `/warp-version` | Installed copy, source copy, `.warp/version` | `version.py` |
| `/warp-uninstall` | Delete `.cursor/plugins/warp` and `.warp/` after you confirm | `install.py uninstall` `--yes` `--remove-gitignore` |

`jira_sync.py` subcommands, each printed by `?`: `verify`, `catchup`, `map`, `resolve`, `project`, `external-id`, `record-external-id`, `plan`, `pick`, `record`, `record-comment`.

## Run control

Plugin hooks do not run on cloud runners. `/warp-start`, `/warp-resume`, and `/warp-status` print the resume hint from `scripts/resume_hint.py` (`--root`, `--beam`). Follow it. It does not dispatch. `/warp-stop` (`scan.py stop`) and `/warp-pause` (`scan.py pause` or `beam.py pause`) append a `session-stop` line through `scripts/session_note.py --type session-stop`. A Shuttle finishes with `scripts/session_note.py --type subagent-stop`. A second note of the same type is skipped while it is still the last journal line. Local hooks in `hooks/hooks.json` only repeat those scripts.

`/warp-start`, `/warp-pause`, `/warp-resume`, and `/warp-stop` take `--beam` and `--reason`. Pause keeps in-flight work. Stop stays stopped until the next start. `scan.py start` and `scan.py resume` run `beam.py watchdog`. Every `/warp` tick runs it before `ready()`. A Shuttle heartbeats with `beam.py heartbeat` (`lastSeenAt` and `--agent`). The listener heartbeats with `inbound.py heartbeat` (`listener.lastSeenAt` and `listener.agentId`). A worker is dead when that heartbeat is older than `staleMinutes` (default 15), or it never heartbeated and the claim or listener start is older than `staleMinutes`. A fresh heartbeat is left alone. Paused and stopped runs print `watchdog: skipped` and do not start a replacement. A dead listener while the run is running prints `listener: replace <id>`: launch exactly one `warp-listen` agent with that id. The stale `running` flag is already gone. Do not claim a second id. A second tick must not launch another. A dead Shuttle prints `shuttle: replace <id>`: dispatch exactly one Shuttle for that same ticket. Status becomes `recovering`. Branch, pull request, `jira.startedAt`, and locks stay. Do not queue a duplicate. Past `maxRecoveries` (default 3) the line is `shuttle: alarm <id> worker-died` and no Shuttle starts. Herald posts each `herald:` line once. `Listener died. A new one started.` `<id> worker died. A new Shuttle started.` Plugin hooks do not run on cloud runners. A dead turn does not notify Warp. Cursor does not restart it. There is no process table.

`/warp-start` and `/warp-resume` launch one `warp-listen` sub-agent when `inbound.py claim` prints `listener: started`, or when the watchdog printed `listener: replace` for the id that now owns the slot. `listener: already running` means do not launch a second, except that one replace id if this start has not launched it yet. `/warp-pause` and `/warp-stop` run `inbound.py release`. The listener must not keep reading while paused or stopped. It is one listener for the beam, not one per awaiting_approval ticket. The beam field is `listener`: `state` (`running` or `stopped`), `agentId`, optional `pid`, `startedAt`, and `lastSeenAt`. Cursor cannot start it from a Slack message. There is no webhook. `/warp` is one tick of that loop (watchdog, reconcile, ready, claim) and does not implement a ticket. `/warp-status` rewrites `.warp/STATUS.md`, `.warp/status.json`, `.warp/BOARD.md`, and `.warp/board.html`. When `.warp/warp-complete.html` exists, the status footer names it. Working rows include `bugbot=` and `ci=` for manual tickets as well as auto-merge. `/warp-status-post` posts that digest. `/warp-export` writes `.warp/WARP_PLAN.md` and `.warp/WARP_PLAN.json`. `/warp-import` replaces the graph from `--plan`, keeps status for ids that still exist (`--keep-status`, the default), and leaves the run stopped. `/warp-ingest` builds a beam from a schedule and does not dispatch. `/warp-proceed <id>` is `warp:proceed` for one manual ticket that is already `awaiting_approval`, with Bugbot pass and green CI on the beam. The token may be the plan id (`WV-01`), the Jira key (`WAR-1`), or a `#` number (`#01`). A ticket that is not awaiting approval is refused: the reply names the current status and nothing is merged. There is no `--force`. After the merge, Jira moves to Done unless `jiraDoneOnManualMerge` is false.

## /warp-report

Writes `.warp/warp-complete.html`. `reportOnComplete` (default true) also writes it from `beam.py set` when no ticket is queued or active, and from `scan.py stop`. `false` leaves the write to this command.

```bash
python3 <plugin>/scripts/report.py --beam .warp/beam.json
python3 <plugin>/scripts/report.py --beam .warp/beam.json --partial
python3 <plugin>/scripts/report.py --beam .warp/beam.json --out .warp/warp-complete.html --open
```

A run that still has queued or active tickets exits 2 and prints `not complete; pass --partial`. It does not write the file. `--partial` writes a snapshot with a Partial banner. `--open` prints a `file://` URL. `reportPath` is a second copy when it is not already `.warp/warp-complete.html`.

Waves are concurrency-run segments. A ticket is active from the status event that enters the working set until a status event leaves it. The interval is half-open. Each stretch between two boundaries has one set of active tickets. An empty set is drawn on the concurrency chart and is not a wave, and it splits waves so the same tickets resuming later are a new wave. Adjacent stretches with the same set are one wave. A handoff that keeps the count the same but changes who is running is two waves. Dependency levels are not used.

`beam.py set` appends a `status` event (and `bugbot`, `ci`, `alarm` when those change) to the ticket and to `.warp/events.jsonl`. The first status that leaves `queued` sets `runStartedAt`. Usage is recorded with `beam.py usage` or on `set`:

```bash
python3 <plugin>/scripts/beam.py usage --beam .warp/beam.json --id WV-01 --tokens-in 1000 --tokens-out 200 --tokens-cached 50 --cost 1.25
python3 <plugin>/scripts/beam.py set --beam .warp/beam.json --id WV-01 --tokens-in 1000 --tokens-out 200 --cost 1.25
```

Pass the totals the Cursor run reported for that ticket. Each flag replaces the previous value. If the run did not report usage, do not call this and do not invent numbers. The report shows n/a. `beam.py spend` remains the additive `tokens` and `minutes` total and is not tokens in, out, or cached. A non-green `--ci` increments `pr.ciRetries`.

## /warp-init

```bash
python3 <plugin>/scripts/install.py init
python3 <plugin>/scripts/install.py init --dry-run
python3 <plugin>/scripts/install.py init --channel eng-builds
python3 <plugin>/scripts/install.py init --no-allow
python3 <plugin>/scripts/install.py ?
```

Idempotent. A second run on a complete install changes nothing.

| Flag | Effect |
|---|---|
| `--root` | Repo root. Default is the git top level, else the current directory. |
| `--channel NAME` | Channel for a fresh config, or for an empty channel. Lowercased. Letters, digits, `-`, `_`, max 80. |
| `--dry-run` | Print the steps. Write nothing. |
| `--no-allow` | Do not write the project MCP allowlist. `autoAllowTools: false` does the same on every init. |

Steps: create `.cursor/plugins` and `.warp` if missing; copy plugin files that are not already there (existing files are never overwritten); copy `assets/config.example.yaml` on a fresh config, or append missing keys with their defaults and comments; set an empty `slackChannel` or `teamsChannel` (or the old `Warp` default) to `warp`; detect `gitProvider` from `origin` unless it is already `github` or `bitbucket`; set `pushMerge: false` when there is no remote; write the project MCP allowlist when `autoAllowTools` is true (Slack, Teams, Jira, and GitHub `add_issue_comment` when the provider is GitHub); append the gitignore snippet once; write `.warp/version` from the project plugin copy. User-level `~/.cursor` files are not written. Undo the allowlist with `/warp-allow-notify --revoke`.

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
| `editJiraIssue` | Write the plan id into the External ID field, or add a label with `update.labels` add (never `fields.labels`, which would replace every label). `/warp-jira-external-id --apply --yes`, `/warp-jira-match --write-external-id --yes`, or `jira_sync.py external-id --yes`. Also `/warp-scan` and `/warp-jira-check` when `jiraWriteExternalId` is true (that flag is the consent, so `--yes` is not required). Not a comment. |
| `getJiraIssueEditmeta` | Whether that field exists and is editable on the issue. A missing field falls back. A read-only field is skipped. |
| `getJiraScreen` | Read a company-managed screen. Used only when trying to attach an External ID field that already exists. |
| `updateJiraScreen` | Add an existing field to a screen. It does not create a field. Returns a plan until commit. |
| `createJiraIssueRemoteIssueLink` | Add a remote link whose globalId is `warp:<id>` when `jiraExternalIdFallback` is `remote-link`. |
| `createJiraField`, `createCustomField`, `createJiraCustomField` | Probes. Not in the Rovo catalog (30 Sep 2026). `/warp-jira-external-id --create-field --yes` looks for one of these names and does not invent a call. |

| When | Jira | Pull request |
|---|---|---|
| Claim | In Progress (`jiraInProgressStatus`) and a comment, if `jiraTransition` is true and the ticket has a key | none |
| PR opened | comment with the link | connected mode only: GitHub `add_issue_comment` or `gh pr comment`. Bitbucket uses the comment tool on `bitbucketMcp` (unnamed here). |
| Bugbot / CI | comment | connected mode only |
| Waiting (`autoMerge` false, sizes not in `autoMergeSizes`) | After Bugbot pass and CI green: QA Ready (`jiraQaReadyStatus`) and a comment, "Bugbot clean, ready for manual review", with the findings-fixed count | connected mode only |
| Merged, auto | Done (`jiraDoneStatus`) and a comment | connected mode only |
| Merged, manual | Done (`jiraDoneStatus`) and a merged comment, unless `jiraDoneOnManualMerge` is false (then the comment only, and the issue stays at QA Ready) | connected mode only |
| Release to queued | no move, unless `jiraRestoreOnRelease` is true | none |
| Pause / stop | no change | none |

A plan id is not a Jira issue key. `WV-01` is not sent as `WV-01`. A key is confirmed when it is on the plan or export, in the map file, its project prefix matches `jiraProject` or `jiraKeyPrefixes`, or Jira has exactly one issue whose external id, `warp:<id>` label, or remote-link id equals the plan id. Otherwise `jiraKey` stays empty and the ticket is `needs mapping`. `/warp-scan` prints `N tickets: K keyed, U need mapping` and, when Jira is connected, resolves those matches before that summary is final. A claim on an unmapped ticket tries that search first. Only a miss or an ambiguous result writes `.warp/outbox.md` and a Herald payload, and it does not call `transitionJiraIssue`. Two Jira issues are reported and neither key is stored. A key set by hand is never replaced.

The first claimed ticket of a run is the one claimed while no ticket has `jira.startedAt` (no earlier ticket was linked and moved to In Progress). If Jira returns not found (`record --result not-found`), or that lookup cannot resolve a real issue key, Warp releases the claim to `queued`, clears `agent`, `branch`, `jira.startedAt`, and `jira.previousStatus`, and stops the run the same way as `/warp-stop`. Herald posts one message, `Run stopped because Jira issues are not linked (WV-01 / WAR-1). Fix jiraProject, /warp-jira-match, or /warp-jira-external-id, then /warp-resume.` It does not also post that the claim still stands. A later miss, after one ticket has `jira.startedAt`, is a per-ticket alarm: the claim stays and the run continues. `jiraTransition: false` does not stop the run.

`/warp-jira-check` runs `jira_sync.py verify` and `catchup`. `verify` with no flags only prints. It does not call Jira, and `needs mapping` on this report does not mean the external-id search already ran. Each ticket has `status: keyed` or `status: unmapped`, `source` (`plan`, `export`, `map`, `manual`, `external`, `label`, `link`, `summary`, `inferred`, or none), and when unmapped a `reason` plus one `fix` command. When every ticket is unmapped the report ends with `why nothing linked:` (`jiraProject is empty`, map keys never copied onto the beam, or no ticket has a Jira key in the plan, map file, or external id). The same line names `jiraMcp`, which must match the Atlassian server Cursor shows. The script cannot tell whether that server is connected. A ticket whose beam status is `merged` and whose `jira.doneAt` is empty is `merged-but-not-done`. `verify` prints that line and the exact `catchup --beam --id` command. `catchup` then prints the Done transition, the merged comment, and the same command.

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

When `jiraWriteExternalId` is true, `catchup` also prints `MUST DO write External ID` for every mapped ticket whose External ID is not yet confirmed equal to the plan id. That config flag is the consent. `--yes` is not required. A key whose source is `external` is already equal and is skipped. A ticket with no confirmed key is skipped. The agent calls `editJiraIssue` for each line, then `jira_sync.py record-external-id --results`. A missing or read-only field sets `jira.externalIdAttempt` and `/warp-jira-check` prints `externalIdAttempt: skipped — field missing`. That line does not replace `lastAttempt`. `/warp-jira-external-id` is the same write when you want it without waiting for a scan.

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

`--write-external-id` is a write to Jira through `editJiraIssue`. It needs the connector permission from `/warp-allow-notify --with-jira` (`editJiraIssue` and `getJiraIssueEditmeta` are on that list). The field name comes from `jiraExternalIdField` and the field catalog. A missing or read-only field is skipped and the command still succeeds. Dry-run shows before and after. A different non-empty value is left alone unless `--force-external-id`. A second run sees `jira.externalIdWritten` and does not send the edit again. No Jira comment is added. `jiraWriteExternalId` defaults to false. When it is true, `/warp-scan` and `/warp-jira-check` queue the write for mappings that already exist, and `--yes` is not required. These flags work even when that key is false, once `--yes` is set. The bulk command for an existing map is `/warp-jira-external-id`.

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
| `--apply FILE` | Transcript from `getAccessibleAtlassianResources` and `getVisibleJiraProjects`. One project, or one match, is stored. One site is stored in `jiraSite`. Several projects write a probe instead of guessing. Issue keys in the transcript or already on the beam are enough on their own: every key in one project is stored even when `getVisibleJiraProjects` is missing. The line is `jira: set jiraProject to WAR (every stored key is in project WAR)`. |
| `--probe` | Write JQL that searches each visible project for the first plan ids (`externalId`, then `External ID`, then label `warp:<id>`). Writes nothing to `jiraProject`. |
| `--record FILE` | Probe transcript. Exactly one project with a hit is stored, with how it was found. Several hits are listed with issue keys and `project --set`. None lists every project. |

When every stored issue key is in one project, `/warp-scan` and `project --apply` write that key even if the project-list tool is missing. Do not run `project --set` after `jira: set jiraProject to WAR (every stored key is in project WAR)`. Keys in more than one project are named (`jira: jiraProject left empty. Stored keys are in ABC, WAR.`) and nothing is written. A different value already in the config is left in place, and the line says the matched issues differ. The same value is left in place. `project --set` remains the manual override when the keys do not agree.

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
python3 <plugin>/scripts/jira_sync.py record-external-id --results edits.json
```

`plan --event` is `claim`, `release`, `qa-ready`, or `done`. `record --result` is `moved`, `already`, `skipped`, `unavailable`, `no-transition`, `failed`, or `not-found`. `not-found` on the first claimed ticket releases the claim and stops the run. A later `not-found` does not. `record-external-id` stores each `editJiraIssue` result. A success sets `jira.externalIdWritten`. A skip sets `jira.externalIdAttempt` and does not clear a transition `lastAttempt`.

## /warp-jira-external-id

Write the plan id into Jira's External ID field for tickets that already have a confirmed key. Dry-run is the default. The script does not call Jira. Use this when `.warp/jira-map.json` and the beam already hold `WV-01 -> WAR-1` and the Jira field was never updated.

```bash
python3 <plugin>/scripts/jira_external_id.py
python3 <plugin>/scripts/jira_external_id.py --ticket WV-01
python3 <plugin>/scripts/jira_external_id.py --apply --yes
python3 <plugin>/scripts/jira_external_id.py --apply --yes --force --force-external-id
python3 <plugin>/scripts/jira_sync.py record-external-id --results edits.json
```

| Flag | Effect |
|---|---|
| (none) | List `WV-01 -> WAR-1: External ID currently <value\|empty\|unknown\|field missing> -> would set WV-01`. Write nothing. |
| `--apply --yes` | Print one `MUST DO write External ID` block. The agent calls `editJiraIssue` for each line. |
| `--dry-run` | List pairs even when `--apply` is set. Write nothing. |
| `--ticket ID` | One plan id. |
| `--force` | Queue a ticket that already has `jira.externalIdWritten`, including a key whose source is `external`. An equal live value is not an error. |
| `--force-external-id` | Replace a different non-empty External ID. The line shows before and after. |
| `--results FILE` | Record a saved transcript. With `--apply --yes` the beam is updated. |
| `--create-field` | With `--yes`, probe for a create-field tool. Site-wide admin change. Needs `--apply --yes` to queue the rest. |
| `--recheck` | Ignore `.warp/jira-field.json` and look the field up again. |

The summary is `jira: external id: would write N, already equal N, skipped N (ID reason)`. After `record-external-id` the verb is `written`. Skips are `no key`, `not editable`, and `different value`. A missing field is one summary line, then the fallback, not one skip per ticket.

Dry-run names the method on each line: `(method field)`, `method label`, or `method remote-link`.

When `.warp/jira-field.json` says the field is missing for this project and config, scan and this command queue the fallback only. `--recheck`, or a change to `jiraExternalIdField`, `jiraExternalIdFieldName`, `jiraProject`, `jiraCreateExternalIdField`, or `jiraExternalIdFallback`, looks again.

`jiraCreateExternalIdField` defaults to false. The one command that allows creation is `/warp-jira-external-id --create-field --yes`. If no create tool exists, or Jira denies it, the command still succeeds and uses `jiraExternalIdFallback`. `jira.externalIdWritten.method` is `field`, `label`, or `remote-link`. `/warp-jira-check` prints `jira mapping:`. `/warp-jira-view` prints `stored in Jira:`.

A key that came from the external-id search is already equal and is not queued. A ticket with no confirmed key is skipped. `jira.externalId` on the beam is the desired plan id, not a confirmation that Jira holds it. Idempotency is `jira.externalIdWritten`. The field is discovered with `getJiraProjectIssueTypesMetadata` and checked with `getJiraIssueEditmeta` on each issue. A missing or read-only field is skipped and the command still succeeds. No comment is added.

`/warp-scan` and `/warp-jira-check` print the same MUST DO block when `jiraWriteExternalId` is true. That flag is the consent, so those paths do not need `--yes`. This command needs `--apply --yes`, and it works when the config flag is false. `editJiraIssue` and `getJiraIssueEditmeta` are already on `/warp-allow-notify --with-jira`.

## /warp-allow-notify

Full behavior is also in the README. Summary:

```bash
python3 <plugin>/scripts/allow_notify.py ?
python3 <plugin>/scripts/allow_notify.py --dry-run
python3 <plugin>/scripts/allow_notify.py
python3 <plugin>/scripts/allow_notify.py --list
python3 <plugin>/scripts/allow_notify.py --check
python3 <plugin>/scripts/allow_notify.py --server plugin-slack-slack
python3 <plugin>/scripts/allow_notify.py --allow-server-tools
python3 <plugin>/scripts/allow_notify.py --with-git
python3 <plugin>/scripts/allow_notify.py --user --yes
python3 <plugin>/scripts/allow_notify.py --revoke
```

| Flag | Effect |
|---|---|
| (default) | Project files. Slack, Teams, and Jira tools. |
| `--dry-run` | Unified diff. Writes nothing, including no backup. |
| `--user` | `~/.cursor/permissions.json`, `cli-config.json`, and `hooks.json`. Refuses to write without `--yes`. `--dry-run`, `--list`, and `--check` do not need `--yes`. |
| `--with-jira` | Accepted. Jira tools are already included. |
| `--with-git` | Also GitHub `add_issue_comment`. No Bitbucket name. |
| `--list` | Detected MCP server names. Writes nothing. |
| `--check` | Covered and missing Warp tools, Run Mode caveat, entries that would fix the IDE prompt. Writes nothing. |
| `--server NAME` | Also allow the tools on this server id. Repeatable. |
| `--allow-server-tools` | Also write `server:*` and `*name*:*`. Every tool on that server, including destructive ones. |
| `--revoke` | Remove only what `.cursor/warp-allow.json` recorded. |
| `--root` | Repo root. |
| `--cursor-home` | Stand-in for `~/.cursor`. Tests. |

Default tool names come from `scripts/mcp_tools.py`: `slack_post_message`, `slack_send_message`, `slack_read_channel`, `slack_read_thread`, `slack_search_channels`, `send_channel_message`, `teams_send_message`, `teams_read_channel`, `teams_read_thread`, `teams_search_channels`, and the Jira tools in the table above. `lookupJiraAccountId` is not called. Each tool is also written as `user-<name>`, `project-<name>`, `plugin-<name>-<name>`, and `*<name>*:<tool>`. Extras go in `notifyAllow` as `server:tool`. The Run prompt shows the tool name when it is different.

IDE: `permissions.json` `mcpAllowlist` skips the prompt when Run Mode is Auto-review, Allowlist, or Run Everything. Ask Every Time ignores it. The key replaces the in-app MCP allowlist. CLI entries do not change the IDE button. Headless CLI uses those `Mcp(server:tool)` lines, or `agent -f` for one process. The local hook returns allow for this list only; a hook allow does not currently skip the prompt. Plugin hooks do not run on cloud runners. Cloud agents and automations do not prompt. They run `scripts/mcp_allow.py --server SERVER --tool TOOL` (`--root`, `--cursor-home`) and call the tool only when it prints `allow`. `ask` means do not call it. That is the same list, not every MCP tool, and it does not approve shell commands.

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

`beam.py` subcommands: `ingest`, `ready`, `set`, `spend`, `gate`, `pause`, `resume`, `board`, `check`, `eta`. `set` takes `--status`, `--agent`, `--branch`, `--jira`, `--pr`, `--sha`, `--via local|connected`, `--approved-by`, `--proceeded-by`, `--merge-method`, `--bugbot pass|fail`, `--ci green`, `--alarm`, `--attempts`, `--force`. `--status awaiting_approval`, `merging`, and `merged` are refused until CI is green and, when Bugbot applies, `--bugbot pass`. A fail sets `fix` or, at `maxFixAttempts`, `alarm` / `bugbot-failed`. A new `--sha` during `awaiting_approval` sets `bugbot_running` and does not move Jira. Setting `merged` stores the sha, `mergedAt`, `via`, merge method, and who approved, releases locks, and prints one post-merge MUST DO. `--jira` is rejected when the prefix is not in `jiraProject` or `jiraKeyPrefixes` unless `--force` is set. A plan id is not a Jira key. Do not hand-edit `beam.json`.

## /warp-proceed

```bash
python3 <plugin>/scripts/proceed.py --beam .warp/beam.json WV-01
python3 <plugin>/scripts/proceed.py --beam .warp/beam.json "warp:proceed WAR-1"
python3 <plugin>/scripts/proceed.py --beam .warp/beam.json --by alex "#01"
python3 <plugin>/scripts/proceed.py ?
```

| Flag | Effect |
|---|---|
| `--beam` | Beam file. Default `.warp/beam.json`. |
| `--by` | Who said `warp:proceed`. Stored on `pr.proceededBy` and, when empty, `pr.approvedBy`. |

The token is a plan id, a Jira key, or a `#` number (`01` and `#01` match a ticket whose id or key ends in that number). One match that is `awaiting_approval` with the gate open is accepted. The status stays `awaiting_approval` until the merge command in the same output is run. Local mode prints `provider.py merge-local`. Connected mode prints the squash-merge, then `beam.py set --status merged --sha --via connected`. That set prints one post-merge MUST DO: Jira transition to Done, merged comments, lock release, dependents that are now ready, `status` and `board`, and the Slack reply with the merge sha and the Jira status. Do not stop after the provider merge. A miss, a ticket in any other status, or a closed gate is a refusal and a Slack reply. Nothing is merged.

## Channel verbs

One `warp-listen` listener reads these. Herald posts an acknowledgement in the same channel before the command runs. There is no `warp:hold`.

`warp:proceed <id>`, `warp:retry <id>`, `warp:pause`, `warp:resume`, `warp:stop`, `warp:start`, `warp:status`.

| Command | Ack, then the action |
|---|---|
| `warp:proceed <id>` | `Received warp:proceed XV-01. Merging and moving Jira to Done.` Plan id or Jira key. Only that awaiting_approval ticket. A bad id acks and merges nothing else. |
| `warp:retry <id>` | `Received warp:retry XV-01. Requeueing XV-01.` Status `queued`, alarm cleared, attempts kept. |
| `warp:pause` | `Received warp:pause. Pausing the run and stopping the listener.` |
| `warp:resume` | `Received warp:resume. Resuming the run.` Does not launch a second listener. |
| `warp:stop` | `Received warp:stop. Stopping the run and the listener.` |
| `warp:start` | `Received warp:start. Starting the run.` |
| `warp:status` | `Received warp:status. Posting the digest.` |
| anything else starting with `warp:` | `Not understood: warp:hold. Accepted forms: warp:pause, warp:resume, warp:stop, warp:start, warp:proceed <id>, warp:retry <id>, warp:status.` |

```bash
python3 <plugin>/scripts/inbound.py claim --beam .warp/beam.json --agent-id <id> --pid <pid>
python3 <plugin>/scripts/inbound.py heartbeat --beam .warp/beam.json --agent-id <id>
python3 <plugin>/scripts/inbound.py release --beam .warp/beam.json
python3 <plugin>/scripts/inbound.py status --beam .warp/beam.json
python3 <plugin>/scripts/inbound.py handle --beam .warp/beam.json --text "warp:proceed XV-01" --by <who> --source slack
python3 <plugin>/scripts/inbound.py enqueue --beam .warp/beam.json --text "warp:status" --by <who> --message-id <ts> --source slack
python3 <plugin>/scripts/inbound.py drain --beam .warp/beam.json
python3 <plugin>/scripts/inbound.py ?
```

| Flag | Effect |
|---|---|
| `--beam` | Beam file. Default `.warp/beam.json`. |
| `--agent-id` | Sub-agent id stored on `listener.agentId`. Claim refuses a second id while `listener.state` is `running`. Heartbeat refuses a different id. |
| `--pid` | Optional process id stored on `listener.pid`. |
| `--text` | The channel line to parse or apply. |
| `--by` | Who sent it. Stored on the ack journal and, for proceed, on `pr.proceededBy`. |
| `--message-id` | Dedupe key for `enqueue`. A repeat is not applied twice. |
| `--source` | `slack` or `teams`. Recorded on the ack. |

`handle` writes `.warp/inbound-ack.json` before it changes the beam. `drain` applies `.warp/pending-commands.jsonl` in order, each ack first. `jiraDoneOnManualMerge: false` changes the proceed ack to say Jira stays at QA Ready. Pause and stop set `listener.state` to `stopped`. The listener must not keep reading while paused or stopped. `heartbeat` writes `listener.lastSeenAt` for the owning agent id. Claim does that once, and the listener repeats it every pass. `beam.py heartbeat --id --agent` is the same write for a Shuttle. `beam.py watchdog` is the check. Shuttle commands:

```bash
python3 <plugin>/scripts/beam.py heartbeat --beam .warp/beam.json --id WV-01 --agent shuttle-WV-01
python3 <plugin>/scripts/beam.py watchdog --beam .warp/beam.json
```
