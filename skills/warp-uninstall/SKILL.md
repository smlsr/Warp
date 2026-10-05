---
name: warp-uninstall
description: "Remove Warp from the current repo: .cursor/plugins/warp, the .warp folder, and optionally the .gitignore snippet, so a fresh /warp-init is possible. Use only when the user asks to uninstall or reset Warp. Never delete without explicit confirmation."
---

# Warp uninstall

Destructive. `.warp/` holds the beam, journal, and config, and none of it can be recovered.

## Run

1. List first. This deletes nothing.

```bash
python3 <plugin>/scripts/install.py uninstall --remove-gitignore
```

Drop `--remove-gitignore` if the user wants to keep the `.gitignore` entry.

2. Show the user the list under "Will remove" and any warnings. If the beam is running, suggest `/warp-stop` first.
3. Ask for an explicit yes. The command alone is not consent. Silence or a vague reply is a no.
4. Only after the yes, run the same line with `--yes` added.

```bash
python3 <plugin>/scripts/install.py uninstall --remove-gitignore --yes
```

## What it removes

| Target | Rule |
|---|---|
| `.cursor/plugins/warp` | The project copy. Empty `.cursor/plugins` and `.cursor` are removed too. |
| `.warp` | Beam, config, exports, journal. |
| `.gitignore` snippet | Only with `--remove-gitignore`, and only the exact block `/warp-init` added. Other lines are kept. A hand-written `.warp/` entry is left alone. |
| Project allow-notify entries | The hook command, `mcpAllowlist` entries, and CLI allow entries recorded by `/warp-allow-notify` in this repo (`.cursor/warp-allow.json`). Other hooks and other allow entries stay. |

It does not touch product code, `warp/<id>` branches, pull requests, a `stateDir` outside the repo, a plugin installed for the user through Cursor Settings, or user-level allow-notify files (`~/.cursor/permissions.json`, `~/.cursor/cli-config.json`, `~/.cursor/hooks.json`).

## After

Tell the user to reload Cursor, then `/warp-init` for a fresh install.
