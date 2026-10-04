---
name: warp-init
description: "Install Warp in the current repo: copy the plugin to .cursor/plugins/warp, create .warp/config.yaml with the shared Warp channel, and gitignore .warp/. Use on a fresh repo, or to repair a partial install. Safe to run again."
---

# Warp init

Automates the manual install. Each step checks first and runs only if needed, so a second run changes nothing.

## Run

```bash
python3 <plugin>/scripts/install.py init
```

`<plugin>` is wherever this plugin is loaded from. The script works from the repo root (the git top level, or the current directory outside git). Add `--dry-run` to preview, or `--channel NAME` to override the channel.

## Steps

| Step | Done when | Otherwise |
|---|---|---|
| `mkdir -p .cursor/plugins .warp` | both folders exist | create them |
| Copy the plugin to `.cursor/plugins/warp` | every plugin file is present | copy only the missing files. Existing files are never overwritten. |
| `.warp/config.yaml` | the file exists | copy `assets/config.example.yaml` |
| Channel | `slackChannel` and `teamsChannel` are set | set each empty one (or the old `Warp` default) to `warp` |
| `.gitignore` | it already ignores `.warp/` | append `assets/gitignore-snippet.txt` once |

`warp` is one shared channel for every repo. A channel that already has a value is left alone, and so is the rest of an existing config. `--channel NAME` sets a different name on a fresh config; it is lowercased and must be lowercase letters, digits, `-` or `_`. If an existing `slackChannel` has uppercase letters, init prints a `[warn]` line and Herald posts to the lowercase name; pass that on to the user. Warp does not create the channel; tell the user to create `warp` in Slack and Teams if it does not exist.

To upgrade an installed copy, run `/warp-uninstall` and then `/warp-init`. Init does not replace files.

## Message to Slack or Teams

If init changed anything, the script also writes `.warp/notify-post.json`: a message headed `Warp | <repo> / <project>` that says "Cursor repo <name> was initialized with Warp", with the channel used and the repo link. Follow the `herald:` line it prints. `post` means Herald posts that text to the listed channels through the connected Slack or Teams MCP server. No message is built when init changed nothing or ran with `--dry-run`. Respect `notify` and `messenger` in the config. Post the `slack` view to Slack and the `teams` view to Teams. Never create the channel. If Slack or Teams is not connected, or no channel is set, the text goes to `.warp/outbox.md`. Report that to the user and carry on; it is not an error. Warp stores no webhook or token.

## After

Print the script output, then tell the user to reload Cursor. Remind them to connect Jira, Bitbucket, and Slack or Teams in Cursor Settings, and that `/warp-scan` is next. Do not scan or start.
