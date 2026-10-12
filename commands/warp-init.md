---
name: warp-init
description: Install Warp in this repo, including the MCP allowlist. Safe to run again.
---

Run the `warp-init` skill on the current repo. If the user gave a name (`/warp-init admin`), pass it: it names this checkout's run, or takes up a run of that name that is already in the repo. Print every `run:`, `config:`, `folder:`, `scan:`, `state:`, `progress:`, `warn:`, and `next:` line to the user. Do not scan or dispatch. Post the init message (headed `Warp | <repo> / <project>`, footer starting `Warp v<version>`) through Herald when the script says to; if Slack or Teams is not available, say so and continue. Finish by reminding the user to reload Cursor. `install.py ?` prints the options.
