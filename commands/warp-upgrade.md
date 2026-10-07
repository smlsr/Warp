---
name: warp-upgrade
description: Replace the installed Warp plugin. Leaves config and the beam alone.
---

Run the `warp-upgrade` skill. Do not scan or dispatch. Run it in the repo that has Warp installed. It replaces `.cursor/plugins/warp`, fetches the default branch when the source is a git checkout, and does not overwrite `.warp/config.yaml` or the beam. Print `Warp vX.Y.Z`. Tell the user to reload Cursor, then confirm with `/warp-version`. If the fetch fails, the installed copy stays (`fetch failed`, `keeping the installed copy`). `/warp-init` does not upgrade an existing copy. This command does. `--force` is not an upgrade flag. `upgrade.py ?` prints `--root` and `--source`.
