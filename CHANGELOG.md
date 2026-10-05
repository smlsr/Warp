# Changelog

## 1.3.1

- `/warp-allow-notify` writes a specific MCP allowlist so Slack and Teams posts, and optionally the Jira and GitHub tools Warp calls, can run without a Cursor Run prompt.
- `/warp-version` prints the installed plugin version and, when it differs, the source copy. `/warp-init` and `/warp-status` print it too. Herald init and scan messages end with `Warp v1.3.1`.
- Init records the installed version in `.warp/version`. When the project copy is older than the source copy, it says to run `/warp-uninstall` then `/warp-init`.
- Every pull request must bump the patch version and add a changelog entry. `scripts/bump_version.py` does both. CI rejects a pull request whose version is not newer than main.

## 1.3.0

The manifest stayed at 1.3.0 from `/warp-init` through the Jira comment work. Those changes shipped without a later bump:

- `/warp-init`, `/warp-uninstall`, and `/warp-scan` with an optional folder.
- One Herald formatter, header `Warp | <repo> / <project>`, and a shared lowercase `warp` channel. Warp does not invite anyone to the channel.
- Jira moves to In Progress on claim, to QA Ready or Done on the merge path, and comments on the issue and the pull request. `/warp-jira-check` shows what is missing. Re-running init adds config keys that an old file lacks.
- Git provider `auto`, GitHub, or Bitbucket, and local-only merges when there is nothing to push.
- Default coding model `claude-sonnet-5-5-high`.

## 1.2.0

- Initial plugin: scan a plan, dispatch workers, post status to Slack and Teams, and keep the beam out of git.
