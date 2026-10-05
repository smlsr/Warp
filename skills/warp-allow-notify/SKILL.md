---
name: warp-allow-notify
description: "Write the Cursor allowlist so Herald can post to Slack and Teams without a Run approval prompt. Optional flags add the Jira and GitHub tools Warp calls. Use when the user wants those MCP writes to stop prompting."
---

# Allow notify

Cursor asks for a Run approval every time Herald posts, until those tools are on the MCP allowlist. This command writes that list. It never writes a wildcard.

Tool names come from `scripts/mcp_tools.py` (the same names the Jira and Herald docs use). Extra pairs come from `notifyAllow` in `.warp/config.yaml`, each one `server:tool`.

## Which Cursor surface this changes

| Surface | What the script writes | What it does |
|---|---|---|
| IDE, this repo | `.cursor/permissions.json` `mcpAllowlist` | Skips the Run prompt for those tools when Run Mode is Auto-review, Allowlist, or Run Everything. |
| IDE, all repos | `~/.cursor/permissions.json` `mcpAllowlist` | Same, only with `--user` after an explicit yes. |
| CLI, this repo | `.cursor/cli.json` `permissions.allow` `Mcp(server:tool)` | Cursor CLI only. Does not change the IDE Run button. |
| CLI, all repos | `~/.cursor/cli-config.json` | Same, only with `--user`. |
| Hook | `.cursor/hooks.json` or `~/.cursor/hooks.json` `beforeMCPExecution` | Returns allow for this list and ask for every other tool. A hook allow does not currently skip the Run prompt. The permissions file does. |
| Cloud agents | nothing | Cloud agents do not use Run Modes and do not ask for approval. `beforeMCPExecution` does not run there. |

Setting `mcpAllowlist` replaces the in-app MCP allowlist. Entries that exist only in Cursor Settings prompt again until they are in the file. The terminal allowlist and `autoRun` are left alone. Ask Every Time does not consult the file. A team admin Run Mode override ignores it.

Default tools are the Slack post tools on `slackMcp` and the Teams post tools on `teamsMcp`. `--with-jira` adds the Atlassian tools Warp calls. `--with-git` adds GitHub `add_issue_comment` only. Warp does not name a Bitbucket comment tool; that belongs in `notifyAllow`.

## Run

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
