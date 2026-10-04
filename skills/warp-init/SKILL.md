---
name: warp-init
description: "Install Warp in the current repo: copy the plugin to .cursor/plugins/warp, create .warp/config.yaml with a warp-<reponame> channel, and gitignore .warp/. Use on a fresh repo, or to repair a partial install. Safe to run again."
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
| Channel | `slackChannel` and `teamsChannel` are set | set each empty one to `warp-<reponame>` |
| `.gitignore` | it already ignores `.warp/` | append `assets/gitignore-snippet.txt` once |

`<reponame>` comes from the `origin` remote, else the folder name. It is lowercased, and anything outside `a-z 0-9 _ -` becomes `-`. The channel is cut to 50 characters, the Teams limit (Slack allows 80), so no dot or other invalid character can appear. A channel that already has a value is left alone, and so is the rest of an existing config.

To upgrade an installed copy, run `/warp-uninstall` and then `/warp-init`. Init does not replace files.

## After

Print the script output, then tell the user to reload Cursor. Remind them to connect Jira, Bitbucket, and Slack or Teams in Cursor Settings, and that `/warp-scan` is next. Do not scan or start.
