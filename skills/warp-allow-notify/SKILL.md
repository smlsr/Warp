---
name: warp-allow-notify
description: "Write the Cursor allowlist so Slack and Jira calls Warp makes do not stop for Allow or Run. Use when a prompt names slack_send_message or addOrEditJiraIssueComment, or after init if autoAllowTools was off."
---

# Allow notify

`/warp-init` writes this list for the project when `autoAllowTools` is true. Run this command to refresh it, to add `--with-git`, or to edit user-level files after an explicit yes.

Tool names come from `scripts/mcp_tools.py`. Jira tools are included. Extra pairs come from `notifyAllow` in `.warp/config.yaml`, each one `server:tool`, with no wildcard. `server:*` is written only with `--allow-server-tools`, and that allows every tool on the server, including destructive ones.

## Which Cursor surface this changes

| Surface | What the script writes | What it does |
|---|---|---|
| IDE, this repo | `.cursor/permissions.json` `mcpAllowlist` | Skips the Run prompt for those tools when Run Mode is Auto-review, Allowlist, or Run Everything. |
| IDE, all repos | `~/.cursor/permissions.json` `mcpAllowlist` | Same, only with `--user` after an explicit yes. |
| CLI, this repo | `.cursor/cli.json` `permissions.allow` `Mcp(server:tool)` | Cursor CLI and headless runs. Does not change the IDE Run button. `agent -f` approves tools for that process. |
| CLI, all repos | `~/.cursor/cli-config.json` | Same, only with `--user`. |
| Hook | `.cursor/hooks.json` or `~/.cursor/hooks.json` `beforeMCPExecution` | Returns allow for this list and ask for every other tool. A hook allow does not currently skip the Run prompt. The permissions file does. |
| Cloud agents and automations | nothing | They do not use Run Modes and do not ask for approval. `beforeMCPExecution` does not run there. |

Setting `mcpAllowlist` replaces the in-app MCP allowlist. Entries that exist only in Cursor Settings prompt again until they are in the file. The terminal allowlist and `autoRun` are left alone. Ask Every Time does not consult the file. A team admin Run Mode override ignores it.

The default list is Slack (post and the `warp:status` reads), Teams, and Jira, on the configured name plus `user-<name>`, `project-<name>`, `plugin-<name>-<name>`, and `*<name>*:<tool>`. `--with-jira` changes nothing. `--with-git` adds GitHub `add_issue_comment` only. Warp does not name a Bitbucket comment tool; that belongs in `notifyAllow`. `--list` and `--check` write nothing. `--check` is the diagnostic when a prompt is still there.

## Run

`python3 <plugin>/scripts/allow_notify.py ?` prints every option (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it. Show that output when the user asks what the command can do.

1. Show a dry run first. This writes nothing.

```bash
python3 <plugin>/scripts/allow_notify.py --dry-run
```

Add `--with-jira` and/or `--with-git` when the user asked for those tools too. Read the diff to the user, including which files change.

2. Project scope is the default. Running this command is the user's ask to edit the project files. After the dry run, apply it:

```bash
python3 <plugin>/scripts/allow_notify.py
```

3. User scope (`--user`) edits files in the home directory. Stop after the dry run. Ask, in your own words, whether to edit `~/.cursor/permissions.json`, `~/.cursor/cli-config.json`, and `~/.cursor/hooks.json`. Wait for an explicit yes. Silence or a vague reply is a no. Only then:

```bash
python3 <plugin>/scripts/allow_notify.py --user --dry-run
python3 <plugin>/scripts/allow_notify.py --user --yes
```

The script refuses `--user` without `--yes`.

4. Tell the user what changed, and tell them to reload Cursor (Developer: Reload Window) or start a new agent chat. Run Mode has to be Auto-review, Allowlist, or Run Everything. If the prompt names a tool that is not in the list, add `server:tool` to `notifyAllow` and run the command again.

`--revoke` removes only the entries the manifest recorded. It does not remove entries the user already had. `/warp-uninstall` does that revoke for the project files only.
