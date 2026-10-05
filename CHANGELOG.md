# Changelog

## 1.3.5

- jiraProject is detected instead of hand-edited. Init and scan write it when one prefix is clear from the plan, branches, or recent commits, and ignore plan-id prefixes such as WV that have no Jira key. Several prefixes are not guessed. When Jira is connected, one visible project or one match is stored, and a single site is stored in jiraSite. An empty jiraKeyPrefixes becomes that project. A value already set is left alone. The warning is: jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC).

## 1.3.4

- Jira auto-match checks the external-id field first (configured name or customfield id, then External ID, External Id, ExternalId, External Key, Plan ID, and Ticket ID), then a warp:<id> label and a remote link. One exact hit is stored with its source and confidence. A summary match waits for --yes. Two matches are reported and neither is stored. A manual key is never overwritten. map --from-jira and --auto apply a saved search; --dry-run writes nothing.

## 1.3.3

- A plan id such as `WV-01` is not sent to Jira. A key is kept when its prefix matches `jiraProject` or `jiraKeyPrefixes`, or it was set on the plan, in a Jira export, in the map file, or by hand.
- `/warp-scan` stores `schedule.json` `jiraKey`, a markdown `Jira:` line, a `Jira Key` column, `[WAR-1]` in the heading, `.warp/jira-map.json`, and `jiraKeyMap`. A rescan keeps a key set with `beam.py set --jira`.
- Scan and the first claim look up Jira's external id field. One exact match is stored. The summary line is `N tickets: K keyed, U need mapping`. `/warp-jira-map` is only for a ticket that stays unmapped or ambiguous, or for an override.
- A claim with no key searches before it flags. A miss or an ambiguous result goes to `.warp/outbox.md` and Herald and does not call Jira. `catchup` then asks for the transition the current status still owes.
- Config keys `jiraKeyPrefixes`, `jiraKeyMap`, and `jiraExternalIdField`. `/warp-init` adds them to an existing file.

## 1.3.2

- README covers `/warp-allow-notify` (project and `--user --yes`, dry run, revoke, Jira and GitHub tools, `notifyAllow`, backups, idempotency, and which Cursor surface each file changes). `docs/COMMANDS.md` is the command reference. `docs/CONFIG.md` lists every config key and its default.
- Quick start, upgrade (`/warp-uninstall` then `/warp-init`, or init alone to backfill keys), and troubleshooting (Run prompts, Jira not moving, no Slack message, channel name).
- Scripts people run directly accept `?`, `help`, `-h`, and `--help`. `?` is always help. A bare `help` after an option that takes a value stays that value.
- `beam.py` no longer defaults `jiraProject` to `HOS` when a config file is absent. The default is empty, matching `assets/config.example.yaml`.

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
