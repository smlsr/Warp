---
name: warp-init
description: "Install Warp in the current repo: copy the plugin, create .warp/config.yaml, gitignore .warp/, and write the project MCP allowlist. Use on a fresh repo, or to repair a partial install. Safe to run again."
---

# Warp init

Automates the manual install. Each step checks first. A second run on a complete install changes nothing. A second run on an older config adds any keys that config is missing, and says which ones.

## Run

```bash
python3 <plugin>/scripts/install.py init
```

`<plugin>` is wherever this plugin is loaded from. The script works from the repo root (the git top level, or the current directory outside git). Add `--dry-run` to preview, `--channel NAME` to override the channel, or `--no-allow` to skip the MCP allowlist. `install.py ?` (and `init ?`, `help`, `-h`, `--help`) prints the options. Quote `?` if the shell expands it.

## Steps

| Step | Done when | Otherwise |
|---|---|---|
| `mkdir -p .cursor/plugins .warp` | both folders exist | create them |
| Copy the plugin to `.cursor/plugins/warp` | every plugin file is present | copy only the missing files. Existing files are never overwritten. |
| `.warp/config.yaml` | the file exists and has every key from `assets/config.example.yaml` | on a fresh install, copy the example (every key, its comments, and the commented local-only lines). On a re-run, append any missing key with its default and comments. Existing values and comments are not changed or removed. The output names the keys that were added. |
| Channel | `slackChannel` and `teamsChannel` are set | set each empty one (or the old `Warp` default) to `warp` |
| `.gitignore` | the current snippet is already there | append `assets/gitignore-snippet.txt`, or replace an old `.warp/` ignore-all line. Other lines stay. |
| Git provider | `gitProvider` is already `github` or `bitbucket` | read `origin` and write it. A custom value is left alone, with a warning if it disagrees with the remote. No remote sets `pushMerge: false`. |
| Jira project | `jiraProject` is already set | detect it from plan files, branches, and recent commits. One clear prefix is written, and an empty `jiraKeyPrefixes` is set to the same key. Several prefixes, or none, are not guessed. The warning names the candidates. |
| MCP allowlist | `.cursor/permissions.json` already has Warp's tools, or `autoAllowTools` is false, or `--no-allow` was passed | write the project allowlist (Slack, Teams, Jira, and GitHub `add_issue_comment` when the provider is GitHub). Say what changed. Undo is `/warp-allow-notify --revoke`. Do not write `~/.cursor` files. |
| Version | `.warp/version` matches the project plugin copy | write that version. If `.cursor/plugins/warp` is older than the plugin this command ran from, print `plugin is vOLD, repo copy is vNEW: run /warp-upgrade` and do not overwrite plugin files. |

`warp` is one shared channel for every repo. A channel that already has a value is left alone. Other existing config values are left alone too; only keys that are absent are added. `--channel NAME` sets a different name on a fresh config; it is lowercased and must be lowercase letters, digits, `-` or `_`. If an existing `slackChannel` has uppercase letters, init prints a `[warn]` line and Herald posts to the lowercase name; pass that on to the user. Warp does not create the channel; tell the user to create `warp` in Slack and Teams if it does not exist.

The example includes commented lines such as `# pushMerge: false`, `# runner: local`, `# baseBranch: "develop"`, `# gitProvider: github`, and `# ghCli: false`. They are hints. Uncommenting one does not by itself turn the active line off; comment out the active line too. Init treats a line that starts with `#` as a comment, not as the key, so those hints never block adding the real key.

Re-run `/warp-init` to pick up new keys after a plugin upgrade, including `staleMinutes` (default 15), `maxRecoveries` (default 5), `alarmRepairMinutes` (default 15), `maxAlarmRepairs` (default 5), and `repairSweepMinutes` (default 15). Init still does not overwrite plugin files or replace a value you already set. A missing key in an old config still behaves as the default until you re-run: the scripts fill it in when they read.

## Jira project

If init prints `jira: PROJECT`, or `.warp/jira-project.json` is present and `jiraProject` is still empty, call `getAccessibleAtlassianResources` and `getVisibleJiraProjects` on `jiraMcp` before you tell the user init is finished. Save `{"resources":[{"url":"https://yoursite.atlassian.net","id":"..."}],"projects":[{"key":"WAR","name":"Warp"}]}` and run `jira_sync.py project --apply results.json`. One site is stored in `jiraSite`. One project, or one that matches the repo or a candidate, is stored. If several projects come back, read the warning to the user and do not pick one. The command is `python3 <plugin>/scripts/jira_sync.py project --set WAR`.

## Message to Slack or Teams

If init changed anything, the script also writes `.warp/notify-post.json`: a message headed `Warp | <repo> / <project>` that says "Cursor repo <name> was initialized with Warp", with the channel used and the repo link. The footer starts with `Warp v<version>`. Follow the `herald:` line it prints. `post` means Herald posts that text to the listed channels through the connected Slack or Teams MCP server. No message is built when init changed nothing or ran with `--dry-run`. Respect `notify` and `messenger` in the config. Post the `slack` view to Slack and the `teams` view to Teams. Never create the channel. If Slack or Teams is not connected, or no channel is set, the text goes to `.warp/outbox.md`. Report that to the user and carry on; it is not an error. Warp stores no webhook or token.

## After

Print the script output, including every `[warn]` about a missing GitHub or Bitbucket connector or `gh`. Warp does not install `gh`, create keys, or connect an app. Then tell the user to reload Cursor. Remind them to connect Jira, the git provider if they want pull requests, and Slack or Teams in Cursor Settings, and that `/warp-scan` is next. Do not scan or start.
