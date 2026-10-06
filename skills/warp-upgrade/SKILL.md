---
name: warp-upgrade
description: "Replace .cursor/plugins/warp with the current plugin source. Leaves .warp/config.yaml and the beam alone. Use when init says the project copy is older, or the user asks to upgrade Warp."
---

# Warp upgrade

Replace the project plugin copy. Do not touch `.warp/config.yaml` or the beam. Do not scan or dispatch.

```bash
python3 <plugin>/scripts/upgrade.py ?
python3 <plugin>/scripts/upgrade.py --root .
```

`?`, `help`, `-h`, and `--help` print the same reference. Quote `?` if the shell expands it.

- The source is the plugin tree this command is running from. `--source` names a different tree.
- When that tree is a git checkout, the command fetches the default branch and copies that tree.
- If the fetch fails, it prints `fetch failed` and `keeping the installed copy`. Do not copy over the install yourself.
- It prints `Warp vX.Y.Z`.
- Tell the user to reload Cursor (Developer: Reload Window).
- A second run is safe. `/warp-init` still does not replace an existing plugin copy. This command does.
