# Warp

Warp is a Cursor plugin that scans a repo for a plan, builds a schedule, and dispatches workers under dependency, lock, and gate rules. It is not tied to one product. A third party can point it at their own specs.

Shuttle workers implement one ticket each. Reed reviews. The orchestrator is the only merger: the Warp master loop dispatches, merges, and tracks, and it writes no ticket product code. Herald posts to Slack and Teams. The beam survives a stop.

There is no per-person cap. There are two caps. `maxAgents` (default 18) is how many agents run at once: every Shuttle, whatever its step. `maxInProgress` (default 20) is how many tickets are open at once, started and not merged or parked, and it holds only new tickets. The orchestrator reads both.

## Quick start

1. Import `https://github.com/smlsr/Warp` (Install, below), install Warp from Customize, and reload. Then `/warp-init` in the repo you want built.
2. Connect Jira, GitHub or Bitbucket if you want pull requests, and Slack or Teams. Create a lowercase `warp` channel, or set another name with `/warp-init --channel NAME`. Init writes the project allowlist for the Slack and Jira tools Warp calls. Set Run Mode to Auto-review, Allowlist, or Run Everything.
3. `/warp-scan` (or `/warp-scan <folder>`). `jiraProject` is filled when one project is clear from the plan, branches, recent commits, or Jira. A Jira JSON import uses `externalId` as the plan id (`h2. Size`, `h2. Locks`, `h2. Blocked by`, `h2. Acceptance`, and labels such as `size:S`). That value is not the issue key. Keys already on the plan, in an export `key`, or in `.warp/jira-map.json` are stored. If Jira is connected, the rest are resolved in order: `jiraExternalIdField`, then a `warp:<id>` label, then a remote link. The summary looks like `12 tickets: 9 keyed, 3 need mapping`. `/warp-jira-match` is the summary step. `/warp-jira-map` is only for ids that stay unmapped or ambiguous. Then `/warp-start`.
4. `/warp-status` reads the board. `/warp-jira-check` explains a Jira status that did not move. `/warp-jira-view WAR-1` prints one issue. `/warp-version` compares the installed copy with this plugin.

The command table is below. Flags are in [docs/COMMANDS.md](docs/COMMANDS.md). Every config key is in [docs/CONFIG.md](docs/CONFIG.md). The Jira path from import to merge is in [docs/GUIDE.md](docs/GUIDE.md). When a transition fails, start with [docs/RUNBOOK.md](docs/RUNBOOK.md). How agents start, end, and are torn down is in [docs/LIFECYCLE.md](docs/LIFECYCLE.md). What changed is in [CHANGELOG.md](CHANGELOG.md).

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

`/warp-upgrade` replaces an installed copy. Run it in the repo that has Warp installed. Init and upgrade write every `commands/*.md` file to `.cursor/commands/`, including `.cursor/commands/warp-upgrade.md` and `.cursor/commands/warp-cleanup.md`, so a reload lists `/warp-upgrade`, `/warp-update-state`, `/warp-list`, `/warp-cleanup`, `/warp-status`, and the other commands when the plugin command index still omits that path.

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

`/warp-upgrade` is `commands/warp-upgrade.md` with `name: warp-upgrade` and a `description`, the same shape as `commands/warp-version.md`, listed in `.cursor-plugin/plugin.json` with the other commands. Cursor's plugin command index keeps the paths it had when that index was published, so files added after that index, including `/warp-upgrade` and `/warp-update-state`, stay omitted there even when `plugin.json` lists them. Init and upgrade write every `commands/*.md` file to `.cursor/commands/`, including `.cursor/commands/warp-upgrade.md`. Reload Cursor (Developer: Reload Window) so those project commands show up next to the others.

`/warp-version` prints the script when that slash command is missing from an older palette. The script does not need the slash command. A 1.4.6 tree includes `scripts/upgrade.py`:

```bash
python3 .cursor/plugins/warp/scripts/upgrade.py
python3 .cursor/plugins/warp/scripts/upgrade.py ?
```

If `.cursor/plugins/warp/scripts/upgrade.py` is not there, that copy is not a full 1.4.6 tree. From a git checkout of https://github.com/smlsr/Warp at main, run the script with `--source` pointing at that checkout and `--root` pointing at the project, then Developer: Reload Window, then `/warp-version`.

After install, these entries belong in the Cursor MCP allow list (`mcpAllowlist` in `.cursor/permissions.json`). Run Mode must be Auto-review, Allowlist, or Run Everything, then reload Cursor. `/warp-init` and `/warp-allow-notify` write them. The globs are what to add because Cursor names the server differently per machine.

`/warp-list` shows what this run still has out, running or idle, and changes nothing. `/warp-cleanup` cancels and archives what `/warp-list` shows. `.warp/agents.json` is the one registry. A Shuttle is replaced only after it is confirmed dead, and a fix round resumes that Shuttle instead of starting another. Bugbot is requested once per head commit. Nothing spawns while the run is paused or stopped, or every ticket is merged or parked. `/warp-pause` and `/warp-stop` end every registered agent. With `CURSOR_API_KEY` in the environment (never commit it) they also cancel and archive this run's cloud agents. Without the key they print `cleanup: link https://cursor.com/agents/<id>` for each one. `/warp-cleanup` is the clear you run yourself, with the same key. It never runs on its own:

```bash
python3 scripts/agents.py list --beam .warp/beam.json
python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud
python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
```

The first command is `/warp-list`. It prints `instance: warp:<instance> host=<host> machine=<machine id>`, one `agent:` line per row in `.warp/agents.json`, `registry: live=<n> ended=<n>`, one `cloud: id= name= repo= status= updated=` line per tagged cloud agent, `list: kept <id> reason=parent` and `reason=self` for the two that are never touched, and `out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>`. The `cloud:` lines need `CURSOR_API_KEY`. Without it the command prints `cloud: not read.` and one `cloud: link https://cursor.com/agents/<id>` for each recorded cloud agent.

The second command is the `/warp-cleanup` dry run, which is the third command without `--apply`. It prints `cleanup: tag [warp:<instance>]`, each match with id, name, repo, status, last update, and link, `cleanup: would cancel <id>` for the running ones, then `cleanup: count`. It changes nothing.

The third command is `/warp-cleanup`. A match is in this checkout's origin repository, and it is in `.warp/agents.json` or its name or prompt starts with this run's `[warp:<instance>]` tag. Other agents in the repo are not matched, whatever words are in their names, and neither are another Warp run's. An IDLE match is archived with `POST /v1/agents/{id}/archive` (`cleanup: archived <id>`). A RUNNING or ACTIVE match has its run cancelled with `POST /v1/agents/{id}/runs/{runId}/cancel` (`cleanup: cancelled <id>`) and is then archived. Without `--running` it stays (`reason=running`). The agent running the command stays (`reason=self`). This run's parent stays (`reason=parent`). While this run is running, the command archives the idle ones and leaves the running ones: `cleanup: --running refused while the run is running. /warp-pause or /warp-stop first.` `--force` overrides that refusal and cancels live work. Archive is reversible in the Cursor UI. Warp never deletes an agent. Without the key, the command prints `cleanup: link https://cursor.com/agents/<id>` for the Cursor UI.

`/warp-list` and `/warp-cleanup` take the same selection:

| Option | Selection |
|---|---|
| none | This run: the registry, and cloud agents in this repo tagged `[warp:<instance>]` |
| `--tag <instance>` | Another run's tag in place of this one. `a1b2c3`, `warp:a1b2c3`, and `[warp:a1b2c3]` are the same |
| `--tag all` | The tag of any Warp run. `--tag '*'` is the same |
| `--untagged` | Also agents with no Warp tag, from before 1.5.0, matched loosely on Warp role words in the name or prompt (Shuttle, IMPLEMENT, fix, rebase, listener, Bugbot) |
| `--all-idle` | Every idle agent in this repo |
| `--any-repo` | Do not limit to this repo |
| `--scan <words>` | Every cloud agent the key can see whose name, summary or description, or first prompt contains one of the words. Ignores the repo and the tag. `--scan` crosses repos: read the list before `--apply` |

An agent that only matched loosely and is still running is left alone (`reason=running-untagged`) unless `--force`, because it may be someone else's. `--scan="Jira","Fix","Bugbot"` matches those words, case-insensitive, in the name, summary or description, and first prompt, in every repo the key can see. `--scan Jira,Fix,Bugbot` and `--scan Jira --scan Fix` are the same. It ignores the repo filter and the tag and registry rules. `--scan` crosses repos, so read `/warp-list --scan` before `/warp-cleanup --scan --apply`. A dry run prints `would archive` and `would cancel`. A running match is left (`reason=running-scan`) unless `--force`. A pile of about 199 idle Warp agents from a run before 1.5.0 carries no tag: `/warp-stop`, then `/warp-list --untagged` to read it, then `/warp-cleanup --untagged`. If either slash command is missing from the command list, run `python3 .cursor/plugins/warp/scripts/agents.py list --beam .warp/beam.json` or `python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running`.

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
| `/warp-start` | Set the run running, from a stopped run, a paused run, or a run that is already running, and print which (`run: was stopped. Starting.`). The same command as `/warp-resume`. This turn becomes the parent of the run and prints `parent: session <id>`. It loads main's beam on a fresh checkout, runs the watchdog, and starts unfinished work first (`unfinished:`). A cloud run refuses when Jira or Slack tools are missing from the allow list | `--reason`, `--force` |
| `/warp-pause` | End every agent in `.warp/agents.json`, free every Shuttle slot, close the listener's poll (`halt: paused agents=<n>`), then push that paused beam to main. With `CURSOR_API_KEY`, cancel and archive this run's cloud agents. Branches, worktrees, and pull requests stay. No completion report. Carry on with `/warp-start` or `/warp-resume` | `--reason` |
| `/warp-resume` | The same command as `/warp-start`. On a paused run it prints `run: was paused (<reason>). Continuing.` A ticket that had a Shuttle out gets one new Shuttle on the same branch (`fix: <id> resume after halt`). Nothing from before the pause or the stop is resumed | `--reason`, `--force` |
| `/warp-stop` | The same teardown as `/warp-pause` (`halt: stopped agents=<n>`), with one difference: it also writes the completion report and records `stoppedAt`. Push that stopped beam to main. Carry on with `/warp-start` or `/warp-resume` | `--reason` |
| `/warp-listen` | Take the listener lease in this chat and run the same read loop. Any other copy exits superseded. The parent only applies | `--holder` |
| `/warp-list` | Show what this run still has out, running or idle: the instance, every registry row, each tagged cloud agent with its status, and `out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>`. Changes nothing. `--scan` lists word matches in any repo | `--beam`, `--tag`, `--untagged`, `--all-idle`, `--any-repo`, `--scan` |
| `/warp-cleanup` | Cancel the run of each agent `/warp-list` shows as running, and archive all of them. Leave `--apply` off for a dry run. While the run is running it archives the idle ones and refuses the running ones unless `--force`. `--scan` archives idle word matches and leaves running ones unless `--force` | `--beam`, `--cloud`, `--apply`, `--running`, `--force`, `--tag`, `--untagged`, `--all-idle`, `--any-repo`, `--scan` |
| `/warp-update-state` | Load main and ticket folders, keep parent pause, stop, and claims, push the beam | `--beam` |
| `/warp-sync` | Read Jira into the beam. Dry-run unless `--apply`. Does not dispatch | `--jira`, `--prs`, `--apply`, `--force` |
| `/warp` | One tick of the master loop, including the watchdog | |
| `/warp-status` | List open tickets, alarms and stalls first, and rewrite `.warp/STATUS.md` and the board | |
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
Warp | <repo> / <project> | warp:<instance>
```

`<instance>` is six characters that name one Warp run, which is one beam. `/warp-start` sets it once and it stays on the beam. On a cloud runner it is the last six characters of the parent's cloud agent id. On a laptop it is six characters of a hash of the machine id and the beam's path, so two checkouts on one machine differ. A `/warp-start` or `/warp-resume` from another window or VM keeps it. It is the same tag this run's agents carry in their names (`[warp:<instance>] <ticket> <step>`), so two Warp runs posting to one channel, in one repo or in different repos, are told apart, and so are their agents. `/warp-start`, `/warp-resume`, `/warp-status`, `/warp-version`, and `/warp-list` print it as `instance: warp:<instance> host=<host> machine=<machine id>`, and `.warp/STATUS.md` and `.warp/status.json` record it. Before the first `/warp-start` there is no instance and the header ends at the project.

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
Warp | Demo-App / ops-platform | warp:a1b2c3
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

Plugin hooks do not run on cloud runners. Nothing in Warp depends on a hook. The same behavior runs from scripts that a command, a skill, or a prompt already runs.

| What the hook did | Replacement | Where it runs |
|---|---|---|
| `sessionStart` prints paused, done/total, ETA, and alarms, and says to read the board before dispatch | `scripts/resume_hint.py` (`--root`, `--beam`) | `/warp`, `/warp-start`, `/warp-resume`, `/warp-status`, and the always-applied Warp session rule |
| `stop` appends `session-stop` to `.warp/journal.jsonl` | `scripts/session_note.py --type session-stop` | `/warp-stop`, `/warp-pause`, and the end of a `/warp` tick |
| `subagentStart` (`hooks/subagent-start.sh`) refuses a Warp subagent launch the gate did not issue | `scripts/checkout.py launch`, which prints `spawn: closed <id>` and no prompt, and the reap check `scripts/agents.py reap` in every worker prompt | Before the parent starts a Shuttle, then first thing in every worker and after each state it writes |
| `subagentStop` appends `subagent-stop` | `scripts/session_note.py --type subagent-stop` | When a Shuttle finishes. The next tick reconciles from the beam |
| `beforeMCPExecution` returns allow for Warp's tool list and ask otherwise | `scripts/mcp_allow.py --server SERVER --tool TOOL` | Before an MCP call that a Warp skill does not already name. `ask` means do not call it |

A local IDE may still run `hooks/hooks.json` and the `beforeMCPExecution` hook `/warp-allow-notify` installs. Those hooks call the same scripts. They are not a second behavior, and cloud runners do not need them. `hooks/subagent-start.sh` is a local IDE duplicate of the gate: `checkout.py launch` is the gate, and nothing depends on the hook. `WARP_SPAWN_GATE=off` turns the hook off. Its marks are in `.warp/spawned.json`. `mcp_allow.py` does not approve shell commands and does not allow every MCP tool.

## Run control

Four commands, two behaviors.

`/warp-start` and `/warp-resume` are the same command. Either one sets the run running, from a stopped run, a paused run, or a run that is already running. It prints which: `run: was stopped. Starting.`, `run: was paused (<reason>). Continuing.`, or `run: was already running. This session takes it over.` Both run the same prompt gate, and `--force` means the same on both. `/warp-start` on a paused run is not refused. It just continues. There is nothing to choose between them.

`/warp-pause` and `/warp-stop` are the same teardown. One thing differs: stop also writes the completion report and records `stoppedAt`.

| | `/warp-pause` | `/warp-stop` |
|---|---|---|
| `runState` | `paused` | `stopped` |
| Every agent in `.warp/agents.json` ended, every Shuttle slot freed | yes | yes |
| This run's cloud agents cancelled and archived (with `CURSOR_API_KEY`) | yes | yes |
| Branches, worktrees, pull requests, locks, `jira.startedAt` | kept | kept |
| Completion report `.warp/warp-complete.html` and the Herald totals | no | yes |
| `stoppedAt` recorded as the end of the run | no | yes |
| Use it when | you will carry on: overnight, a meeting, a bad base branch | the run is over, or you want the report |
| To carry on | `/warp-start` or `/warp-resume` | `/warp-start` or `/warp-resume` |

Start and resume check for unfinished work before anything new, and print it:

```text
unfinished: 6 open. 1 need a fix, 3 need a Shuttle, 0 have a Shuttle out, 2 wait on review or checks. These start first, up to maxAgents 18. New tickets start after, while fewer than maxInProgress 20 are open.
```

The order is fixed: open pull requests that need a fix (a conflict, CI that never started, red CI, Bugbot findings), then tickets whose Shuttle was halted or died, then new tickets from the ready queue. `unfinished: none. New tickets start from the ready queue.` means there is nothing left over.

There are two caps:

| Key | Counts | Default |
|---|---|---|
| `maxAgents` | Agents that run at once: every Shuttle, whatever its step (implement, fix, rebase, rerun, restart, repair), in a worktree on this machine or on its own VM | 18 |
| `maxInProgress` | Tickets that are open at once: started, and not merged or parked. A ticket waiting on review or CI is open and uses no agent | 20 |

A fix, a rebase, or a relaunch is an agent like any other. It takes a `maxAgents` slot, and it starts even when `maxInProgress` is full, because its ticket is already open. `maxInProgress` holds only new tickets. Unfinished work draws on the slots first, so a new ticket starts only in a slot unfinished work did not need. `memoryCheck: true` can lower `maxAgents` on a shared machine when memory is tight.

`maxLocalSubagents` and `maxFixWorkers` are retired. A config that still has them prints `config: <key>: <value> is retired and ignored.` at start and in `/warp-version`.

## Agent lifecycle

One agent is long-lived: the parent, the turn that ran `/warp-start` or `/warp-resume`. The parent is the only agent that starts another agent, and the only agent that waits. A Shuttle does one step of one ticket and returns one line. The listener is one background subagent on the parent's checkout. A worker never starts another agent, never loops, sleeps, sets a timer, or subscribes, and never waits on a pull request, CI, Bugbot, Slack, or an approval. Plugin hooks do not run on cloud runners, and nothing here depends on a hook: every check is a script that a command, a skill, or a prompt runs. Five mechanisms hold the rule. The whole rule is in [docs/LIFECYCLE.md](docs/LIFECYCLE.md).

- The gate. `checkout.py launch --id <id>` is the only source of a Shuttle prompt, and it records the row in `.warp/agents.json` before it prints. While the run is paused, stopped, or finished, and for a merged or parked ticket, it prints `spawn: closed <id>` and `refuse: ...`, prints no prompt, and exits 2. Lines above `prompt: pass every line below this one to the subagent, and nothing above it` are for the parent. Lines below it are the worker's prompt.
- The contract. Every worker prompt carries `agent: <id>`, the worker contract (one step, one result line, start nothing, wait on nothing), and the reap command. Cursor can wake a cloud agent that opened a pull request when CI fails or a comment arrives, depending on the Cloud Agents settings for the account. A worker woken that way runs the reap check and returns without acting.
- The tag. Cursor has no tag field on an agent, so the tag is the name: `[warp:<instance>] <ticket> <step>` for a Shuttle and `[warp:<instance>] listener` for a poll. `checkout.py launch` prints the `name:` line and the parent uses it. Pause, stop, and `/warp-cleanup` act only on agents in the registry or carrying this run's tag, so other agents in the repo, and another Warp run's, are never touched. `/warp-list` lists this run's tagged agents with their status. `--tag <instance>` names another run's tag in place of this one. `--tag all` matches the tag of any Warp run. `--untagged` matches agents from before the tag, loosely.
- The reap check. `agents.py reap --beam .warp/beam.json --id <agent> --ticket <id>` prints `reap: continue`, or `reap: exit <reason>` and exits 3. A Shuttle on its own VM passes `--remote`. The reasons are `paused`, `stopped`, `done`, `settled`, `replaced`, `halted`, `archived`, `not-resumed`, and `not-launched`. `ticket_state.py append`, `beam.py heartbeat`, and `inbound.py poll` print the same line.
- The halt. `/warp-pause` and `/warp-stop` run the same teardown. The one difference is that stop also writes the completion report and records `stoppedAt`. Every row in `.warp/agents.json` is ended (`stop: <id>`) and marked halted, every Shuttle slot is freed, the listener's poll is closed (`halt: paused agents=<n>` or `halt: stopped agents=<n>`), and the state is pushed. With `CURSOR_API_KEY`, every cloud agent in the registry, and every agent in this repo whose name carries this run's `[warp:<instance>]` tag, has its run cancelled and is archived. The agent running the command and this run's parent are skipped. Without the key the line is `cleanup: link https://cursor.com/agents/<id>`. Pause does not leave a Shuttle running. Branches, worktrees, pull requests, locks, and `jira.startedAt` stay. Nothing from before a halt is resumed: `/warp-start` or `/warp-resume` launches a new Shuttle with a new agent id on the same branch (`fix: <id> resume after halt`), counts no recovery, and tells nobody a worker died. The same teardown runs once when every ticket is merged or parked (`halt: done`).
- The list and the clear. `/warp-list` (`agents.py list --beam .warp/beam.json`) shows what is still out for this run, running or idle, and changes nothing. `/warp-cleanup` (`agents.py cleanup --beam .warp/beam.json --cloud --apply --running`) is the clear you run yourself: it cancels the run of each one that is running and archives all of them. Leave `--apply` off for a dry run. While the run is running it archives the idle ones and refuses the running ones unless `--force`. Both take the same selection: `--tag <instance>`, `--tag all`, `--untagged`, `--all-idle`, and `--any-repo`.

The one wait in a run is `orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>`. It prints `pass: ticket <id>`, `pass: command`, `pass: return`, `pass: halt`, or `pass: timeout`, then `parent: stay`, `parent: exit`, or `parent: exit superseded`. `scan.py start` and `scan.py resume` print `parent: session <id>`. One parent per run: a later start or resume takes the run over.

One ticket, one cloud agent. A Shuttle on its own VM writes `cloudAgent` in its ticket `state.json`, and the parent binds it as `cloudId` on the registry row. A fix round that lands on a new VM retires the VM the ticket left. Merge and park archive every VM the ticket used.

## Dead workers

A Shuttle on a claimed ticket is the background worker. The one channel listener is one background subagent. A stale heartbeat does not start another. The return stamp does, so the watchdog does not recover a listener. A dead turn does not notify Warp. Cursor does not restart it. Plugin hooks do not run on cloud runners. There is no process table.

A Shuttle writes a heartbeat on the beam: `lastSeenAt` and its agent id. It runs `beam.py heartbeat` at claim, at each status change, and at least every 5 minutes while the turn is alive, always inside `staleMinutes` (default 15). A heartbeat is not a timer: the Shuttle writes it between steps of work it is already doing, and reads the `reap:` line the command prints. The listener writes no Shuttle heartbeat. The parent writes `listener.parentSeenAt`. Each read sets `listener.lastSeenAt` on the beam and `lastPollAt` in `.warp/listener.json`. `inbound.py heartbeat` exists for a listener someone starts by hand. A run does not use it. `/warp-init` backfills `staleMinutes` and `maxRecoveries` when `.warp/config.yaml` does not have them yet.

`beam.py watchdog` runs on every `/warp` tick and on `/warp-start` and `/warp-resume`. A Shuttle is dead when `lastSeenAt` is older than `staleMinutes`, or it never heartbeated and the claim is older than that. A fresh heartbeat is left alone. Do not start a second worker. The watchdog prints no listener line and never starts a listener. Pause and stop do not replace anyone. They end every agent, and `/warp-start` or `/warp-resume` launches a new Shuttle on the same branch (`fix: <id> resume after halt`). That is not a recovery.

The listener is a Subagent of the parent. Start one subagent per ticket in its own worktree, in parallel up to maxAgents. The listener is one background subagent on the parent's checkout. `subagentVm` does not apply. `inbound.py wait` sleeps in Python between reads. `orchestrator.py supervise` prints `listener: poll` and the prompt (`LISTEN`) when no holder is live and the return stamp is in, and the parent starts exactly one `warp-listen` Subagent in the background and does not wait. Each cycle runs `inbound.py poll --lease` before any read. `listener: hold reason=live` and `listener: hold reason=await-return` mean start nothing. A stale heartbeat does not start one. `listener: idle` and `listener: none` mean start nothing. `parent-exit` prints `parent: stay` while any ticket is not merged or parked. If the parent's turn dies, nothing restarts it. Workers finish their step, write their ticket folder, and return. Close the window, open a new one, `/warp-start`. State comes from main plus ticket folders.

A dead Shuttle stays on the same branch, pull request, `jira.startedAt`, and locks. Status becomes `recovering`, and exactly one new Shuttle is dispatched for that same ticket. Herald posts `<id> worker died. A new Shuttle started.` It is not released, so another ticket cannot take the files. Past `maxRecoveries` (default 5) the ticket is `alarm` / `worker-died` and no new Shuttle starts.

## Remote status

The claim is in the subagent prompt: ticket, Jira key, locks, acceptance, branch, absolute worktree path, and the subagent's agent id (`agent: <id>`). The prompt comes from `checkout.py launch` and carries the worker contract and the reap command. The subagent does not commit updates to `.warp/beam.json`.

Status is written straight into the parent checkout. Only that subagent writes it, by absolute path:

```
.warp/tickets/<id>/state.json
.warp/tickets/<id>/log.jsonl
```

`state.json` is the current state: when it started, when it last changed, the last heartbeat, the pull request, the check result, the error text, the alarm reason, and any paths that escaped the lock. `log.jsonl` is an append-only mini log. Each line has a timestamp, a state, and the error text.

The subagent writes that directory in the parent checkout and does not push it. `ticket_state.py append --id <id> --state <state> --root <parent>` does that. It prints `reap: continue` or `reap: exit <reason>`, so the subagent reads whether to go on every time it writes its state. On `reap: exit` the subagent stops and returns `result: <id> stopped <reason> agent=<agent>`. The command does not commit `.warp/beam.json`. Optional `launch: agent` may still pass `--push`.

Every tick reads a worktree ticket's directory from the parent checkout. It fetches an agent-mode branch and reads only that ticket's directory. It patches the live beam. It does not replace the live beam with a file from the branch. A second tick applies the same lines once. Paused and stopped runs do not fetch and do not relaunch.

A subagent failure returns straight to the parent, which raises `worker-died` from that result. The heartbeat watchdog stays for a parent that dies mid-turn. The parent uses `heartbeatAt` in `state.json`, or the last `heartbeat` line in the log. If the worker never heartbeated, it uses the launch time already on the live beam. Older than `staleMinutes` means the worker died. One subagent starts again on the same worktree and branch. `/warp-start` and `/warp-resume` reuse them. The branch is not rebased. Branch, pull request, locks, and `jira.startedAt` stay. Past `maxRecoveries` the alarm is `worker-died`. Optional `launch: agent` starts one new Agent on that branch instead.

A `lock-escape` line is an alarm with the escaped paths, even though the Agent has already exited. The repair pass can widen the lock and retry. The parent does not wait for the Agent to still be running.

A `check-red` line is a failed check (`make ci`, Bugbot, or both). The ticket is sent back on the existing path. Past `maxFixAttempts` (default 5) the ticket parks. If the directory never recorded a check, the pull request's check rollup is used instead. A dead Agent that already opened a pull request still reports check-red or check-green from that rollup.

`planning`, `coding`, `review`, `bugbot`, and `fix` are the transient states while the Agent is alive. Herald posts one line when one of these changes the live beam, and one line when an Agent is declared dead.

Those directories can merge to main. They do not overlap. The beam file does not. A squash merge keeps `.warp/tickets/<id>/` and leaves the base beam in place.

## Alarms

The parent tick repairs `lock-escape` and no other alarm, one ticket at a time. It checks while the run is running, on the cadence of `alarmRepairMinutes` (default 15). Pause and stop do not repair. Plugin hooks do not run on cloud runners. The tick calls `scripts/alarm_repair.py next` and starts one subagent in that ticket's worktree. The listener Subagent does not. A second call in the same moment does not start a second repair.

The Shuttle that hits the alarm stores each path outside the lock with `beam.py set --alarm lock-escape --escaped <path>`. Those paths live on the ticket as `escaped`. The repair widens that ticket's locks to those paths, records them on `addedLocks`, and says which paths it added in the Herald line. It does not take a path an in-flight ticket currently holds. It waits, then widens and starts after that holder finishes. A queued ticket's overlapping lock can be widened. An older alarm with no path list prints `alarm-repair: name <id>`. That Shuttle names the paths with `alarm_repair.py paths` before any edit.

One repair runs at a time, one ticket at a time, in plan order. If it alarms `lock-escape` again, or errors, the alarm stays, the attempt is recorded, and the next lock-escape ticket starts in that same call. The one that just failed is not retried in that pass. If it returns the ticket to the normal path with the alarm cleared, or the ticket merges, the parent waits for that completion, then starts the next. `maxAlarmRepairs` (default 5) leaves the alarm in place and later passes skip it. Herald posts one line when a repair starts, when a failure takes the next ticket, and when a ticket is given up. `bugbot-failed`, `stuck`, `worker-died`, `ci-red`, and `gate-red` are left alone by `alarm_repair.py`. `orchestrator.py supervise` restarts a `worker-died` step, starts a fix Shuttle for `ci-red` and `bugbot-failed`, and restarts `stuck` up to `maxRecoveries`. `gate-red` stays on the gate.

`/warp-init` backfills `alarmRepairMinutes` and `maxAlarmRepairs` when `.warp/config.yaml` does not have them yet.

When that pass opens and the ready set is empty, the parent recomputes every pending gate, and a red gate whose failure the beam can already disprove. A red `make ci` on a ticket is sent back in that pass (`send-back <id> fix` and a `start` line) even when other work is ready. The command is the full string `make ci`. The result is stored on `pr.check`. Past `maxFixAttempts` (default 5) the ticket parks. The name alone is not a red check. Launch that Shuttle with the log. A second pass does not start it again while it is in `fix`. A pending gate, or a stale red gate, turns green when every member is merged or done and no check is actually red, or when every ticket on the beam is merged or done. Then that pass runs the tick. G1 with members P-002, P-003, P-004, and P-018 stores evidence `members merged: P-002, P-003, P-004, P-018` and the board is rewritten. Herald posts `G1 pending cleared. Members merged. Tick ran.` A member that is not merged, a ticket that is still alarmed or parked, or a check that is actually red leaves the gate as it is. A recovered ticket that is merged counts. A ticket that is still alarmed does not. A green gate stays green. Every tick recomputes before `beam.py ready` and before `orchestrator.py dispatch`, so a merge does not wait for this pass. The pass is the backstop when no tick is running. Pause and stop do not recompute.

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

1.5.0 changes how agents end on an existing install. `/warp-pause` now ends every agent, with the same teardown as `/warp-stop`. The one difference is that stop also writes the completion report and records `stoppedAt`. Pause no longer leaves in-flight Shuttles running. `/warp-start` and `/warp-resume` are now the same command. Either one launches a new Shuttle on the same branch for each ticket that had one out, before any new ticket. The listener is one background subagent, replaced only from its return stamp, so the config keys `listenerRestartNote` and `maxListenerRestartsPerHour` are gone, and `listenerStaleMinutes` is retired too. There are two caps now, `maxAgents` and `maxInProgress`, so `maxLocalSubagents` and `maxFixWorkers` are retired and ignored. An old `.warp/config.yaml` that still has any of those keys is fine: they are ignored, and start and `/warp-version` print `config: <key>: <value> is retired and ignored.` for each one. `/warp-list` is new, and `/warp-cleanup` now cancels and archives what it shows. Set `CURSOR_API_KEY` in the environment to let pause and stop cancel and archive cloud agents. Without it they print one link per cloud agent. The commands are under [Run control](#run-control). The rule is under [Agent lifecycle](#agent-lifecycle).

`/warp-init` still does not overwrite plugin files or config values you already set. It does append config keys that are missing, with the defaults and comments from `assets/config.example.yaml`. A config that is only missing new keys does not need an upgrade. Re-run `/warp-init`. It appends each missing key and prints the names it added (`jiraKeyPrefixes`, `jiraKeyMap`, `jiraExternalIdField`, `jiraWriteExternalId`, `jiraSite`, `staleMinutes`, `maxRecoveries`, `alarmRepairMinutes`, `maxAlarmRepairs`, and any later key). Values you already set stay. Readers use the same defaults when a key is still absent.

`subagentVm` defaults to false. `runner: local` forces it false and forces `launch` to `worktree`, skips the cloud-environment start check, and logs one note. Start, `/warp-version`, and status print the effective values: `effective: runner=<r> subagentVm=<b> memoryCheck=<b> maxAgents=<n> maxInProgress=<n> launch=<l>`. `memoryCheck` defaults to false, so only `maxAgents` (default 18) limits how many agents run at once. When `memoryCheck` is true, `free -g` can lower `maxAgents` on a shared machine. On a cloud runner, `subagentVm: true` makes `checkout.py launch` print a prompt that says: run in your own cloud environment on a dedicated VM with its own clone and branch, not a git worktree on this machine. The subagent fetches the latest main, clones it, and creates `warp/<id>-<jira>` itself. Its first step is `hostname` and `free -g`. It writes `.warp/tickets/<id>/` on that branch and pushes. The parent patches the beam from those folders. `checkout.py verify` compares the hostname with this machine. The same hostname is a worktree fallback: one warning per run, then the shared-machine rules. `subagentVm: false` fetches origin and adds a git worktree under `worktreeRoot` (default `.warp/worktrees`). Start one subagent per ticket in its own worktree, in parallel up to `maxAgents`. The subagent works only inside that path. `checkout.py verify` runs after it. `checkout.py implement` refuses to do that work in the parent checkout. Cloud subagents use the MCP servers at cursor.com/agents, not this session. A cloud start warns, and `--force` allows it, when `.cursor/environment.json` and `cloudSnapshot` are both missing. In the Agents Window, `/in-cloud` runs one task as a cloud subagent on its own VM and branch. That is a manual fallback, one ticket per invocation. Optional `launch: agent` prints an IMPLEMENT prompt for one new Agent, for a future Cursor Cloud Agents API. This plugin does not call that API. Clone main so `.cursor` rules load, then create the branch. A beam file does not have to exist on the branch before that Agent starts.

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

**Agents piled up, or one is still running after a pause.** `/warp-pause` and `/warp-stop` end every agent in `.warp/agents.json` and print `halt: paused agents=<n>` or `halt: stopped agents=<n>`. With `CURSOR_API_KEY` in the environment they cancel and archive this run's cloud agents. Without it they print `cleanup: link https://cursor.com/agents/<id>`: archive those in the Cursor UI. A subagent that shares the parent's session cannot be cancelled from outside. It exits at its next reap check. `/warp-list` shows what is still out, running or idle. `cleanup: cut short` means the teardown hit its time limit: run `/warp-cleanup`. A pile left by a run before 1.5.0 carries no tag: `/warp-stop`, then `/warp-list --untagged` to read it, then `/warp-cleanup --untagged`. For what another run left behind after its beam is gone, pass `--tag <instance>`, or `--tag all` for the tag of any Warp run.

**`spawn: closed <id>` and no prompt.** `checkout.py launch` printed `refuse: ...` and exited 2 because the run is paused, stopped, or finished, or that ticket is merged or parked. Start nothing. `/warp-resume` or `/warp-start` opens the run again.

**A Shuttle returned `result: <id> stopped <reason> agent=<agent>`.** The parent stamps `reaped` on that agent only. A late line from a replaced VM does not end the new Shuttle. It read `reap: exit <reason>` and stopped. `paused` and `stopped` are a halt. `settled` means the ticket is merged or parked. `replaced` means the parent started another Shuttle for that ticket. `not-launched` means no launch was recorded for that agent id: something other than the parent started it. This is not a dead worker and is not counted as a recovery. The reasons are in [docs/LIFECYCLE.md](docs/LIFECYCLE.md).

**No listener starts.** `listener: none` means no `slackChannel` or `teamsChannel` is set. `listener: hold` means a poll is out or the next one is not due. `listener: idle` means the run is paused, stopped, or finished. The listener stays up. Channel reads use `listenerSlowSeconds` (60) and `listenerFastSeconds` (15). `pollSeconds` is only the parent-wait cap.

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
| Start | `/warp-start` or `/warp-resume`. They are the same command. | `run: was stopped. Starting.` Herald posts. Unfinished work starts first, then Shuttles claim up to `maxAgents`. |
| Pause | `/warp-pause` | `halt: paused agents=<n>`. No new claims. Every agent is ended. Branches, worktrees, and pull requests stay. No completion report. |
| Continue | `/warp-start` or `/warp-resume` | `run: was paused (<reason>). Continuing.` New Shuttles start on the surviving branches, unfinished work first. |
| Download | Ask for a bundle, or download `STATUS.md`. | The zip or the file on your machine. |
| Edit | Edit `WARP_PLAN.json` locally. Do not edit `beam.json`. | Changed deps, locks, sizes, or gates. |
| Upload | Drag the JSON into the chat, or drop `WARP_PLAN.edited.json`. Do not commit it. | The agent can see the path. |
| Import | `/warp-import` on that path. | Plan replaced. Run stopped. Status kept for surviving ids. |
| Restart | `/warp-start` | Dispatch uses the new graph. |
| Stop | `/warp-stop` | `halt: stopped agents=<n>`. The same teardown as Pause: every agent is ended. It also writes the completion report and records `stoppedAt`. Stays stopped until `/warp-start` or `/warp-resume`. |
| Remove | `/warp-uninstall`, then confirm. | The plugin copy and `.warp/` are gone. `/warp-init` installs again. |

Pause and stop run the same teardown: every agent is ended and the work stays. The one difference is that stop also writes the completion report and records `stoppedAt`. Either way the run carries on with `/warp-start` or `/warp-resume`, which are the same command and start new Shuttles on the surviving branches, unfinished work first. The table is under [Run control](#run-control). Each repo has its own `.warp/`, so stopping one project does not stop another.

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
| `.warp/agents.json` | No | The one registry of agents: every spawn and every exit is a row. `/warp-list` lists it. Scripts write it. |
| `.warp/listener.json` | No | The listener lease: `lastPollAt`, `reason`, `generation`, `holder`, and `returnReason`. |

## Status

`/warp-status` rewrites `.warp/STATUS.md` and `.warp/status.json`. Working means claimed, recovering, coding, review, fix, or waiting on approval. `recovering` is a Shuttle whose heartbeat went stale: the same branch is kept and one replacement is started. Done means merged or moved to Done in Jira. Left is everything else. `.warp/BOARD.md` and `board.html` add gates, alarms, and ETA. When `.warp/warp-complete.html` exists, the status file names it.

`/warp-status`, and the status printed by `/warp-start` and `/warp-resume`, then list every ticket that is not merged. Alarms and stalls come first, then a count of each state. Each row has the state, the current step, how long it has been in that state, when it was last active, the pull request, whether Bugbot, CI, and the acceptance criteria passed, the stall flag, the retry count, any alarm, and the next action. The same rows are stored on the beam as `openWork`, so a new session sees them.

## Completion report

When every ticket is merged, done, skipped, blocked, or alarmed, or when the run is stopped, Warp writes `.warp/warp-complete.html` and Herald posts the headline totals plus that path. The file is gitignored with `.warp`. It is not on the remote. `reportOnComplete: false` skips the automatic write. `/warp-report` still writes it, and `--partial` writes a snapshot while work is in progress.

```bash
python3 <plugin>/scripts/report.py --beam .warp/beam.json
python3 <plugin>/scripts/report.py --beam .warp/beam.json --partial
python3 <plugin>/scripts/report.py --beam .warp/beam.json --out .warp/warp-complete.html --open
```

The page is one self-contained HTML file. Waves are concurrency-run segments: tickets active at the same time, from claim until the status leaves the working set. A gap with nobody running is not a wave. A handoff that keeps the count the same but changes who is running is two waves. Token and cost figures come from `beam.py usage` (or the same flags on `set`). If the Cursor run did not report them, the page says n/a. It does not invent numbers.

## Status in Teams or Slack

The listener is a Subagent of the parent. Start one subagent per ticket in its own worktree, in parallel up to maxAgents. One listener Subagent reads the configured Slack channel, and Teams when `messenger` is `teams` or `both`. It is not a separate Agent. Not one per awaiting_approval ticket, not one per Shuttle, not one per Reed. It shares the parent's session and checkout. It is one background subagent. `subagentVm` does not apply. `inbound.py wait` sleeps in Python between reads. `orchestrator.py supervise` prints `listener: poll`, then the prompt (first line `LISTEN`), when no holder is live and the return stamp is in. The parent starts exactly one `warp-listen` Subagent in the background with those lines and does not wait. When `listenerModel` is unset the prompt says `model: inherit`. When it is set the prompt says `model: <slug>`. Each cycle runs `inbound.py poll --lease --holder` before any read, which prints the reap line and `listen: slack channel=<name> since=<cursor>` for each channel. A lease mismatch prints `reap: exit superseded` and no channel line. It reads each channel once from that mark, runs `inbound.py accept` for each `warp:` command, posts the ack, and queues the command with `inbound.py enqueue`. Both take `--message-id`. A duplicate Slack ts prints `inbound: duplicate` and writes no second ack. Then `inbound.py wait` sleeps `listenerFastSeconds` after a queued command and `listenerSlowSeconds` after an empty read. The parent applies queued commands with `inbound.py apply-pending` and merges a proceed. The listener does not merge and does not implement tickets. `/warp-start` and `/warp-resume` do not claim a listener. `listener: hold` means a poll is out or the next one is not due: do not start a second. `listener: idle` means the run is paused, stopped, or finished. `listener: none` means no `slackChannel` or `teamsChannel` is set, so no listener is started at all. A poll that never reports is issued again after `listenerStaleMinutes`. `/warp-pause` and `/warp-stop` close the poll, run `inbound.py release`, and set `listener.state` to `stopped`. The listener must not keep reading while paused or stopped.

The slot is `listener` on the beam. `state` is the flag: `running` while a holder has the lease, `stopped` between generations and while the run is paused or stopped. `generation` and `holder` are the lease. `parentSeenAt` is the parent heartbeat. `returnReason` is `polled`, `rotated`, `orphaned`, or `stopped`. `agentId` is `listener` for a generation the parent started. `pollStartedAt` is set when the generation starts and cleared by the return stamp. `lastSeenAt` is the last read. `cursor` is the newest message that read reported. `reads` counts reads in this generation. `lastCount` and `polls` count the commands in the last read and the generations in the run. `.warp/listener.json` stores `agentId`, `lastPollAt`, `reason`, `generation`, `holder`, and `returnReason`. `.warp/agents.json` keeps one `listener` row for the run. `inbound.py claim` and `inbound.py heartbeat` remain for a listener someone starts by hand. A run does not use them.

Cursor cannot start that listener from a Slack message. There is no webhook, and plugin hooks do not run on cloud runners. The parent is the one long-lived agent. It starts the next generation only from the return stamp. The parent ends every pass with the one wait in a run, `orchestrator.py parent-exit --beam .warp/beam.json --wait --session <session>`. `<session>` is the id `scan.py start` and `scan.py resume` print as `parent: session <id>`. The wait blocks until a ticket folder changes, the inbound queue changes, the listener writes its return stamp, the run halts, or `pollSeconds` pass, and prints `pass: ticket <id>`, `pass: command`, `pass: return`, `pass: halt`, or `pass: timeout`. Each step writes `listener.parentSeenAt`. Then it prints `parent: stay` while any ticket is not merged or parked, `parent: exit` when every ticket is merged or parked, or the run is paused or stopped, and `parent: exit superseded` when a later `/warp-start` or `/warp-resume` took the run over. Each pass runs `inbound.py apply-pending` and supervise, fetches `.warp/tickets/<id>/` for in-flight tickets, patches the live beam, dispatches a ready slot, pushes the beam, and waits. A launch refusal is not idle. Pause and stop still sync state, and they close the poll.

On every merge the parent commits the live `.warp/beam.json`, journal, and board onto main. The ticket branch's copy of the beam is dropped. The same beam is pushed at the end of each parent tick. Tokens, cost, API keys, and webhook URLs are stripped. `.warp/config.yaml` is not committed. Close the window, open a new one, `/warp-start`. State comes from main plus ticket folders. `/warp-start` and `/warp-resume` fetch origin/main, load that beam when the checkout's beam is missing or empty, then fetch each in-flight ticket branch and patch from `.warp/tickets/<id>/`. `/warp-update-state` is the insurance sync: it always loads main, patches `.warp/tickets/<id>/` only, puts back pause, stop, claims, and runState the parent has that main does not, and pushes that beam. `/warp-pause` and `/warp-stop` run that sync after they set the state, so main shows paused or stopped before the command returns. The registry is pushed with it. A Shuttle on its own VM reads the halt from there with `agents.py reap --remote`. If `/warp-update-state` is not in the command list, run `python3 .cursor/plugins/warp/scripts/update_state.py --beam .warp/beam.json`. `python3 .cursor/plugins/warp/scripts/update_state.py ?` prints the flags. That script is already on a 1.4.12 install and does not need the slash command. `/warp-version` prints the same lines.

Every recognized `warp:` command is acknowledged in the channel before it runs. The ack names the command and, when there is one, the ticket id. Unknown or malformed commands get an ack that says they were not understood and lists the accepted forms.

- `warp:proceed <id>` merges that one `awaiting_approval` ticket and moves Jira to Done (`jiraDoneOnManualMerge`). The id is the plan id or the Jira key. A bad id is acknowledged and nothing else is merged. The ack is `Received warp:proceed XV-01. Merging and moving Jira to Done.`
- `warp:pause`, `warp:resume`, `warp:stop`, `warp:start`, `warp:retry <id>`, and `warp:status` are acknowledged, then run. The listener queues them, and the parent applies them when the poll returns. `warp:pause` and `warp:stop` are the same teardown as `/warp-pause` and `/warp-stop`: every agent is ended. `warp:start` and `warp:resume` are the same command, and do nothing while the run is already running.
- In the agent window, `/warp-status-post` still posts the digest and attaches `STATUS.md`, `status.json`, and `BOARD.md`. `warp:status` does that after its ack.

Set `teamsChannel` and `slackChannel` in `.warp/config.yaml`. If a connector is missing, the text is appended to `.warp/outbox.md`. Warp does not store tokens. Teams and Slack cannot pull files from a stopped agent.

## Configuration

One file, `.warp/config.yaml`. Change it, then restart, so the next tick re-reads it.

| Key | Default | Meaning |
|---|---|---|
| `stateDir` | `.warp` | Beam and exports. Gitignore it. |
| `model` | `claude-sonnet-5-5-high` | Coding slug: Claude Sonnet 5.5 High. Must match the Cursor model picker. |
| `maxAgents` | `18` | Agents that run at once: every Shuttle, whatever its step (implement, fix, rebase, rerun, restart, repair), in a worktree on this machine or on its own VM. The one cap on running agents. A dead Shuttle does not count. `memoryCheck` can lower it on a shared machine. Separate from `maxInProgress`. |
| `maxInProgress` | `20` | Tickets that are open at once: started, and not merged or parked. A ticket waiting on review or CI is open and uses no agent. At the cap, nothing new leaves the ready queue. Fixes, rebases, reruns, and merges keep running, because those tickets are already open. Parked tickets do not count. Status says `holding new launches (N/20), N fix workers running`. |
| `checkCommand` | empty | Optional command the orchestrator runs on a rebased head before merge. Empty means Bugbot and CI as configured. `make ci` is kept as the full check command. A red result is stored on `pr.check` and the ticket is sent back with the log. Past `maxFixAttempts` (default 5) the ticket parks. The name alone is not a red check. |
| `appendOnlyPaths` | empty | Shared files a ticket may append to, besides its locks. Empty by default. A conflict there keeps both sides. Any other conflict is sent back. |
| `mergeQueue` | `false` | `true`, or a GitHub merge queue the provider reports, enqueues the pull request. A direct merge that branch protection rejects is not marked merged. |
| `autoMergeSizes` | `S, M` | Auto-merge after Bugbot and CI, including L and XL when they are listed. L and XL are not special. Sizes not in `autoMergeSizes` wait. The default leaves L and XL waiting. |
| `messenger` | `both` | `slack`, `teams`, or `both`. |
| `notify` | `verbose` | Every claim and tick, plus init and scan. `quiet` posts alarms, approval waits, a red gate, and pause/stop. `warp:status` is always answered. |
| `runner` | `cloud` | The main driver. `local` forces `subagentVm` false and `launch` to `worktree`, and skips the cloud-environment check. Start, `/warp-version`, and status print the effective values. |
| `subagentVm` | `false` | Worktree on this machine. `true` on a cloud runner asks for a dedicated VM. `runner: local` forces false. |
| `memoryCheck` | `false` | `true` lets `free -g` lower `maxAgents` on a shared machine when available memory is under 2 GiB per extra subagent. At least one still runs. `false` uses only `maxAgents`, and `free -g` is not read. |
| `cloudSnapshot` | empty | Snapshot id that counts as a cloud environment beside `.cursor/environment.json`. |
| `worktreeRoot` | `.warp/worktrees` | Parent of `<id>` worktrees. Gitignored. Used when `subagentVm` is false or a VM falls back. |
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
| `jiraInReviewStatus` | `In Review` | Bugbot request, or the pull request opening when Bugbot is off. A missing key keeps `In Review`. An empty string or null turns the move off. A fix send-back stays here. |
| `jiraQaReadyStatus` | `QA Ready` | Manual path (sizes not in `autoMergeSizes`): Jira moves here after Bugbot is clean and CI is green, and waits here until the merge. |
| `jiraDoneStatus` | `Done` | Jira moves here after the merge, auto or manual. |
| `jiraDoneOnManualMerge` | `true` | `false` leaves a manual merge at QA Ready so QA can set Done. |
| `jiraRestoreOnRelease` | `false` | Move the issue back when a claim is released to `queued`. Otherwise it is left alone. |
| `bugbotRequired` | `true` | Both paths. No auto-merge and no QA Ready until Bugbot passes. |
| `bugbotManual` | `true` | Manual tickets only. `false` skips Bugbot before QA Ready. Auto-merge still uses `bugbotRequired`. |
| `maxFixAttempts` | `5` | Shared fix loop, then the ticket alarms. |
| `stuckAfterMinutes` | `90` | No update in this window raises stuck. Separate from a dead worker. |
| `ciStartGraceMinutes` | `5` | Minutes to wait for the first check run. Zero checks after this starts CI in that pass. A conflict is rebased first. |
| `staleMinutes` | `15` | Shuttle heartbeat older than this, or no heartbeat and a claim older than this, means the Shuttle is dead. The watchdog replaces it while the run is running. Pause and stop do not. A Shuttle whose turn returned this long ago and was not resumed reads `reap: exit not-resumed` if it wakes up. `/warp-init` backfills this key. |
| `listenerFastSeconds` | `15` | Seconds `inbound.py wait` sleeps after a cycle that queued a command. |
| `listenerSlowSeconds` | `60` | Seconds `inbound.py wait` sleeps after an empty read. |
| `listenerOrphanMinutes` | `5` | A `listener.parentSeenAt` older than this exits the listener as orphaned. A stale stamp does not start a replacement. |
| `listenerMaxMinutes` | `60` | Rotate a generation after this many minutes, or `listenerMaxReads`, whichever comes first. |
| `listenerMaxReads` | `60` | Successful leased polls in one generation before rotation. |
| `listenerModel` | empty | Empty inherits the parent's model (`model: inherit`). A slug is `model: <slug>`. |
| `maxRecoveries` | `5` | Shuttle replacements on one ticket. The next death is `alarm` / `worker-died` and does not start another. `/warp-init` backfills this key. |
| `alarmRepairMinutes` | `15` | How often the parent tick opens a lock-escape repair pass. The listener does not start the repair Agent. Pause and stop do not. `/warp-init` backfills this key. |
| `maxAlarmRepairs` | `5` | Repair attempts for one lock-escape ticket. The repair widens that ticket's locks to the paths that escaped. Past the cap the alarm stays. `/warp-init` backfills this key. |
| `repairSweepMinutes` | `15` | How often supervise sweeps the whole run for broken tickets, pull requests, CI, locks, gates, the listener, and the beam. It skips work already queued or running. Slack posts only when the sweep starts something new or escalates. |
| `respectMergeWindows` | `false` | True makes new claims wait for a window. |
| `mergeWindows` | `08:30, 13:00, 17:00` | Digest times, or claim gates if the flag is true. |
| `pollSeconds` | `300` | Longest the parent waits between passes (`orchestrator.py parent-exit --wait`) when nothing else wakes it. Channel reads use `listenerFastSeconds` and `listenerSlowSeconds`. |
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
| `slackChannel` / `teamsChannel` | `warp` | Shared channel the one listener reads, and Herald posts to. Must already exist. With neither set, no listener is started (`listener: none`). |
| `projectName` | empty | Shown after the repo in message headers. Empty uses `jiraProject`, then the folder name. |
| `notifyAllow` | empty | Extra `server:tool` pairs for `/warp-allow-notify`. No wildcards. |
| `autoAllowTools` | `true` | `/warp-init` writes the project MCP allowlist. `false` or `--no-allow` skips it. |

Retired keys: `maxLocalSubagents` and `maxFixWorkers` are retired and ignored. `maxAgents` is the one cap on agents that run at once, on this machine or on their own VMs, and a fix is an agent like any other and counts against it. A config that still has them prints `config: <key>: <value> is retired and ignored.` at start and in `/warp-version`. Remove them from `.warp/config.yaml`. The two caps are under [Run control](#run-control).

## Pipeline

`orchestrator.py supervise` and the dispatch tick carry every ticket. The phases are `implementing`, `pr-open`, `reviewing`, `fixing`, `ready`, `merging`, then `merged` or `parked`. Opening a pull request is not done. Each step that changes code is a Shuttle subagent on that ticket's branch or worktree. The Shuttle runs `checkCommand` and reports every acceptance criterion as pass or fail. Failed criteria come back as a fix run. The parent requests Bugbot on the pull request (`bugbot: request`), waits for it, and reads the findings and the check rollup. A finding, red CI, or a failed acceptance criterion starts `step=fix` on the same branch. After the push, Warp asks for review again. Past `maxFixAttempts` (default 5) the ticket parks. The merge gate is Bugbot finished with no unresolved findings, a green rollup, passing acceptance criteria, and a size in `autoMergeSizes` or a manual proceed. The serial merge queue runs on every supervise pass. After each merge the pass prints `herald:`, `jira: MUST DO`, `slack:`, and `beam: push`. A merge, a park, or a green rollup frees the slot and starts the next ready ticket, up to `maxAgents`. `/warp-start` and `/warp-resume` adopt an open pull request into `reviewing` or `fixing` and do not relaunch that ticket from scratch. `checkout.py verify` raises `lock-escape`. That supervise pass calls `alarm_repair.py next`, which widens the locks and reruns the Shuttle. The parent stays up until every ticket is merged or parked, or the run is paused or stopped. The listener stays up on the parent's checkout. `pollSeconds` is only the parent-wait cap.

With no `--provider` file, that pass loads each open pull request from GitHub and writes the head sha, rollup, CI, mergeable state, and conflicting files. A CONFLICTING or DIRTY pull request rebases in that pass, and route registration files keep both sides. A head with no check runs after `ciStartGraceMinutes` (default 5) starts CI in that pass. A green check for the current head beats a stale pending rollup. Acceptance results fall back to the Shuttle `RESULT.json`. Bugbot findings belong to the head sha.

The same pass watches every ticket that is not merged. A state that makes no progress within its limit is stalled and the reason is appended to `.warp/tickets/<id>/log.jsonl`. Progress is a new commit, a phase change, or a check result change. A heartbeat, or a start that does not change those, does not clear the stall. A conflict, CI that never started, a stale rollup, or a head sha mismatch is fixed on that pass. The stall timer is only for a stall that cannot be classified. Limits are `stallImplementingMinutes` (90), `stallPrOpenMinutes` (20), `stallReviewingMinutes` (45), `stallFixingMinutes` (90), `stallReadyMinutes` (30), `stallMergingMinutes` (20), `stallQueuedMinutes` (180), `stallApprovalMinutes` (240), and `stallMinutes` (45). Each stalled or alarmed ticket gets one fix, up to `maxStallFixes` (default 5): ask Bugbot again, rerun CI, rebase onto main, restart a silent Shuttle, widen a lock-escape, clear a stale gate, start a fix when the base branch is red, or return an approved pull request to the merge queue. Past the cap the ticket parks and Slack gets one alarm. A short digest posts every `statusDigestMinutes` (default 60). A new stall or alarm posts once. Every `repairSweepMinutes` (default 15) that pass also sweeps the whole run: what is broken, and what can be fixed that is not already queued or running. Broken covers tickets, pull requests, CI on main, locks, gates, the listener, and the beam, including red or stuck CI, a merge conflict, unresolved Bugbot findings, a stale or orphaned lock, a red base, a stuck gate, a listener with no holder, and a beam that is out of sync with main. The sweep reuses the fixer paths, starts work up to the slot cap, and logs findings and actions. `/warp-status` shows the last sweep. Slack is posted only when the sweep starts something new or escalates.

## Merge policy

The orchestrator is the only merger. Ready means every blocker is merged on the base branch and no lock overlaps an in-flight ticket, including a parent folder. Locks stay until merge or park. A launch slot is a live Shuttle, not a ticket that is only waiting on review. A dead Shuttle releases that slot immediately. The merge queue is serial. Sizes outside `autoMergeSizes` still wait for `/warp-proceed` or `warp:proceed`. Past `maxFixAttempts` (default 5) the ticket parks. A red base branch stops merging. On start or resume, restart classifies each ticket from git: merged, in flight, or pending. One worktree per ticket. Start one subagent per ticket in its own worktree, in parallel up to `maxAgents`. `maxAgents` caps the agents that run at once. `maxInProgress` (default 20) caps tickets that have started and are not merged or parked. At that cap, nothing new is taken from the ready queue. Fix, rebase, and rerun Shuttles still start, because their tickets are already open, and each one takes a `maxAgents` slot.

L and XL are not special. `autoMergeSizes` is the cut. Sizes in `autoMergeSizes` (`autoMerge` true) and sizes not in `autoMergeSizes` (`autoMerge` false) share one gate: Bugbot passes, findings are fixed up to `maxFixAttempts`, and CI is green. A listed size then auto-merges and Jira moves to Done. A size not in `autoMergeSizes` then moves to `awaiting_approval`, Jira moves to QA Ready, and the Jira and pull-request comment says Bugbot is clean and how many finding rounds were fixed. A person approves on the provider or with `warp:proceed <id>` (plan id, Jira key, or a `#` number). The one channel listener acks in channel and queues the command. The parent applies it when that poll returns and merges that ticket. Jira then moves to Done, the merged comments go out, locks drop, and dependents whose deps are terminal become ready. The Slack reply includes the ack, then the merge sha and the Jira status. `jiraDoneOnManualMerge: false` leaves the issue at QA Ready. `bugbotManual: false` skips Bugbot on the manual path only. The provider comes from `gitProvider`. If it is not usable, or `pushMerge` is false, Reed merges the branch into `baseBranch` locally and does not push. Three failed Bugbot rounds raise an alarm. Warp will not retry it until `warp:retry`. A red gate blocks dependents until check evidence is recorded. New commits after QA Ready send the ticket back through Bugbot and leave the Jira status where it is. A merge that never recorded Done shows on `/warp-jira-check` as `merged-but-not-done`.

## Estimate

Scan and export write an estimate into `.warp/_ingested_schedule.json`, the beam, and `.warp/WARP_PLAN.md`. Agent hours are implementation time (S=4, M=7, L=11, XL=16, unless a ticket has its own hours). Human hours assume every approval for a size not in `autoMergeSizes`, and every gate check, is answered within 30 minutes. Sizes in `autoMergeSizes` add no human wait. Elapsed hours are the longer of the critical chain plus those waits, and agent hours divided by `maxAgents`.

## Kickoff

Warp starts each ready ticket in its own git worktree. The prompt begins:

```
SUBAGENT API-01
```

The id is the plan id. The worktree is a checkout of `warp/<id>-<jira>` from `origin/main`, so `.cursor/rules`, `AGENTS.md`, `CLAUDE.md`, and `.warp/` are on disk. The subagent works only inside that path. The prompt comes from `checkout.py launch` and nowhere else. It carries the subagent's agent id (`agent: <id>`), the worker contract, and the reap command. The subagent runs the reap check first and stops on `reap: exit`. Then it reads those rules, then the claim in the prompt. See `assets/KICKOFF.md`.

Optional `launch: agent` still starts with `IMPLEMENT API-01` for one new Agent. This plugin does not call a Cloud Agents API. Clone main so `.cursor` rules load. A beam file does not have to exist on the branch before that Agent starts. Rules apply because the files are in the worktree, not because they were copied into the prompt. A rule only applies if Cursor would apply it in that workspace: `alwaysApply`, a matching glob, or the Shuttle reading it. Warp requires the read.

## Agents

| Agent | Job |
|---|---|
| Warp | Scan, tick, dispatch, halt. The parent: the only agent that starts another agent, and the only one that waits. Never edits product code. |
| Shuttle | One ticket, one branch, one pull request. Does one step and returns one line. Never merges, and never starts another agent. |
| Listener | One poll of Slack and Teams. Queues `warp:` commands and returns `listen: <count>`. Never merges. |
| Reed | Bugbot, evidence, auto-merge or hold. |
| Herald | Slack and Teams. Status, alarms, approval asks. |

## License

MIT. See `LICENSE`.
