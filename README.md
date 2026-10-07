# Warp

Warp is a Cursor plugin that scans a repo for a plan, builds a schedule, and dispatches workers under dependency, lock, and gate rules. It is not tied to one product. A third party can point it at their own specs.

Shuttle workers implement one ticket each. Reed reviews. The orchestrator is the only merger: the Warp master loop dispatches, merges, and tracks, and it writes no ticket product code. Herald posts to Slack and Teams. The beam survives a stop.

There is no per-person cap. The only concurrency cap is `maxAgents`. A project may set 18. The orchestrator reads that config value.

## Quick start

1. Import `https://github.com/smlsr/Warp` (Install, below), install Warp from Customize, and reload. Then `/warp-init` in the repo you want built.
2. Connect Jira, GitHub or Bitbucket if you want pull requests, and Slack or Teams. Create a lowercase `warp` channel, or set another name with `/warp-init --channel NAME`. Init writes the project allowlist for the Slack and Jira tools Warp calls. Set Run Mode to Auto-review, Allowlist, or Run Everything.
3. `/warp-scan` (or `/warp-scan <folder>`). `jiraProject` is filled when one project is clear from the plan, branches, recent commits, or Jira. A Jira JSON import uses `externalId` as the plan id (`h2. Size`, `h2. Locks`, `h2. Blocked by`, `h2. Acceptance`, and labels such as `size:S`). That value is not the issue key. Keys already on the plan, in an export `key`, or in `.warp/jira-map.json` are stored. If Jira is connected, the rest are resolved in order: `jiraExternalIdField`, then a `warp:<id>` label, then a remote link. The summary looks like `12 tickets: 9 keyed, 3 need mapping`. `/warp-jira-match` is the summary step. `/warp-jira-map` is only for ids that stay unmapped or ambiguous. Then `/warp-start`.
4. `/warp-status` reads the board. `/warp-jira-check` explains a Jira status that did not move. `/warp-jira-view WAR-1` prints one issue. `/warp-version` compares the installed copy with this plugin.

The command table is below. Flags are in [docs/COMMANDS.md](docs/COMMANDS.md). Every config key is in [docs/CONFIG.md](docs/CONFIG.md). The Jira path from import to merge is in [docs/GUIDE.md](docs/GUIDE.md). When a transition fails, start with [docs/RUNBOOK.md](docs/RUNBOOK.md). What changed is in [CHANGELOG.md](CHANGELOG.md).

## Install

Customize installs a plugin that is already on the Cursor Marketplace or a team marketplace. Pasting `https://github.com/smlsr/Warp` into Customize does not add Warp, and Cursor shows no error.

A team admin on Teams or Enterprise imports the repository. People on that marketplace then install it from Customize:

1. Open the Cursor dashboard, then Plugins & MCPs.
2. Under Team Marketplaces, choose Add Marketplace, then Import from Repo.
3. Paste `https://github.com/smlsr/Warp`. Cursor reads `.cursor-plugin/marketplace.json` on `main` and lists Warp. Add it, set who can see the marketplace, and save.
4. In the Cursor app, open Customize in the sidebar, find Warp, and choose Install for yourself or for the project.
5. Run Developer: Reload Window.

The plugin manifest is `.cursor-plugin/plugin.json` at the root of that repo. The logo is `assets/logo.svg`, committed in the repo. Listing Warp on the public Cursor Marketplace is a separate review at [cursor.com/marketplace/publish](https://cursor.com/marketplace/publish). Until that review lands, Customize search does not find this repository on its own.

Without a team marketplace, copy this repo to `~/.cursor/plugins/local/warp` so that folder contains `.cursor-plugin/plugin.json`, then reload. Local plugin imports have to be allowed (Dashboard, Settings, Security & Identity, Marketplace and Plugins). A marketplace plugin with the same name takes precedence.

Then, in the repo you want Warp to build, type `/warp-init`. It does the manual steps below and is safe to run again: it checks each one and does only what is missing.

| Step | Skipped when |
|---|---|
| `mkdir -p .cursor/plugins .warp` | both folders exist |
| Copy the plugin to `.cursor/plugins/warp` | every plugin file is already there. Existing files are never overwritten. |
| Copy `assets/config.example.yaml` to `.warp/config.yaml` | the file exists and already has every key. A re-run appends missing keys (with their defaults and comments) and names them. Values you set are not changed. |
| Set `slackChannel` and `teamsChannel` to `warp` | the channel already has a value. Only an empty channel (or the old `Warp` default) is filled; a custom name is kept. |
| Append `assets/gitignore-snippet.txt` to `.gitignore` | the snippet is already there. An old `.warp/` line that ignores the whole directory is replaced. Other lines stay. |

### Update

`/warp-upgrade` replaces an installed copy. Run it in the repo that has Warp installed. Init and upgrade write `.cursor/commands/warp-upgrade.md` from `commands/warp-upgrade.md` so a reload lists `/warp-upgrade` when the plugin command index still omits that path.

1. Run `/warp-upgrade` in the repo that has Warp installed.
2. It replaces `.cursor/plugins/warp` with the plugin this command is running from.
3. It fetches the default branch when the source is a git checkout, and copies that tree.
4. It does not overwrite `.warp/config.yaml` or the beam.
5. It prints `Warp vX.Y.Z`.
6. Reload Cursor after (Developer: Reload Window).
7. Confirm the version with `/warp-version`. That prints the installed copy (`.cursor/plugins/warp`), the source copy, and `.warp/version`.
8. If the fetch fails, it prints `fetch failed` and `keeping the installed copy`. The installed copy stays.
9. `/warp-init` does not upgrade an existing copy. This command does.

`--force` is not an upgrade flag. `python3 <plugin>/scripts/upgrade.py ?` prints `--root` and `--source`.

```bash
python3 <plugin>/scripts/upgrade.py ?
python3 <plugin>/scripts/upgrade.py --root .
python3 <plugin>/scripts/upgrade.py --root . --source /path/to/warp
```

`/warp-upgrade` is `commands/warp-upgrade.md` with `name: warp-upgrade` and a `description`, the same shape as `commands/warp-version.md`, listed in `.cursor-plugin/plugin.json` with the other commands. Cursor's plugin command index keeps the paths it had when that index was published, so this file is in the plugin root and still omitted there. Init and upgrade write `.cursor/commands/warp-upgrade.md` from that file. Reload Cursor (Developer: Reload Window) so that project command shows up next to the others.

`/warp-version` prints the script when that slash command is missing from an older palette. The script does not need the slash command. A 1.4.6 tree includes `scripts/upgrade.py`:

```bash
python3 .cursor/plugins/warp/scripts/upgrade.py
python3 .cursor/plugins/warp/scripts/upgrade.py ?
```

If `.cursor/plugins/warp/scripts/upgrade.py` is not there, that copy is not a full 1.4.6 tree. From a git checkout of https://github.com/smlsr/Warp at main, run the script with `--source` pointing at that checkout and `--root` pointing at the project, then Developer: Reload Window, then `/warp-version`.

After install, these entries belong in the Cursor MCP allow list (`mcpAllowlist` in `.cursor/permissions.json`). Run Mode must be Auto-review, Allowlist, or Run Everything, then reload Cursor. `/warp-init` and `/warp-allow-notify` write them. The globs are what to add because Cursor names the server differently per machine.

Required for the Jira and Slack prompts:

- `*slack*:slack_send_message`
- `*atlassian*:transitionJiraIssue`
- `*atlassian*:addOrEditJiraIssueComment`

Teams (does not cover the three above):

- `*teams*:send_channel_message`
- `*teams*:teams_send_message`
- `*teams*:teams_read_channel`
- `*teams*:teams_read_thread`
- `*teams*:teams_search_channels`

GitHub, only when git comments are on:

- `*github*:add_issue_comment`

`warp` is one shared channel for every repo, so each message names its repo in a header (see below). Warp does not create the channel: create `warp` once in Slack and Teams, or change the config to a channel that exists. Channel names are lowercase (letters, digits, `-`, `_`). `--channel NAME` is lowercased and checked; a custom `slackChannel` with uppercase letters is warned about by `/warp-init` and lowercased when posting.

To do it by hand instead:

```bash
mkdir -p .cursor/plugins .warp
cp -R warp .cursor/plugins/warp
cp .cursor/plugins/warp/assets/config.example.yaml .warp/config.yaml
```

Append `assets/gitignore-snippet.txt` to the repo `.gitignore`. Reload Cursor. Connect Jira, GitHub or Bitbucket if you want pull requests, and Slack or Teams in Cursor Settings.

## Commands

Every chat command. Common flags only. The full list, including `jira_sync.py` subcommands, is in [docs/COMMANDS.md](docs/COMMANDS.md). Each script accepts `?`, `help`, `-h`, and `--help`.

| Command | What it does | Common flags |
|---|---|---|
| `/warp-init` | Copy missing plugin files, backfill config, commit-safe gitignore, allow Warp's MCP tools | `--channel`, `--dry-run`, `--no-allow` |
| `/warp-upgrade` | Replace `.cursor/plugins/warp` with the current plugin. Does not touch config or the beam | `--root`, `--source` |
| `/warp-scan` | Read a plan into the beam and resolve Jira keys | `--folder` |
| `/warp-start` | Start dispatch. A cloud run refuses when Jira or Slack tools are missing from the allow list | `--reason`, `--force` |
| `/warp-pause` | Stop new claims and the listener. In-flight work finishes its step | `--reason` |
| `/warp-resume` | Resume a paused beam, run the watchdog, and start the listener unless one is already running | `--reason` |
| `/warp-stop` | Stay stopped until the next start. The listener stops | `--reason` |
| `/warp` | One tick of the master loop, including the watchdog | |
| `/warp-status` | Rewrite `.warp/STATUS.md` and the board | |
| `/warp-status-post` | Post that digest to Slack or Teams | |
| `/warp-export` | Write `.warp/WARP_PLAN.md` and `.warp/WARP_PLAN.json` | |
| `/warp-import` | Replace the plan from a file. The run stays stopped | `--keep-status` |
| `/warp-ingest` | Build the beam from a schedule you name | |
| `/warp-jira-check` | Per ticket: keyed or unmapped, what is missing, one fix | `verify --link`, `verify --apply` |
| `/warp-jira-view` | Every field on one issue, by key or plan id | `--comments`, `--links`, `--all`, `--full`, `--verbose`, `--json` |
| `/warp-jira-match` | Match unmapped summaries. Optionally write External ID | `--apply`, `--yes`, `--chars`, `--min-score`, `--include-done`, `--write-external-id` |
| `/warp-jira-external-id` | Write existing mappings into External ID, or a label if that field is missing | `--apply`, `--yes`, `--force`, `--force-external-id`, `--ticket`, `--create-field`, `--recheck` |
| `/warp-jira-map` | Review or set plan id to issue key | `--set`, `--import`, `--from-jira`, `--yes` |
| `/warp-allow-notify` | Allow specific Slack, Teams, Jira, and GitHub tools | `--dry-run`, `--with-jira`, `--with-git`, `--list`, `--check`, `--server`, `--allow-server-tools`, `--user`, `--yes`, `--revoke` |
| `/warp-proceed` | Merge one green manual ticket and move Jira to Done | `proceed.py` |
| `/warp-report` | Completion report, or a partial snapshot | `--out`, `--open`, `--partial` |
| `/warp-version` | Installed copy, source copy, and `.warp/version` | |
| `/warp-uninstall` | Delete `.cursor/plugins/warp` and `.warp/` after you confirm | `--yes`, `--remove-gitignore` |

## Slack and Teams messages

Warp has no webhook and no token. Messages go out through the Slack and Teams servers connected in Cursor Settings, posted by the Herald agent in your chat. A script cannot send them on its own, so nothing arrives unless those servers are connected, a channel exists and is set, and the agent session that ran the command is the one that posts. Warp does not create channels.

The channel is shared across repos, so every message opens with a header that names its source:

```text
Warp | <repo> / <project>
```

`<repo>` comes from the `origin` remote, or the folder name if there is none. `<project>` is `projectName` in `.warp/config.yaml`, else `jiraProject`, else the workspace folder name. If it is the same word as the repo, only the repo is shown. One formatter, `scripts/herald_fmt.py`, builds every message that Warp's scripts generate, and Herald uses it for the rest (claims, alarms, gates). Each message has a Slack view (a header block, mrkdwn section and context, with `<url|label>` links) and a Teams view (markdown), plus plain text for `.warp/outbox.md`.

Example, `/warp-init` (shown as Slack renders it):

```text
Warp | Demo-App / ops-platform
Initialized
Cursor repo Demo-App was initialized with Warp.
Channel: warp
Repo: Demo-App (https://github.com/acme/Demo-App)
Branch: main
Config: .warp/config.yaml
Warp v<version>. Next: /warp-scan, then /warp-start.
```

Example, `/warp-scan`:

```text
Warp | Demo-App / ops-platform
Scan finished
Format: markdown
Tickets: 7
Gates: 2
Run: stopped
Estimate: agent 60.0h, human 2.5h, elapsed 43.5h
- Plan used: spec/CURSOR_PLAN.md (https://github.com/acme/Demo-App/blob/main/spec/CURSOR_PLAN.md)
Links point at branch main; they work once it is pushed. Warp v<version>. Next: /warp-start. The beam, journal, and board are committed. .warp/config.yaml stays gitignored.
```

- `/warp-init` posts only if it changed something. A second run posts nothing.
- `/warp-scan` posts the summary and links to the plan and schedule files found. Links use the `origin` remote and branch (GitHub, GitLab, bitbucket.org). Otherwise the relative path is used. Links work once the branch is pushed.
- `/warp-status-post` uses the same header.
- It follows `messenger` and `notify`. `notify: quiet` skips the init and scan messages. Each message goes to `slackChannel` and `teamsChannel` for the messenger you chose.
- Fail-soft: with no channel set, or no connected server, the text is saved to `.warp/outbox.md`, the command says so, and it still succeeds. The payload is in `.warp/notify-post.json`.

## Allow notify

`/warp-init` writes the project allowlist (same as `/warp-allow-notify`, including Jira). `autoAllowTools: false` or `/warp-init --no-allow` skips that. User-level files stay opt-in (`--user --yes`). Options:

```bash
python3 <plugin>/scripts/allow_notify.py ?
python3 <plugin>/scripts/allow_notify.py --dry-run
python3 <plugin>/scripts/allow_notify.py
python3 <plugin>/scripts/allow_notify.py --list
python3 <plugin>/scripts/allow_notify.py --check
python3 <plugin>/scripts/allow_notify.py --server plugin-slack-slack
python3 <plugin>/scripts/allow_notify.py --with-git
python3 <plugin>/scripts/allow_notify.py --allow-server-tools
python3 <plugin>/scripts/allow_notify.py --user --dry-run
python3 <plugin>/scripts/allow_notify.py --user --yes
python3 <plugin>/scripts/allow_notify.py --revoke
```

`?`, `help`, `-h`, and `--help` print the same reference. Quote `?` if the shell expands it.

| Flag | Behavior |
|---|---|
| (none) | Project scope. Slack, Teams, and Jira tools. This is the default. |
| `--dry-run` | Print a unified diff of every file that would change. Write nothing, and do not write a backup. |
| `--user` | Edit the home-directory files instead of the repo. The script refuses to write unless `--yes` is also set. A dry run, `--list`, and `--check` do not need `--yes`. |
| `--yes` | Confirm a user-level write. Project scope does not need it. |
| `--with-jira` | Accepted. Jira tools are already included. |
| `--with-git` | Also allow GitHub `add_issue_comment`. Warp does not name a Bitbucket comment tool. |
| `--list` | Print MCP servers found in mcp.json files. Write nothing. |
| `--check` | Print which Warp tools project and user permissions, CLI, and hook files cover, plus the Run Mode caveat and the entries that would fix a gap. Write nothing. |
| `--server NAME` | Also allow the tools on this server id. Repeatable. |
| `--allow-server-tools` | Also write `server:*`. That allows every tool on the server, including destructive ones. Off by default. |
| `--revoke` | Remove only the entries this command recorded in the manifest. Pre-existing entries stay. |
| `--root` | Repo root. Default is the git top level, else the current directory. |
| `--cursor-home` | Directory to use instead of `~/.cursor`. For tests. |

Default tool names come from `scripts/mcp_tools.py`, on the servers `slackMcp` and `teamsMcp`:

| Server key | Tools |
|---|---|
| `slackMcp` (default `slack`) | `slack_post_message`, `slack_send_message`, `slack_read_channel`, `slack_read_thread`, `slack_search_channels` |
| `teamsMcp` (default `teams`) | `send_channel_message`, `teams_send_message`, `teams_read_channel`, `teams_read_thread`, `teams_search_channels` |
| `jiraMcp` (included by default) | `getAccessibleAtlassianResources`, `getJiraIssue`, `getTransitionsForJiraIssue`, `listJiraIssueTransitions`, `transitionJiraIssue`, `addOrEditJiraIssueComment`, `addCommentToJiraIssue`, `searchJiraIssuesUsingJql`, `getJiraProjectIssueTypesMetadata`, `getJiraIssueRemoteIssueLinks`, `getVisibleJiraProjects`, `editJiraIssue`, `getJiraIssueEditmeta`, `getJiraScreen`, `updateJiraScreen`, `createJiraIssueRemoteIssueLink`, `createJiraField`, `createCustomField`, `createJiraCustomField` |
| `githubMcp` with `--with-git` | `add_issue_comment` |

Each tool is written for the configured name and for the ids Cursor's Run dialog actually shows: `user-<name>`, `project-<name>`, `plugin-<name>-<name>`, and `*<name>*:<tool>`. `project-0-<folder>-<name>` changes per workspace and worktree, so the glob keeps the tool specific. `server:*` is only written with `--allow-server-tools`. `lookupJiraAccountId` is not called.

`notifyAllow` in `.warp/config.yaml` adds extra `server:tool` pairs. No wildcards. If the Run prompt names a different tool, copy that server and tool into `notifyAllow` and run the command again. The prompt is where the connector's real tool name shows up.

What each Cursor surface actually does:

| Surface | File | Effect |
|---|---|---|
| IDE Run prompt | `.cursor/permissions.json` (`--user`: `~/.cursor/permissions.json`) `mcpAllowlist` | Skips the prompt for those pairs when Run Mode is Auto-review, Allowlist, or Run Everything. Ask Every Time does not consult the list. Setting the key replaces the in-app MCP allowlist, so a tool allowed only in Cursor Settings prompts again until it is in the file. Per-user and per-repo files are combined. A team admin Run Mode override ignores the file. |
| CLI | `.cursor/cli.json` (`--user`: `~/.cursor/cli-config.json`) `permissions.allow` as `Mcp(server:tool)` | Cursor CLI and headless `agent` runs. It does not change the IDE Run button. `agent -f` (also `--force`) approves tools for that process. |
| Hook | `.cursor/hooks.json` (`--user`: `~/.cursor/hooks.json`) `beforeMCPExecution` | Local IDE only. Returns allow for this list and ask for every other call. `failClosed` is off. A hook allow does not currently skip the Run prompt. The permissions file does. Plugin hooks do not run on cloud runners. |
| Cloud agents and automations | `scripts/mcp_allow.py` | They do not use Run Modes and do not ask for approval. Call a tool only when the script prints `allow`. `ask` means do not call it. Same Slack, Teams, Jira, and optional GitHub list. Not every MCP tool. |

The command merges into JSON that is already there. Other hooks, other allow entries, the terminal allowlist, and `autoRun` are left in place. Before it changes an existing file it writes a sibling `.bak`. A second run with the same flags prints `Already set. Nothing changed.` and does not touch the backup. `--revoke` deletes an `mcpAllowlist` key that would otherwise be empty, so Cursor can fall back to the in-app list. `/warp-uninstall` removes the project hook and the project allow entries the manifest recorded. It does not remove `~/.cursor` files. Reload Cursor (Developer: Reload Window) or start a new agent chat afterwards.

## Cloud runners

Plugin hooks do not run on cloud runners. Warp does not depend on them there. The same behavior runs from commands the agent already follows.

| What the hook did | Replacement | Where it runs |
|---|---|---|
| `sessionStart` prints paused, done/total, ETA, and alarms, and says to read the board before dispatch | `scripts/resume_hint.py` (`--root`, `--beam`) | `/warp`, `/warp-start`, `/warp-resume`, `/warp-status`, and the always-applied Warp session rule |
| `stop` appends `session-stop` to `.warp/journal.jsonl` | `scripts/session_note.py --type session-stop` | `/warp-stop`, `/warp-pause`, and the end of a `/warp` tick |
| `subagentStop` appends `subagent-stop` | `scripts/session_note.py --type subagent-stop` | When a Shuttle finishes. The next tick reconciles from the beam |
| `beforeMCPExecution` returns allow for Warp's tool list and ask otherwise | `scripts/mcp_allow.py --server SERVER --tool TOOL` | Before an MCP call that a Warp skill does not already name. `ask` means do not call it |

A local IDE may still run `hooks/hooks.json` and the `beforeMCPExecution` hook `/warp-allow-notify` installs. Those hooks call the same scripts. They are not a second behavior, and cloud runners do not need them. `mcp_allow.py` does not approve shell commands and does not allow every MCP tool.

## Dead workers

A Shuttle on a claimed ticket and the one channel listener are the background workers. A dead turn does not notify Warp. Cursor does not restart it. Plugin hooks do not run on cloud runners. There is no process table.

Each worker writes a heartbeat on the beam: `lastSeenAt` and its agent id. A Shuttle runs `beam.py heartbeat` at claim, at each status change, and at least every 5 minutes while the turn is alive. The listener runs `inbound.py heartbeat` at claim and on every channel read. Both stay inside `staleMinutes` (default 15). `/warp-init` backfills `staleMinutes` and `maxRecoveries` when `.warp/config.yaml` does not have them yet.

`beam.py watchdog` runs on every `/warp` tick and on `/warp-start` and `/warp-resume`. A worker is dead when `lastSeenAt` is older than `staleMinutes`, or it never heartbeated and the claim or listener start is older than that. A fresh heartbeat is left alone. Do not start a second worker. Pause and stop do not replace anyone.

A dead listener, while the run is running, is cleared and exactly one replacement is reserved. Herald posts `Listener died. A new one started.` A second tick does not start another.

A dead Shuttle stays on the same branch, pull request, `jira.startedAt`, and locks. Status becomes `recovering`, and exactly one new Shuttle is dispatched for that same ticket. Herald posts `<id> worker died. A new Shuttle started.` It is not released, so another ticket cannot take the files. Past `maxRecoveries` (default 3) the ticket is `alarm` / `worker-died` and no new Shuttle starts.

## Alarms

The one `warp-listen` listener repairs `lock-escape` and no other alarm. It checks while the run is running, on the cadence of `alarmRepairMinutes` (default 15). Pause and stop stop the listener, so they do not repair. Plugin hooks do not run on cloud runners. The listener calls `scripts/alarm_repair.py next`. A second call in the same moment does not start a second repair.

The Shuttle that hits the alarm stores each path outside the lock with `beam.py set --alarm lock-escape --escaped <path>`. Those paths live on the ticket as `escaped`. The repair widens that ticket's locks to those paths, records them on `addedLocks`, and says which paths it added in the Herald line. It does not take a path an in-flight ticket currently holds. It waits, then widens and starts after that holder finishes. A queued ticket's overlapping lock can be widened. An older alarm with no path list prints `alarm-repair: name <id>`. That Shuttle names the paths with `alarm_repair.py paths` before any edit.

One repair runs at a time, one ticket at a time, in plan order. If it alarms `lock-escape` again, or errors, the alarm stays, the attempt is recorded, and the next lock-escape ticket starts in that same call. The one that just failed is not retried in that pass. If it returns the ticket to the normal path with the alarm cleared, or the ticket merges, the listener waits for that completion, then starts the next. `maxAlarmRepairs` (default 3) leaves the alarm in place and later passes skip it. Herald posts one line when a repair starts, when a failure takes the next ticket, and when a ticket is given up. `bugbot-failed`, `stuck`, `worker-died`, `ci-red`, and `gate-red` are left alone.

`/warp-init` backfills `alarmRepairMinutes` and `maxAlarmRepairs` when `.warp/config.yaml` does not have them yet.

When that pass opens and the ready set is empty, the listener recomputes every pending gate, and a red gate whose failure the beam can already disprove. A red `make ci` on a ticket is sent back in that pass (`send-back <id> fix` and a `start` line) even when other work is ready. The command is the full string `make ci`. The result is stored on `pr.check`. The third red parks the ticket. The name alone is not a red check. Launch that Shuttle with the log. A second pass does not start it again while it is in `fix`. A pending gate, or a stale red gate, turns green when every member is merged or done and no check is actually red, or when every ticket on the beam is merged or done. Then that pass runs the tick. G1 with members P-002, P-003, P-004, and P-018 stores evidence `members merged: P-002, P-003, P-004, P-018` and the board is rewritten. Herald posts `G1 pending cleared. Members merged. Tick ran.` A member that is not merged, a ticket that is still alarmed or parked, or a check that is actually red leaves the gate as it is. A recovered ticket that is merged counts. A ticket that is still alarmed does not. A green gate stays green. Every tick recomputes before `beam.py ready` and before `orchestrator.py dispatch`, so a merge does not wait for this pass. The pass is the backstop when no tick is running. Pause and stop do not recompute.

## Version

The version lives in `VERSION`. `.cursor-plugin/plugin.json` carries the same number. What changed in each version is [CHANGELOG.md](CHANGELOG.md). `/warp-version` prints the installed copy (`.cursor/plugins/warp`) and the source copy this plugin was loaded from. `/warp-init` and `/warp-status` print it too. Init writes the installed version to `.warp/version`. When the project copy is older, init says `plugin is vOLD, repo copy is vNEW: run /warp-upgrade`. Init does not replace plugin files. `/warp-upgrade` does.

```bash
python3 <plugin>/scripts/version.py
```

Every change bumps the patch version and adds a `CHANGELOG.md` entry. `python3 scripts/bump_version.py` does both (`--minor` or `--major` when those are intended). A pull request that does not move the version past `main` fails CI. `scripts/check_version.py --against origin/main` is that check. An equal version passes when the working tree already matches `main`, which is what happens when the check starts after the merge. An equal version with other changes still fails.

## Upgrade

The steps are in [Install](#install), under Update. `/warp-upgrade` replaces `.cursor/plugins/warp`. When the source is a git checkout, it fetches the default branch and copies that tree. If the fetch fails, it prints `fetch failed` and `keeping the installed copy`. It does not overwrite `.warp/config.yaml` or the beam. It prints `Warp vX.Y.Z`. Reload Cursor, then confirm with `/warp-version`. `--force` is not an upgrade flag. The flags are `--root` and `--source`. Running it again is safe.

```bash
python3 <plugin>/scripts/upgrade.py ?
python3 <plugin>/scripts/upgrade.py --root .
```

If `/warp-upgrade` is not in the command list, `/warp-version` prints `python3 .cursor/plugins/warp/scripts/upgrade.py`. `?` prints the flags. The script does not need the slash command. The steps are the same as [Update](#update).

`/warp-init` still does not overwrite plugin files or config values you already set. It does append config keys that are missing, with the defaults and comments from `assets/config.example.yaml`. A config that is only missing new keys does not need an upgrade. Re-run `/warp-init`. It appends each missing key and prints the names it added (`jiraKeyPrefixes`, `jiraKeyMap`, `jiraExternalIdField`, `jiraWriteExternalId`, `jiraSite`, `staleMinutes`, `maxRecoveries`, `alarmRepairMinutes`, `maxAlarmRepairs`, and any later key). Values you already set stay. Readers use the same defaults when a key is still absent.

The plugin has no API that creates a Cursor cloud agent. On `runner: cloud`, the orchestrator starts one new Agent per ticket. An Agent is a separate top-level cloud agent. Own conversation, own VM, own checkout. Check out the ticket branch. Do not start it on a fresh clone of main. `checkout.py launch` fetches origin and cuts a new ticket branch from the tip of the base branch, then commits the beam, journal, board, and that ticket's claim onto that branch and pushes it before the Agent starts. A ticket branch already in flight is not rebased. Tokens, cost, API keys, and webhook URLs are stripped. `.warp/config.yaml` is not committed. The Agent reads the claim from `.warp/beam.json` in that checkout. Do not start a Subagent. A Subagent is a child spawned inside the orchestrator's turn (Task / subagent). It shares the parent session and checkout. Forbidden for tickets. Do not use the Task tool for a ticket. Do not implement the ticket in this turn. `checkout.py implement` refuses to do that work in the orchestrator checkout. If the branch has no `.warp` beam, do not start the Agent. `runner: local` is `git worktree add` and `git worktree remove` of that same branch, one worktree per ticket, not a Subagent inside the orchestrator checkout.

## Troubleshooting

**agents stop and ask to Run/Allow.** The usual cause is a server id mismatch, or an allowlist that was never written. `/warp-allow-notify` used to write `slack:slack_send_message` and skipped Jira unless you passed `--with-jira`, and `/warp-init` did not run it. Cursor's dialog often shows `user-slack`, `plugin-slack-slack`, `project-0-<folder>-slack`, `atlassian-rovo`, or `claude_ai_Atlassian`, so `slack:tool` never matched. Re-run `/warp-init` (or `/warp-allow-notify`). Then set Run Mode to Auto-review, Allowlist, or Run Everything. Ask Every Time ignores the file. `/warp-allow-notify --check` prints what is covered and the entries that would fix a gap. `--list` prints detected servers. A different tool name goes in `notifyAllow`. Reload Cursor. A hook allow does not skip the prompt. Cloud agents do not show it. They use `scripts/mcp_allow.py` instead of a hook. The longer note is in [docs/RUNBOOK.md](docs/RUNBOOK.md).

**Jira transitions failing: ticket needs a real key.** Plan ids such as `WV-01` are not Jira issue keys. `/warp-init` and `/warp-scan` set `jiraProject` when the plan, a branch, a recent commit, or Jira itself shows one project. You do not have to edit it by hand in that case. When every matched issue key is in one project, scan writes that key even if `getVisibleJiraProjects` is not on the connector. The line is `jira: set jiraProject to WAR (every stored key is in project WAR)`. Jira moves use that key. Do not run `project --set` for that result. If the keys span more than one project, `jiraProject` stays empty and the line names them. A different value already in the config is left in place. If the line says `jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC)`, pick one:

```bash
python3 <plugin>/scripts/jira_sync.py project --set WAR
```

An empty `jiraKeyPrefixes` is set to the same project. A value already in the config is left alone. Several candidates are not guessed. `project --list` prints them and writes nothing. `project --probe` writes the JQL that searches each visible project, and `project --record` stores the one project that contains the plan id. One Atlassian site is stored in `jiraSite`.

`/warp-scan` stores a key that is already in the plan (`Jira: WAR-1`, a `Jira Key` column, `[WAR-1]` in the heading, or `schedule.json` `jiraKey`), in a Jira export `key`, or in `.warp/jira-map.json`. A Jira JSON import is different: `externalId` is the plan id, and `h2. Size`, `h2. Locks`, `h2. Blocked by`, `h2. Acceptance`, plus labels `size:S`, `auto-merge`, and `area:*`, describe the ticket. That `externalId` is not stored as `jiraKey`. If Jira is connected, scan looks up the plan id on `jiraExternalIdField` (a field name or `customfield_NNNNN`; also External ID, External Id, ExternalId, External Key, Plan ID, and Ticket ID), then a label `warp:WV-01`, then a remote-link id, and stores `WAR-1` when exactly one issue matches. A summary match is only a proposal. You do not run `/warp-jira-map` for a single exact hit. The scan line is `12 tickets: 9 keyed, 3 need mapping`. A claim tries the same lookup before it will flag the ticket, and it must `resolve --ticket` before any transition. Two matches are reported and neither is stored. A key you set by hand is never replaced.

`/warp-jira-map` is only for a ticket that is still unmapped or ambiguous, or when you want to override:

```bash
python3 <plugin>/scripts/jira_sync.py map --set WV-01=WAR-1
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json
```

`map` with no arguments lists `unmapped` tickets. A CSV, JSON object, or markdown table can be imported with `--import`. After a leftover is mapped, `catchup` prints the transition and comments the current status still owes. The agent calls `transitionJiraIssue` with issue key `WAR-1`, not `WV-01`, then `record`.

**Match by summary.** `/warp-jira-match` compares each unmapped summary with Jira. An exact or 60-character prefix hit is stored with `--apply`. A fuzzy hit (default score 0.9) stays a proposal until `--apply --yes`. Two matches are listed and not stored. `--write-external-id --yes` writes the plan id into the External ID field with `editJiraIssue` (a real Jira write; `/warp-allow-notify --with-jira` allows it). No comment is added. Details are in [docs/COMMANDS.md](docs/COMMANDS.md).

**External ID not written.** Setting `jiraWriteExternalId: true` does not edit Jira by itself. Before 1.3.11, `/warp-scan` skipped every ticket that already had a key, so an existing `.warp/jira-map.json` never produced a write. The only hint was a sentence on a newly resolved ticket, and that path still asked for `--yes`. Re-run `/warp-scan` or `/warp-jira-check`. With the flag true, both print `MUST DO write External ID` for every mapped ticket whose External ID is not confirmed equal to the plan id. The config flag is the consent. `--yes` is not required. The agent must call `editJiraIssue` for each line, then `jira_sync.py record-external-id --results edits.json`. Or preview and apply in one command:

```bash
/warp-jira-external-id
/warp-jira-external-id --apply --yes
```

A key whose source is `external` already matches and is skipped. A ticket with no confirmed key is skipped. A different non-empty value needs `--force-external-id`. A missing or read-only field is skipped and shows on `/warp-jira-check` as `externalIdAttempt: skipped — field missing`. `jira.externalId` on the beam is the plan id we want, not proof Jira holds it. `jira.externalIdWritten` is that proof. A second run does not send the edit again. `--force` queues those tickets again. No comment is posted. The explicit command still needs `--apply --yes`, and it works when the config flag is false.

**External ID field missing.** WAR issues often have no External ID field (Rank may be the only custom field). Warp cannot create that field through the current Atlassian Rovo MCP: the catalog has `editJiraIssue`, `getJiraScreen`, and `updateJiraScreen` (add a field that already exists), and `createJiraIssueRemoteIssueLink`. It does not expose `POST /rest/api/3/field` or field contexts. There is no `createJiraField` tool. If a future server has one, `/warp-jira-external-id --create-field --yes` probes for it. That is a site-wide admin change and stays off unless `jiraCreateExternalIdField` is true.

Until the field exists, the default `jiraExternalIdFallback: label` adds `warp:WV-01` on each mapped issue. Lookup already treats that label as the plan id. The edit is `editJiraIssue` with `update.labels` add, so other labels stay. A second run sees `jira.externalIdWritten.method: label` and does not add it again. `.warp/jira-field.json` remembers the miss, so the next scan prints one summary instead of one skip per ticket. `--recheck` looks the field up again.

```text
/warp-jira-external-id
/warp-jira-external-id --apply --yes
```

To try field creation (it will fall back to labels if the tool is missing or denied):

```text
/warp-jira-external-id --create-field --yes --apply
```

To create the field yourself: Jira admin, Short text custom field named `External ID`, add it to the project's screens and field context, then `/warp-jira-external-id --recheck`. Set `jiraExternalIdFallback` to `remote-link` or `none` if you do not want labels. `/warp-jira-check` prints `jira mapping: label warp:WV-01` (or `field`, `remote link`, or `none`). `/warp-jira-view` prints `stored in Jira:`.

**One Jira issue, every field.** `/warp-jira-view WAR-1` or `/warp-jira-view WV-01`. `WAR-1` is fetched with `getJiraIssue`. `WV-01` is resolved from the beam and `.warp/jira-map.json`, then from the external-id field (and each visible project when `jiraProject` is empty). The script prints `field: value`, the status, the external-id field id, and whether that key is stored. `--comments`, `--links`, `--all`, `--full`, `--verbose`, and `--json` are in [docs/COMMANDS.md](docs/COMMANDS.md). `getJiraIssue`, `getTransitionsForJiraIssue`, and `searchJiraIssuesUsingJql` are already on `/warp-allow-notify --with-jira`.

**First ticket is not linked to Jira.** The first claimed ticket is the one claimed while no ticket has `jira.startedAt` (no earlier ticket was linked and moved to In Progress). If Jira says that issue was not found, or no real issue key can be resolved, Warp releases the claim back to `queued`, clears the agent, branch, and in-progress timestamps, and stops the run the same way as `/warp-stop`. It does not start implementation or open a pull request. Herald posts one message: `Run stopped because Jira issues are not linked (WV-01 / WAR-1). Fix jiraProject, /warp-jira-match, or /warp-jira-external-id, then /warp-resume.` It does not also post that the claim still stands. A later miss, after one ticket has `jira.startedAt`, stays a per-ticket alarm and does not stop the run. `jiraTransition: false` does not stop the run. Fix the link, then `/warp-resume`.

**Jira did not move.** Start with `/warp-jira-check`. With no flags it only prints the beam. It does not call Jira, and `needs mapping` does not mean the lookup already ran. The longer tree is in [docs/RUNBOOK.md](docs/RUNBOOK.md).

1. The first line is `jiraProject not set`, and the matched keys do not already agree on one project. Run `project --list`. One candidate you recognize: `project --set WAR`. Jira is connected and several projects are visible: `project --probe`, then `project --record`. If the scan already printed `jira: set jiraProject to WAR (every stored key is in project WAR)`, the key is written. Do not run `project --set`.
2. `why nothing linked: jiraProject is empty`. Same as the step above. Moves stay off until a project is set. They are not off after the scan wrote the key from matched issues.
3. `why nothing linked: the map file has keys the beam never stored`. `verify --link`, then `catchup`.
4. `status: unmapped` and the `fix` is `verify --link`. That copies a map-file key and writes `.warp/jira-resolve.json`. The agent searches. `verify --apply results.json` stores one exact external-id, label, or remote-link hit. A summary hit is not stored here.
5. The report has `summary candidate:`. `/warp-jira-match --apply` stores one exact or 60-character prefix. A fuzzy line waits for `--apply --yes`. An ambiguous line is not stored. Set it with `map --set WV-01=WAR-1`.
6. `key missing: claim lookup result was not recorded`. The search may have worked. Record it before any transition: `resolve --ticket WV-01 --key WAR-1 --issue-id <id> --cloud-id <cloudId>`. Do not pass the plan id to `transitionJiraIssue`.
7. `key stored` and `lastAttempt: claim failed`. Read the error from `record --result failed --error "..."`. `jira.startedAt` was not set. Fix the workflow name or the transition, then `record` again. The failure is also in `.warp/outbox.md`.
8. `key stored` and nothing else is missing. Check `jiraTransition` is not false, `jiraMcp` is the server Cursor shows, and `jiraInProgressStatus`, `jiraQaReadyStatus`, and `jiraDoneStatus` match the workflow. Re-run `/warp-init` if the config is missing keys.

**No Slack or Teams message.** Herald posts only from the agent session that ran the command. `notify: quiet` skips init and scan. An empty `slackChannel` or `teamsChannel` writes `.warp/outbox.md` instead. Warp does not create the channel. Slack names are lowercased. The payload is `.warp/notify-post.json`.

**Channel name rejected.** Use lowercase letters, digits, `-`, and `_`. No spaces or dots. `--channel` is lowercased for you.

## Uninstall

`/warp-uninstall` removes `.cursor/plugins/warp` and `.warp/`, and optionally the snippet `/warp-init` added to `.gitignore`, so a fresh `/warp-init` works. It also removes project allow-notify entries recorded by `/warp-allow-notify`. It first prints what it will remove and deletes nothing until you confirm. `.warp/` holds the beam, journal, and config, and it cannot be recovered. Stop a running beam first with `/warp-stop`. Product code, `warp/<id>` branches, pull requests, a `stateDir` outside the repo, a plugin installed through Cursor Settings, and user-level allow-notify files are not touched. Reload Cursor afterwards.

## Where the files are

A local agent uses your clone. A cloud agent uses a Cursor virtual machine with its own clone. Warp commits `.warp/beam.json`, `.warp/journal.jsonl`, `.warp/STATUS.md`, and `.warp/BOARD.md` on the base branch so the next agent sees who is in flight, which locks are held, and which tickets are parked. `.warp/config.yaml` stays gitignored. Tokens, cost, API keys, and webhook URLs are stripped before that commit. `state_commit.py commit` writes it. Product code is still a Shuttle pull request on `warp/<id>`.

Review files from the Cursor file tree. On a cloud VM, download them before the VM is discarded. Ask for a bundle and Warp writes `.warp/warp-review.zip` (status, board, plan; no journal). Set `stateDir` to an absolute path to keep the beam outside the repo.

## If you have no plan file

1. Paste `examples/PROMPT-make-cursor-plan.md` into an agent in that repo. It writes `CURSOR_PLAN.md` from the specs and tickets. A filled example is `examples/CURSOR_PLAN.sample.md`.
2. `/warp-scan`, or `/warp-scan <folder>` to look only in one folder (see below). Warp also reads `schedule.json`, a Jira JSON export, or a markdown table with `id` and `deps`.
3. A connected Jira plugin is the live system of record after the scan, not a second scheduler. Warp will not invent tickets.

## Scan one folder

`/warp-scan` searches the whole repo. In a monorepo with specs in several places, give a folder path or name:

```
/warp-scan HOS/spec
/warp-scan spec
```

Only that folder is searched for `CURSOR_PLAN.md`, `schedule.json`, `WARP_PLAN.json`, and Jira exports. `.warp/` stays at the repo root.

- An existing path under the repo is used as given.
- A name is matched against folder names, or the end of their paths, anywhere in the repo.
- If a name matches several folders, Warp lists them, marks which have plan files, and scans nothing. If only one has plan files it uses that one and notes the others. Re-run with the full path to pick.
- With no argument and plans in more than one folder, Warp prints the folders it found and uses the richest plan, as before.

## Session

| Step | In the agent window | What you should see |
|---|---|---|
| Install | `/warp-init`. Reload. | `/warp-scan` and `/warp-start` appear. |
| Tick | `/warp` | One pass: watchdog, reconcile, ready, claim. |
| Scan | `/warp-scan` or `/warp-scan <folder>` | Ticket count, format, run stopped. `beam.json` exists. |
| Start | `/warp-start` | Herald posts. Shuttles claim up to `maxAgents`. |
| Pause | `/warp-pause` | No new claims. In-flight finishes its step. |
| Download | Ask for a bundle, or download `STATUS.md`. | The zip or the file on your machine. |
| Edit | Edit `WARP_PLAN.json` locally. Do not edit `beam.json`. | Changed deps, locks, sizes, or gates. |
| Upload | Drag the JSON into the chat, or drop `WARP_PLAN.edited.json`. Do not commit it. | The agent can see the path. |
| Import | `/warp-import` on that path. | Plan replaced. Run stopped. Status kept for surviving ids. |
| Restart | `/warp-start` | Dispatch uses the new graph. |
| Stop | `/warp-stop` | Stays stopped until the next start. |
| Remove | `/warp-uninstall`, then confirm. | The plugin copy and `.warp/` are gone. `/warp-init` installs again. |

Pause is a hold. Stop is an end. Each repo has its own `.warp/`, so stopping one project does not stop another.

## Export, edit, import

Pause first, so a tick cannot claim while the graph is replaced.

```
/warp-pause
/warp-export
```

Export writes `.warp/WARP_PLAN.json` and `.warp/WARP_PLAN.md`. Download both. Only the JSON is imported. The markdown is the brief for another model.

Safe edits: `deps`, `locks`, `size`, `autoMerge`, `critical`, and gate `members` and `checks`. Keep an id stable if you want its pull request, tokens, and minutes kept. A renamed id is treated as new.

Upload by dragging the file into the agent chat, or saving it in the workspace. Then `/warp-import` on that path. Import rewrites the graph, copies status, agent, branch, attempts, tokens, minutes, alarm, the pull request, and `jira` onto ids that still exist, drops removed ids, and sets the run to stopped. Heartbeats (`lastSeenAt`) are not part of the plan file and are not copied. Config is not replaced. Read `.warp/STATUS.md`, then `/warp-start`.

| File | Edit | Role |
|---|---|---|
| `.warp/WARP_PLAN.json` | Yes | The file import reads. |
| `.warp/WARP_PLAN.md` | Read | Analysis brief. Not imported. |
| `.warp/config.yaml` | Yes | Model, cap, messenger. Survives import. |
| `.warp/beam.json` | No | Live status. Scripts write it. |
| `.warp/STATUS.md` | No | Done, working, left. Download to review. |
| `.warp/journal.jsonl` | No | Append-only log. `state_commit.py` commits it with tokens stripped. Do not hand-edit it. |

## Status

`/warp-status` rewrites `.warp/STATUS.md` and `.warp/status.json`. Working means claimed, recovering, coding, review, fix, or waiting on approval. `recovering` is a Shuttle whose heartbeat went stale: the same branch is kept and one replacement is started. Done means merged or moved to Done in Jira. Left is everything else. `.warp/BOARD.md` and `board.html` add gates, alarms, and ETA. When `.warp/warp-complete.html` exists, the status file names it.

## Completion report

When every ticket is merged, done, skipped, blocked, or alarmed, or when the run is stopped, Warp writes `.warp/warp-complete.html` and Herald posts the headline totals plus that path. The file is gitignored with `.warp`. It is not on the remote. `reportOnComplete: false` skips the automatic write. `/warp-report` still writes it, and `--partial` writes a snapshot while work is in progress.

```bash
python3 <plugin>/scripts/report.py --beam .warp/beam.json
python3 <plugin>/scripts/report.py --beam .warp/beam.json --partial
python3 <plugin>/scripts/report.py --beam .warp/beam.json --out .warp/warp-complete.html --open
```

The page is one self-contained HTML file. Waves are concurrency-run segments: tickets active at the same time, from claim until the status leaves the working set. A gap with nobody running is not a wave. A handoff that keeps the count the same but changes who is running is two waves. Token and cost figures come from `beam.py usage` (or the same flags on `set`). If the Cursor run did not report them, the page says n/a. It does not invent numbers.

## Status in Teams or Slack

One listener Agent reads the configured Slack channel, and Teams when `messenger` is `teams` or `both`, for the whole run. It is one Agent for the beam: not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. Ticket workers are not Subagents of that listener. `/warp-start` and `/warp-resume` launch the `warp-listen` skill when `inbound.py claim` prints `listener: started`. `listener: already running` means do not launch a second. `/warp-pause` and `/warp-stop` run `inbound.py release` and set `listener.state` to `stopped`. The listener must not keep reading while paused or stopped.

The slot is `listener` on the beam. `state` is the flag (`running` or `stopped`). `agentId` is the one Agent id. `pid` is optional and is often empty on a cloud runner. `lastSeenAt` is the heartbeat, written by `inbound.py heartbeat` at claim and on every read.

Cursor cannot start that listener from a Slack message. There is no webhook, and plugin hooks do not run on cloud runners. The listener stays in its turn and re-reads the channel every `pollSeconds`. If that turn dies, [Dead workers](#dead-workers) is how Warp notices and starts one replacement. Pause and stop do not.

Every recognized `warp:` command is acknowledged in the channel before it runs. The ack names the command and, when there is one, the ticket id. Unknown or malformed commands get an ack that says they were not understood and lists the accepted forms.

- `warp:proceed <id>` merges that one `awaiting_approval` ticket and moves Jira to Done (`jiraDoneOnManualMerge`). The id is the plan id or the Jira key. A bad id is acknowledged and nothing else is merged. The ack is `Received warp:proceed XV-01. Merging and moving Jira to Done.`
- `warp:pause`, `warp:resume`, `warp:stop`, `warp:start`, `warp:retry <id>`, and `warp:status` are acknowledged, then run. Pause and stop also stop the listener.
- In the agent window, `/warp-status-post` still posts the digest and attaches `STATUS.md`, `status.json`, and `BOARD.md`. `warp:status` does that after its ack.

Set `teamsChannel` and `slackChannel` in `.warp/config.yaml`. If a connector is missing, the text is appended to `.warp/outbox.md`. Warp does not store tokens. Teams and Slack cannot pull files from a stopped agent.

## Configuration

One file, `.warp/config.yaml`. Change it, then restart, so the next tick re-reads it.

| Key | Default | Meaning |
|---|---|---|
| `stateDir` | `.warp` | Beam and exports. Gitignore it. |
| `model` | `claude-sonnet-5-5-high` | Coding slug: Claude Sonnet 5.5 High. Must match the Cursor model picker. |
| `maxAgents` | `18` | Concurrent Shuttles. The only cap. A project may set 18. |
| `checkCommand` | empty | Optional command the orchestrator runs on a rebased head before merge. Empty means Bugbot and CI as configured. `make ci` is kept as the full check command. A red result is stored on `pr.check` and the ticket is sent back with the log. The third red parks. The name alone is not a red check. |
| `appendOnlyPaths` | empty | Shared files a ticket may append to, besides its locks. Empty by default. A conflict there keeps both sides. Any other conflict is sent back. |
| `mergeQueue` | `false` | `true`, or a GitHub merge queue the provider reports, enqueues the pull request. A direct merge that branch protection rejects is not marked merged. |
| `autoMergeSizes` | `S, M` | Auto-merge after Bugbot and CI, including L and XL when they are listed. L and XL are not special. Sizes not in `autoMergeSizes` wait. The default leaves L and XL waiting. |
| `messenger` | `both` | `slack`, `teams`, or `both`. |
| `notify` | `verbose` | Every claim and tick, plus init and scan. `quiet` posts alarms, approval waits, a red gate, and pause/stop. `warp:status` is always answered. |
| `runner` | `cloud` | Cloud VM, or `local` for this machine. |
| `jiraProject` | empty | Jira project prefix, such as `WAR`. Plan ids are not issue keys unless the prefix matches. |
| `jiraKeyPrefixes` | empty | More prefixes that confirm a Jira key. |
| `jiraKeyMap` | empty | Plan id to issue key, for example `{"WV-01": "WAR-1"}`. |
| `jiraExternalIdField` | `externalId` | Field name or `customfield_NNNNN` matched to the plan id. One exact hit is stored on scan and claim. |
| `jiraWriteExternalId` | `false` | When true, `/warp-scan` and `/warp-jira-check` queue a write for every mapped ticket that is not already confirmed. The flag is the consent, so `--yes` is not required. A missing External ID field falls back to `jiraExternalIdFallback` (default `label`). |
| `jiraExternalIdFieldName` | `External ID` | Name used if field creation is allowed. |
| `jiraCreateExternalIdField` | `false` | Probe for a create-field tool when the field is missing. Site-wide admin change. Off by default. `--create-field --yes` is the one-shot consent. |
| `jiraExternalIdFallback` | `label` | `label`, `remote-link`, or `none` when the External ID field is missing. |
| `jiraTransition` | `true` | On claim, move the Jira issue to In Progress (tickets with a Jira key only). `false` does not stop the run when the first ticket has no Jira issue. |
| `jiraInProgressStatus` | `In Progress` | Target status name, matched by transition name, status name, then status category. |
| `jiraQaReadyStatus` | `QA Ready` | Manual path (sizes not in `autoMergeSizes`): Jira moves here after Bugbot is clean and CI is green, and waits here until the merge. |
| `jiraDoneStatus` | `Done` | Jira moves here after the merge, auto or manual. |
| `jiraDoneOnManualMerge` | `true` | `false` leaves a manual merge at QA Ready so QA can set Done. |
| `jiraRestoreOnRelease` | `false` | Move the issue back when a claim is released to `queued`. Otherwise it is left alone. |
| `bugbotRequired` | `true` | Both paths. No auto-merge and no QA Ready until Bugbot passes. |
| `bugbotManual` | `true` | Manual tickets only. `false` skips Bugbot before QA Ready. Auto-merge still uses `bugbotRequired`. |
| `maxFixAttempts` | `3` | Shared fix loop, then the ticket alarms. |
| `stuckAfterMinutes` | `90` | No update in this window raises stuck. Separate from a dead worker. |
| `staleMinutes` | `15` | Heartbeat older than this, or no heartbeat and a claim or listener start older than this, means the worker is dead. The watchdog replaces it while the run is running. Pause and stop do not. `/warp-init` backfills this key. |
| `maxRecoveries` | `3` | Shuttle replacements on one ticket. The next death is `alarm` / `worker-died` and does not start another. `/warp-init` backfills this key. |
| `alarmRepairMinutes` | `15` | How often the one listener opens a lock-escape repair pass. Pause and stop do not. `/warp-init` backfills this key. |
| `maxAlarmRepairs` | `3` | Repair attempts for one lock-escape ticket. The repair widens that ticket's locks to the paths that escaped. Past the cap the alarm stays. `/warp-init` backfills this key. |
| `respectMergeWindows` | `false` | True makes new claims wait for a window. |
| `mergeWindows` | `08:30, 13:00, 17:00` | Digest times, or claim gates if the flag is true. |
| `pollSeconds` | `300` | How often the one listener re-reads the channel, and how often a running loop reconciles pull requests. |
| `reportOnComplete` | `true` | Write `.warp/warp-complete.html` and ask Herald to post the totals when the run finishes or is stopped. `false` writes the file only from `/warp-report`. |
| `reportPath` | `.warp/warp-complete.html` | Where the completion report is written, relative to the repo. The default stays inside `.warp`, which is gitignored. |
| `jiraMcp` | `atlassian` | Connected Jira server name, not a tool name. |
| `jiraSite` | empty | Optional site URL used as `cloudId` when you have more than one Atlassian site. |
| `gitProvider` | `auto` | `github`, `bitbucket`, or `auto` (from the `origin` host). |
| `githubMcp` / `bitbucketMcp` | `github` / `bitbucket` | Connected git server names. |
| `ghCli` | `true` | GitHub may use an already-authenticated `gh`. Warp does not install it. |
| `pushMerge` | `true` | `false` is local-only: no push, no pull request, local merge. |
| `baseBranch` | empty | Integration branch. Empty detects it. |
| `slackMcp` / `teamsMcp` | `slack` / `teams` | Connected messenger names. |
| `slackChannel` / `teamsChannel` | `warp` | Shared channel the one listener reads, and Herald posts to. Must already exist. |
| `projectName` | empty | Shown after the repo in message headers. Empty uses `jiraProject`, then the folder name. |
| `notifyAllow` | empty | Extra `server:tool` pairs for `/warp-allow-notify`. No wildcards. |
| `autoAllowTools` | `true` | `/warp-init` writes the project MCP allowlist. `false` or `--no-allow` skips it. |

## Merge policy

The orchestrator is the only merger. Ready means every blocker is merged on the base branch and no lock overlaps an in-flight ticket, including a parent folder. A green pull request frees the slot and keeps the locks until merge or park. The slot stays occupied until the provider check rollup is green. The merge queue is serial. Sizes outside `autoMergeSizes` still wait for `/warp-proceed` or `warp:proceed`. The third red parks the ticket. A red base branch stops merging. On start or resume, restart classifies each ticket from git: merged, in flight, or pending. One checkout per ticket: a cloud agent, or a local git worktree. `maxAgents` is the only cap.

L and XL are not special. `autoMergeSizes` is the cut. Sizes in `autoMergeSizes` (`autoMerge` true) and sizes not in `autoMergeSizes` (`autoMerge` false) share one gate: Bugbot passes, findings are fixed up to `maxFixAttempts`, and CI is green. A listed size then auto-merges and Jira moves to Done. A size not in `autoMergeSizes` then moves to `awaiting_approval`, Jira moves to QA Ready, and the Jira and pull-request comment says Bugbot is clean and how many finding rounds were fixed. A person approves on the provider or with `warp:proceed <id>` (plan id, Jira key, or a `#` number). The one channel listener acks in channel, then merges that ticket in the same turn. Jira then moves to Done, the merged comments go out, locks drop, and dependents whose deps are terminal become ready. The Slack reply includes the ack, then the merge sha and the Jira status. `jiraDoneOnManualMerge: false` leaves the issue at QA Ready. `bugbotManual: false` skips Bugbot on the manual path only. The provider comes from `gitProvider`. If it is not usable, or `pushMerge` is false, Reed merges the branch into `baseBranch` locally and does not push. Three failed Bugbot rounds raise an alarm. Warp will not retry it until `warp:retry`. A red gate blocks dependents until check evidence is recorded. New commits after QA Ready send the ticket back through Bugbot and leave the Jira status where it is. A merge that never recorded Done shows on `/warp-jira-check` as `merged-but-not-done`.

## Estimate

Scan and export write an estimate into `.warp/_ingested_schedule.json`, the beam, and `.warp/WARP_PLAN.md`. Agent hours are implementation time (S=4, M=7, L=11, XL=16, unless a ticket has its own hours). Human hours assume every approval for a size not in `autoMergeSizes`, and every gate check, is answered within 30 minutes. Sizes in `autoMergeSizes` add no human wait. Elapsed hours are the longer of the critical chain plus those waits, and agent hours divided by `maxAgents`.

## Kickoff

Warp starts each ticket in this repo's workspace, local or cloud, with one line:

```
IMPLEMENT API-01
```

The id is the plan id. The workspace is the clone, so `.cursor/rules`, `AGENTS.md`, `CLAUDE.md`, and `.warp/` are on disk. A Shuttle does not inherit the parent chat. Its first step is to read those rules, then the claim in the beam. See `assets/KICKOFF.md`.

Cloud and local are the same contract. A cloud agent is a virtual machine with a clone of the repo. A local agent is your clone. Rules apply because the files are in the workspace, not because they were copied into the prompt. A rule only applies if Cursor would apply it in that workspace: `alwaysApply`, a matching glob, or the Shuttle reading it. Warp requires the read.

## Agents

| Agent | Job |
|---|---|
| Warp | Scan, tick, dispatch, halt. Never edits product code. |
| Shuttle | One ticket, one branch, one pull request. Never merges. |
| Reed | Bugbot, evidence, auto-merge or hold. |
| Herald | Slack and Teams. Status, alarms, approval asks. |

## License

MIT. See `LICENSE`.
