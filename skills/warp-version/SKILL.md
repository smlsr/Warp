---
name: warp-version
description: "Print the Warp plugin version of the project copy and of the source copy this command ran from. Use when the user asks which Warp version is installed, or when init says an upgrade needs a reinstall."
---

# Warp version

```bash
python3 <plugin>/scripts/version.py
```

`version.py ?` prints the options (`help`, `-h`, and `--help` do the same). Quote `?` if the shell expands it.

Read the output to the user.

- `Warp v<version>` means the project copy and the source copy match.
- `installed:` is `.cursor/plugins/warp`. `source:` is the plugin tree this command ran from. `recorded:` is `.warp/version`, written by `/warp-init`.
- `plugin is vOLD, repo copy is vNEW: run /warp-upgrade` means the project copy is older. Init does not overwrite plugin files. `/warp-upgrade` replaces the plugin copy and leaves `.warp/config.yaml` and the beam alone.
- The output always ends with the upgrade script, for a command list that does not show `/warp-upgrade`. Read these lines to the user. They do not need the slash command:

```bash
python3 .cursor/plugins/warp/scripts/upgrade.py
python3 .cursor/plugins/warp/scripts/upgrade.py ?
```

`?` prints the flags (`--root` and `--source`). Quote `?` if the shell expands it. A 1.4.6 tree includes `scripts/upgrade.py` at that path.

The number in `VERSION` is the source of truth. `.cursor-plugin/plugin.json` must match it.
