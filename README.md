# Warp

Warp is a Cursor plugin that scans a repo for a plan, builds a schedule, and dispatches workers under dependency, lock, and gate rules. It is not tied to one product. A third party can point it at their own specs.

Shuttle workers implement one ticket each. Reed reviews and merges, or holds a large ticket for approval. Herald posts to Slack and Teams. The beam survives a stop.

There is no per-person cap. The only concurrency cap is `maxAgents`.

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
| Copy `assets/config.example.yaml` to `.warp/config.yaml` | the config exists. It is never overwritten. |
| Set `slackChannel` and `teamsChannel` to `warp-<reponame>` | the channel already has a value. Only an empty (default) channel is filled. |
| Append `assets/gitignore-snippet.txt` to `.gitignore` | `.gitignore` already ignores `.warp/`, so the snippet is never added twice |

`<reponame>` comes from the `origin` remote, or the folder name if there is none. It is lowercased, and anything but letters, digits, `_` and `-` becomes `-`. The channel is cut to 50 characters, the Teams limit (Slack allows 80), so it is valid in both. Then reload Cursor.

To do it by hand instead:

```bash
mkdir -p .cursor/plugins .warp
cp -R warp .cursor/plugins/warp
cp .cursor/plugins/warp/assets/config.example.yaml .warp/config.yaml
```

Append `assets/gitignore-snippet.txt` to the repo `.gitignore`. Reload Cursor. Connect Jira, Bitbucket, and Slack or Teams in Cursor Settings.

Commands include `/warp-init`, `/warp-scan`, `/warp-start`, `/warp-pause`, `/warp-resume`, `/warp-stop`, `/warp-status`, `/warp-status-post`, and `/warp-uninstall`.

## Slack and Teams messages from init and scan

Warp has no webhook and no token. Messages go out through the Slack and Teams servers connected in Cursor Settings, posted by the Herald agent in your chat. A script cannot send them on its own, so nothing arrives unless those servers are connected, a channel is set, and the agent session that ran the command is the one that posts.

- `/warp-init` posts "Cursor repo <name> was initialized with Warp" with the channel used, but only if it changed something. A second run posts nothing.
- `/warp-scan` posts the format, ticket and gate counts, the estimate, and links to the plan and schedule files found. Links use the `origin` remote and branch (GitHub, GitLab, bitbucket.org). Otherwise the relative path is used. Links work once the branch is pushed.
- It follows `messenger` and `notify`. `notify: quiet` posts neither. Each message goes to `slackChannel` and `teamsChannel` for the messenger you chose.
- Fail-soft: with no channel set, or no connected server, the text is saved to `.warp/outbox.md`, the command says so, and it still succeeds. The payload is in `.warp/notify-post.json`.
- `/warp-init` fills in `warp-<reponame>`. That channel must exist, and the Warp app or bot must be in it.

## Uninstall

`/warp-uninstall` removes `.cursor/plugins/warp` and `.warp/`, and optionally the snippet `/warp-init` added to `.gitignore`, so a fresh `/warp-init` works. It first prints what it will remove and deletes nothing until you confirm. `.warp/` holds the beam, journal, and config, and it cannot be recovered. Stop a running beam first with `/warp-stop`. Product code, `warp/<id>` branches, pull requests, a `stateDir` outside the repo, and a plugin installed through Cursor Settings are not touched. Reload Cursor afterwards.

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
| `model` | `claude-sonnet-5.5` | Coding slug. Must match the picker. |
| `maxAgents` | `18` | Concurrent Shuttles. The only cap. |
| `autoMergeSizes` | `S, M` | Auto-merge after Bugbot and CI. L and XL wait. |
| `messenger` | `both` | `slack`, `teams`, or `both`. |
| `notify` | `verbose` | Every claim and tick. `quiet` still answers `warp:status`. |
| `runner` | `cloud` | Cloud VM, or `local` for this machine. |
| `jiraProject` | empty | Pin a Jira key, or leave blank. |
| `bugbotRequired` | `true` | No merge without a Bugbot pass. |
| `maxFixAttempts` | `3` | Then the ticket alarms. |
| `stuckAfterMinutes` | `90` | No update in this window raises stuck. |
| `respectMergeWindows` | `false` | True makes new claims wait for a window. |
| `mergeWindows` | `08:30, 13:00, 17:00` | Digest times, or claim gates if the flag is true. |
| `pollSeconds` | `300` | How often pull requests and channel commands are read. |
| `jiraMcp` | `atlassian` | Connected Jira server name. |
| `bitbucketMcp` | `bitbucket` | Connected Bitbucket server name. |
| `slackMcp` / `teamsMcp` | `slack` / `teams` | Connected messenger names. |
| `slackChannel` / `teamsChannel` | empty, or `warp-<reponame>` after `/warp-init` | Channel to post and to watch for `warp:status`. |

## Merge policy

S and M auto-merge after Bugbot and CI are green and every acceptance criterion is on the pull request and the Jira issue. L and XL wait for a Bitbucket APPROVED, or for `warp:proceed <id>`. Three failed reviews raise an alarm. Warp will not retry it until `warp:retry`. A red gate blocks dependents until check evidence is recorded.

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
