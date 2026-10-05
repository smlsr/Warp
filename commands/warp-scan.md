---
name: warp-scan
description: Scan this repo, or one folder, and build the Warp plan
---

Run the `warp-scan` skill on the current repo. If the user gave a folder path or name, pass it as the folder and scan only there. If the name matches several folders, list them and ask which one. Post the scan summary (headed `Warp | <repo> / <project>`, footer starting `Warp v<version>`) through Herald when the script says to; if Slack or Teams is not available, say so and continue. Do not dispatch. `scan.py ?` prints the options.
