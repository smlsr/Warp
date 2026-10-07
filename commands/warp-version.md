---
name: warp-version
description: Print the installed Warp version and the source copy, and say when an upgrade needs a reinstall
---

Run the `warp-version` skill. Print the script output, including the upgrade script lines when `/warp-upgrade` is not in the command list. Do not uninstall or change files. `version.py ?` prints the options (`help`, `-h`, and `--help` do the same). `bump_version.py ?` does the same for the bump script. The upgrade script is `python3 .cursor/plugins/warp/scripts/upgrade.py` and does not need the slash command. `python3 .cursor/plugins/warp/scripts/upgrade.py ?` prints its flags.
