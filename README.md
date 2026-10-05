# Warp

Warp is a Cursor plugin that scans a repo for a plan, builds a schedule, and dispatches workers under dependency, lock, and gate rules. It is not tied to one product. A third party can point it at their own specs.

Shuttle workers implement one ticket each. Reed reviews and merges, or holds a large ticket for approval. Herald posts to Slack and Teams. The beam survives a stop.

There is no per-person cap. The only concurrency cap is `maxAgents`.

## Quick start

1. `/add-plugin https://github.com/smlsr/Warp`, then `/warp-init` in the repo you want built. Reload Cursor.
2. Connect Jira, GitHub or Bitbucket if you want pull requests, and Slack or Teams. Create a lowercase `warp` channel, or set another name with `/warp-init --channel NAME`.
3. `/warp-scan` (or `/warp-scan <folder>`). Keys in the plan, a Jira export, or `.warp/jira-map.json` are stored. If Jira is connected, a plan id that equals one issue's external id (then a `warp:<id>` label, then a remote link) is stored too. The summary looks like `12 tickets: 9 keyed, 3 need mapping`. `/warp-jira-map` is only for ids that are still unmapped or ambiguous. Then `/warp-start`.
4. `/warp-status` to read the board. `/warp-allow-notify` if the Run prompt blocks Slack or Teams posts. `/warp-version` to see which copy is installed.

The command reference is [docs/COMMANDS.md](docs/COMMANDS.md). Every config key is in [docs/CONFIG.md](docs/CONFIG.md).

## Install

Type this in a Cursor agent chat. It will not show up in autocomplete, so enter the whole line.

```text
/add-plugin https://github.com/smlsr/Warp
```

That works because the plugin manifest is at the root of that repo (.cursor-plugin/plugin.json), on main. You need Cursor 2.5 or later. Install it for yourself, or for the project, when Cursor asks.
Then, in the repo you want Warp to build, type `/warp-init`. It does the manual steps below and is safe to run again: it checks each one and does only what is missing.

| Step | Skipped when |
|---|---|
| `mkdir -p .cursor/plugins .warp` | both folders exist |
| Copy the plugin to `.cursor/plugins/warp` | every plugin file is already there. Existing files are never overwritten. |
| Copy `assets/config.example.yaml` to `.warp/config.yaml` | the file exists and already has every key. A re-run appends missing keys (with their defaults and comments) and names them. Values you set are not changed. |
| Set `slackChannel` and `teamsChannel` to `warp` | the channel already has a value. Only an empty channel (or the old `Warp` default) is filled; a custom name is kept. |
| Append `assets/gitignore-snippet.txt` to `.gitignore` | `.gitignore` already ignores `.warp/`, so the snippet is never added twice |

`warp` is one shared channel for every repo, so each message names its repo in a header (see below). Warp does not create the channel: create `warp` once in Slack and Teams, or change the config to a channel that exists. Channel names are lowercase (letters, digits, `-`, `_`). `--channel NAME` is lowercased and checked; a custom `slackChannel` with uppercase letters is warned about by `/warp-init` and lowercased when posting.

To do it by hand instead:

```bash
mkdir -p .cursor/plugins .warp
cp -R warp .cursor/plugins/warp
cp .cursor/plugins/warp/assets/config.example.yaml .warp/config.yaml
```

Append `assets/gitignore-snippet.txt` to the repo `.gitignore`. Reload Cursor. Connect Jira, GitHub or Bitbucket if you want pull requests, and Slack or Teams in Cursor Settings.

Commands include `/warp-init`, `/warp-scan`, `/warp-start`, `/warp-pause`, `/warp-resume`, `/warp-stop`, `/warp-status`, `/warp-status-post`, `/warp-jira-check`, `/warp-jira-map`, `/warp-allow-notify`, `/warp-version`, and `/warp-uninstall`. Flags and behavior are in [docs/COMMANDS.md](docs/COMMANDS.md). The scripts those commands run accept `?`, `help`, `-h`, and `--help`.

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
Links point at branch main; they work once it is pushed. Warp v<version>. Next: /warp-start. Status files are in .warp/ (not committed).
```

- `/warp-init` posts only if it changed something. A second run posts nothing.
- `/warp-scan` posts the summary and links to the plan and schedule files found. Links use the `origin` remote and branch (GitHub, GitLab, bitbucket.org). Otherwise the relative path is used. Links work once the branch is pushed.
- `/warp-status-post` uses the same header.
- It follows `messenger` and `notify`. `notify: quiet` skips the init and scan messages. Each message goes to `slackChannel` and `teamsChannel` for the messenger you chose.
- Fail-soft: with no channel set, or no connected server, the text is saved to `.warp/outbox.md`, the command says so, and it still succeeds. The payload is in `.warp/notify-post.json`.

## Allow notify

Cursor asks you to approve each Slack or Teams tool call until that call is on the MCP allowlist. `/warp-allow-notify` writes a specific `server:tool` list. It never writes a wildcard. Options:

```bash
python3 <plugin>/scripts/allow_notify.py ?
python3 <plugin>/scripts/allow_notify.py --dry-run
python3 <plugin>/scripts/allow_notify.py
python3 <plugin>/scripts/allow_notify.py --with-jira --with-git
python3 <plugin>/scripts/allow_notify.py --user --dry-run
python3 <plugin>/scripts/allow_notify.py --user --yes
python3 <plugin>/scripts/allow_notify.py --revoke
```

`?`, `help`, `-h`, and `--help` print the same reference. Quote `?` if the shell expands it.

| Flag | Behavior |
|---|---|
| (none) | Project scope. Slack and Teams post tools only. This is the default. |
| `--dry-run` | Print a unified diff of every file that would change. Write nothing, and do not write a backup. |
| `--user` | Edit the home-directory files instead of the repo. The script refuses to write unless `--yes` is also set. A dry run does not need `--yes`. The skill must get an explicit yes before `--user --yes`. |
| `--yes` | Confirm a user-level write. Project scope does not need it. |
| `--with-jira` | Also allow the Atlassian tools Warp calls. |
| `--with-git` | Also allow GitHub `add_issue_comment`. Warp does not name a Bitbucket comment tool. |
| `--revoke` | Remove only the entries this command recorded in the manifest. Pre-existing entries stay. |
| `--root` | Repo root. Default is the git top level, else the current directory. |
| `--cursor-home` | Directory to use instead of `~/.cursor`. For tests. |

Default tool names come from `scripts/mcp_tools.py`, on the servers `slackMcp` and `teamsMcp`:

| Server key | Tools |
|---|---|
| `slackMcp` (default `slack`) | `slack_post_message`, `slack_send_message` |
| `teamsMcp` (default `teams`) | `send_channel_message`, `teams_send_message` |
| `jiraMcp` with `--with-jira` | `getAccessibleAtlassianResources`, `getJiraIssue`, `getTransitionsForJiraIssue`, `listJiraIssueTransitions`, `transitionJiraIssue`, `addOrEditJiraIssueComment`, `addCommentToJiraIssue`, `searchJiraIssuesUsingJql` |
| `githubMcp` with `--with-git` | `add_issue_comment` |

`notifyAllow` in `.warp/config.yaml` adds extra `server:tool` pairs. No wildcards. If the Run prompt names a different tool, copy that server and tool into `notifyAllow` and run the command again. The prompt is where the connector's real tool name shows up.

What each Cursor surface actually does:

| Surface | File | Effect |
|---|---|---|
| IDE Run prompt | `.cursor/permissions.json` (`--user`: `~/.cursor/permissions.json`) `mcpAllowlist` | Skips the prompt for those pairs when Run Mode is Auto-review, Allowlist, or Run Everything. Ask Every Time does not consult the list. Setting the key replaces the in-app MCP allowlist, so a tool allowed only in Cursor Settings prompts again until it is in the file. Per-user and per-repo files are combined. A team admin Run Mode override ignores the file. |
| CLI | `.cursor/cli.json` (`--user`: `~/.cursor/cli-config.json`) `permissions.allow` as `Mcp(server:tool)` | Cursor CLI only. It does not change the IDE Run button. |
| Hook | `.cursor/hooks.json` (`--user`: `~/.cursor/hooks.json`) `beforeMCPExecution` | Returns allow for this list and ask for every other call. `failClosed` is off. A hook allow does not currently skip the Run prompt. The permissions file does. |
| Cloud agents | none | Cloud agents do not use Run Modes and do not ask for approval. `beforeMCPExecution` does not run there. |

The command merges into JSON that is already there. Other hooks, other allow entries, the terminal allowlist, and `autoRun` are left in place. Before it changes an existing file it writes a sibling `.bak`. A second run with the same flags prints `Already set. Nothing changed.` and does not touch the backup. `--revoke` deletes an `mcpAllowlist` key that would otherwise be empty, so Cursor can fall back to the in-app list. `/warp-uninstall` removes the project hook and the project allow entries the manifest recorded. It does not remove `~/.cursor` files. Reload Cursor (Developer: Reload Window) or start a new agent chat afterwards.

## Version

The version lives in `VERSION`. `.cursor-plugin/plugin.json` carries the same number. `/warp-version` prints the installed copy (`.cursor/plugins/warp`) and the source copy this plugin was loaded from. `/warp-init` and `/warp-status` print it too. Init writes the installed version to `.warp/version`. When the project copy is older, init says `plugin is vOLD, repo copy is vNEW: run /warp-uninstall then /warp-init`.

```bash
python3 <plugin>/scripts/version.py
```

Every change bumps the patch version and adds a `CHANGELOG.md` entry. `python3 scripts/bump_version.py` does both (`--minor` or `--major` when those are intended). A pull request that does not move the version past `main` fails CI.

## Upgrade

`/warp-init` does not overwrite plugin files or config values you already set. It does append config keys that are missing, with the defaults and comments from `assets/config.example.yaml`.

When `/warp-version` or init says the project copy is older than the source copy:

1. `/warp-stop` if a beam is running.
2. Copy `.warp/` aside if you need the journal. Uninstall deletes it.
3. `/warp-uninstall`, read the list, then run it again with `--yes`.
4. Reload Cursor. `/warp-init`.

A config that is only missing new keys does not need an uninstall. Re-run `/warp-init` and it names the keys it added. Readers already use the same defaults when a key is absent.

## Troubleshooting

**Run prompt on every Slack or Teams post.** Run `/warp-allow-notify` (start with `?` or `--dry-run`). Set Run Mode to Auto-review, Allowlist, or Run Everything. Ask Every Time never consults the allowlist. If the prompt's tool name is not in the default list, add `server:tool` to `notifyAllow` and run the command again. Reload Cursor. A hook allow does not skip the prompt. Cloud agents do not show this prompt.

**Jira transitions failing: ticket needs a real key.** Plan ids such as `WV-01` are not Jira issue keys. `/warp-init` and `/warp-scan` set `jiraProject` when the plan, a branch, a recent commit, or Jira itself shows one project. You do not have to edit it by hand in that case. If the line says `jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC)`, pick one:

```bash
python3 <plugin>/scripts/jira_sync.py project --set WAR
```

An empty `jiraKeyPrefixes` is set to the same project. A value already in the config is left alone. `/warp-scan` stores a key that is already in the plan (`Jira: WAR-1`, a `Jira Key` column, `[WAR-1]` in the heading, or `schedule.json` `jiraKey`), in a Jira export, or in `.warp/jira-map.json`. If Jira is connected it looks up the plan id on `jiraExternalIdField` (a field name or `customfield_NNNNN`; also External ID, External Id, ExternalId, External Key, Plan ID, and Ticket ID), then a label `warp:WV-01`, then a remote-link id, and stores `WAR-1` when exactly one issue matches. A summary match is only a proposal. You do not run `/warp-jira-map` for a single exact hit. The scan line is `12 tickets: 9 keyed, 3 need mapping`. A claim tries the same lookup before it will flag the ticket. Two matches are reported and neither is stored. A key you set by hand is never replaced.

`/warp-jira-map` is only for a ticket that is still unmapped or ambiguous, or when you want to override:

```bash
python3 <plugin>/scripts/jira_sync.py map --set WV-01=WAR-1
python3 <plugin>/scripts/jira_sync.py catchup --beam .warp/beam.json
```

`map` with no arguments lists `unmapped` tickets. A CSV, JSON object, or markdown table can be imported with `--import`. After a leftover is mapped, `catchup` prints the transition and comments the current status still owes. The agent calls `transitionJiraIssue` with issue key `WAR-1`, not `WV-01`, then `record`.

**Jira did not move.** `/warp-jira-check`. If the line says `needs mapping`, the external-id, label, and remote-link lookup already ran and was not one exact match, so this ticket is a leftover: use `/warp-jira-map` for that id only. `map --from-jira --dry-run` shows what Jira would match without writing it. Check `jiraTransition` is not false, and that `jiraInProgressStatus`, `jiraQaReadyStatus`, and `jiraDoneStatus` match the workflow names. A failed move is in `.warp/outbox.md`. Re-run `/warp-init` if the config is missing keys.

**No Slack or Teams message.** Herald posts only from the agent session that ran the command. `notify: quiet` skips init and scan. An empty `slackChannel` or `teamsChannel` writes `.warp/outbox.md` instead. Warp does not create the channel. Slack names are lowercased. The payload is `.warp/notify-post.json`.

**Channel name rejected.** Use lowercase letters, digits, `-`, and `_`. No spaces or dots. `--channel` is lowercased for you.

## Uninstall

`/warp-uninstall` removes `.cursor/plugins/warp` and `.warp/`, and optionally the snippet `/warp-init` added to `.gitignore`, so a fresh `/warp-init` works. It also removes project allow-notify entries recorded by `/warp-allow-notify`. It first prints what it will remove and deletes nothing until you confirm. `.warp/` holds the beam, journal, and config, and it cannot be recovered. Stop a running beam first with `/warp-stop`. Product code, `warp/<id>` branches, pull requests, a `stateDir` outside the repo, a plugin installed through Cursor Settings, and user-level allow-notify files are not touched. Reload Cursor afterwards.

## Where the files are

A local agent uses your clone. A cloud agent uses a Cursor virtual machine with its own clone. Warp writes `.warp/` in that working copy. It does not commit that directory, and it does not push it. Product code is the only commit: a Shuttle opens `warp/<id>` and a pull request. Status writes stay local. Hundreds of beam updates are not source history.

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

Upload by dragging the file into the agent chat, or saving it in the workspace. Then `/warp-import` on that path. Import rewrites the graph, copies status onto ids that still exist, drops removed ids, and sets the run to stopped. Config is not replaced. Read `.warp/STATUS.md`, then `/warp-start`.

| File | Edit | Role |
|---|---|---|
| `.warp/WARP_PLAN.json` | Yes | The file import reads. |
| `.warp/WARP_PLAN.md` | Read | Analysis brief. Not imported. |
| `.warp/config.yaml` | Yes | Model, cap, messenger. Survives import. |
| `.warp/beam.json` | No | Live status. Scripts write it. |
| `.warp/STATUS.md` | No | Done, working, left. Download to review. |
| `.warp/journal.jsonl` | No | Append-only log. Leave it out of git. |

## Status

`/warp-status` rewrites `.warp/STATUS.md` and `.warp/status.json`. Working means claimed, coding, review, fix, or waiting on approval. Done means merged or moved to Done in Jira. Left is everything else. `.warp/BOARD.md` and `board.html` add gates, alarms, and ETA.

## Status in Teams or Slack

Teams and Slack cannot pull files from a stopped agent. They can ask, and Warp pushes, when the agent is running or a tick fires.

- In the agent window: `/warp-status-post`. Herald posts the digest and attaches `STATUS.md`, `status.json`, and `BOARD.md`.
- In the channel: send `warp:status`. The next tick replies in thread with the same files.
- The same channels accept `warp:pause`, `warp:resume`, `warp:stop`, `warp:start`, `warp:proceed <id>`, and `warp:retry <id>`.

Set `teamsChannel` and `slackChannel` in `.warp/config.yaml`. If a connector is missing, the text is appended to `.warp/outbox.md`. Warp does not store tokens.

## Configuration

One file, `.warp/config.yaml`. Change it, then restart, so the next tick re-reads it.

| Key | Default | Meaning |
|---|---|---|
| `stateDir` | `.warp` | Beam and exports. Gitignore it. |
| `model` | `claude-sonnet-5-5-high` | Coding slug: Claude Sonnet 5.5 High. Must match the Cursor model picker. |
| `maxAgents` | `18` | Concurrent Shuttles. The only cap. |
| `autoMergeSizes` | `S, M` | Auto-merge after Bugbot and CI. L and XL wait. |
| `messenger` | `both` | `slack`, `teams`, or `both`. |
| `notify` | `verbose` | Every claim and tick, plus init and scan. `quiet` posts alarms, approval waits, a red gate, and pause/stop. `warp:status` is always answered. |
| `runner` | `cloud` | Cloud VM, or `local` for this machine. |
| `jiraProject` | empty | Jira project prefix, such as `WAR`. Plan ids are not issue keys unless the prefix matches. |
| `jiraKeyPrefixes` | empty | More prefixes that confirm a Jira key. |
| `jiraKeyMap` | empty | Plan id to issue key, for example `{"WV-01": "WAR-1"}`. |
| `jiraExternalIdField` | `externalId` | Field name or `customfield_NNNNN` matched to the plan id. One exact hit is stored on scan and claim. |
| `jiraTransition` | `true` | On claim, move the Jira issue to In Progress (tickets with a Jira key only). |
| `jiraInProgressStatus` | `In Progress` | Target status name, matched by transition name, status name, then status category. |
| `jiraQaReadyStatus` | `QA Ready` | Manual path (L and XL): Jira moves here when a person must review and merge. |
| `jiraDoneStatus` | `Done` | Auto-merge path (S and M): Jira moves here after the merge. |
| `jiraRestoreOnRelease` | `false` | Move the issue back when a claim is released to `queued`. Otherwise it is left alone. |
| `bugbotRequired` | `true` | No merge without a Bugbot pass. |
| `maxFixAttempts` | `3` | Then the ticket alarms. |
| `stuckAfterMinutes` | `90` | No update in this window raises stuck. |
| `respectMergeWindows` | `false` | True makes new claims wait for a window. |
| `mergeWindows` | `08:30, 13:00, 17:00` | Digest times, or claim gates if the flag is true. |
| `pollSeconds` | `300` | How often pull requests and channel commands are read. |
| `jiraMcp` | `atlassian` | Connected Jira server name, not a tool name. |
| `jiraSite` | empty | Optional site URL used as `cloudId` when you have more than one Atlassian site. |
| `gitProvider` | `auto` | `github`, `bitbucket`, or `auto` (from the `origin` host). |
| `githubMcp` / `bitbucketMcp` | `github` / `bitbucket` | Connected git server names. |
| `ghCli` | `true` | GitHub may use an already-authenticated `gh`. Warp does not install it. |
| `pushMerge` | `true` | `false` is local-only: no push, no pull request, local merge. |
| `baseBranch` | empty | Integration branch. Empty detects it. |
| `slackMcp` / `teamsMcp` | `slack` / `teams` | Connected messenger names. |
| `slackChannel` / `teamsChannel` | `warp` | Shared channel to post to and to watch for `warp:status`. Must already exist. |
| `projectName` | empty | Shown after the repo in message headers. Empty uses `jiraProject`, then the folder name. |
| `notifyAllow` | empty | Extra `server:tool` pairs for `/warp-allow-notify`. No wildcards. |

## Merge policy

S and M (`autoMerge` true) auto-merge after Bugbot and CI are green and every acceptance criterion is on the pull request and the Jira issue, then Jira moves to Done. L and XL wait for an approval on the provider (a GitHub review or a Bitbucket APPROVED) or for `warp:proceed <id>`, and Jira moves to QA Ready when they are waiting. The provider comes from `gitProvider`. If it is not usable, or `pushMerge` is false, Reed merges the branch into `baseBranch` locally and does not push. Three failed reviews raise an alarm. Warp will not retry it until `warp:retry`. A red gate blocks dependents until check evidence is recorded.

## Estimate

Scan and export write an estimate into `.warp/_ingested_schedule.json`, the beam, and `.warp/WARP_PLAN.md`. Agent hours are implementation time (S=4, M=7, L=11, XL=16, unless a ticket has its own hours). Human hours assume every L/XL approval and every gate check is answered within 30 minutes. S/M add no human wait. Elapsed hours are the longer of the critical chain plus those waits, and agent hours divided by `maxAgents`.

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
