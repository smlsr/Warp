---
name: warp-upgrade
description: "Replace .cursor/plugins/warp with the current plugin source. Leaves .warp/config.yaml and the beam alone. Use when init says the project copy is older, or the user asks to upgrade Warp."
---

# Warp upgrade

Replace the project plugin copy. Do not touch `.warp/config.yaml` or the beam. Do not scan or dispatch. `--force` is not an upgrade flag.

Run it in the repo that has Warp installed. Tell the user these steps, in this order:

1. Run `/warp-upgrade` in the repo that has Warp installed.
2. It replaces `.cursor/plugins/warp` with the plugin this command is running from.
3. It fetches the default branch when the source is a git checkout, and copies that tree.
4. It does not overwrite `.warp/config.yaml` or the beam.
5. It prints `Warp vX.Y.Z`.
6. Reload Cursor after (Developer: Reload Window).
7. Confirm the version with `/warp-version`. That prints the installed copy, the source copy, and `.warp/version`.
8. If the fetch fails, it prints `fetch failed` and `keeping the installed copy`. The installed copy stays. Do not copy over the install yourself.
9. `/warp-init` does not upgrade an existing copy. This command does.

```bash
python3 <plugin>/scripts/upgrade.py ?
python3 <plugin>/scripts/upgrade.py --root .
python3 <plugin>/scripts/upgrade.py --root . --source /path/to/warp
```

`?`, `help`, `-h`, and `--help` print the same reference. Quote `?` if the shell expands it. The flags are `--root` and `--source`. A second run is safe.
