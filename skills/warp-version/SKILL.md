---
name: warp-version
description: "Print the Warp plugin version of the project copy and of the source copy this command ran from. Use when the user asks which Warp version is installed, or when init says an upgrade needs a reinstall."
---

# Warp version

```bash
python3 <plugin>/scripts/version.py
```

Read the output to the user.

- `Warp v<version>` means the project copy and the source copy match.
- `installed:` is `.cursor/plugins/warp`. `source:` is the plugin tree this command ran from. `recorded:` is `.warp/version`, written by `/warp-init`.
- `plugin is vOLD, repo copy is vNEW: run /warp-uninstall then /warp-init` means the project copy is older. Init does not overwrite plugin files, so a fresh install is the way to pick up the new copy. Do not uninstall unless the user asks.

The number in `VERSION` is the source of truth. `.cursor-plugin/plugin.json` must match it.
