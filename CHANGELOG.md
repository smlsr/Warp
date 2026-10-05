# Changelog

## 1.3.16

- The version check passes when the working tree already matches main, so a pull request that was just merged is not marked failed. Script annotations use Optional instead of X | None so the scripts parse on Python 3.9.

## 1.3.15

- Skipped. No separate change; rolled into 1.3.16.

## 1.3.14

- 1.3.14 version bump, no functional changes.

## 1.3.13

- /warp-init writes the project MCP allowlist for Slack, Teams, and Jira. Entries include user-, plugin-, and *name* variants because the Run dialog often does not use the mcp.json key. server:* stays behind --allow-server-tools. --check and --list diagnose a prompt. autoAllowTools defaults to true. --no-allow skips one init. User-level files still need --user --yes.

## 1.3.12

- /warp-jira-external-id remembers a missing External ID field and, by default, adds label warp:<id> with editJiraIssue update.labels add. Field creation stays off unless jiraCreateExternalIdField or --create-field --yes. The Rovo MCP catalog has no create-field tool; a missing tool or a denial is not a failure. getJiraScreen and updateJiraScreen only add a field that already exists. --recheck looks again. jira.externalIdWritten.method is field, label, or remote-link. No comment is added.

## 1.3.11

- /warp-jira-external-id and scan/catchup write existing plan-id mappings into Jira External ID when jiraWriteExternalId is true. The config flag is the consent, so scan does not need --yes. Keys found by the external-id search are already equal and are skipped. record-external-id records each edit. A missing or read-only field is skipped. A different non-empty value needs --force-external-id. No comment is added.

## 1.3.10

- Docs now cover 1.3.3 through 1.3.9: plan id versus Jira key, external-id lookup, jiraProject detection, /warp-jira-check, /warp-jira-view, and /warp-jira-match. The README has a command table, upgrade steps, and a troubleshooting tree. GUIDE.md walks Jira from import to merge.

## 1.3.9

- /warp-jira-match links unmapped tickets to Jira by summary. Exact and a 60-character prefix are stored with --apply. A fuzzy score at or above 0.9 is a proposal until --yes. Ambiguous matches are not stored. --write-external-id writes the plan id into the External ID field with editJiraIssue after --yes, skips a missing or read-only field, and does not add a comment. jiraWriteExternalId defaults to false, so scan and claim do not write that field.

## 1.3.8

- /warp-jira-view prints every field on a Jira issue. An issue key such as WAR-1 is fetched with getJiraIssue. An external id such as WV-01 is resolved from the beam and the map, then from the external-id field, and from each visible project when jiraProject is empty. Two matches are listed and neither issue is printed. --comments, --links, --all, --full, --verbose, and --json control the report.

## 1.3.7

- A claim that finds a Jira issue by external id now has to record it before any transition: resolve --ticket WV-01 --key WAR-1 --issue-id ID --cloud-id CLOUD writes the beam and the map together, and the transition todo uses only that key. A plan id is refused. Jira import JSON uses externalId as the plan id (h2. Size, Locks, Blocked by, Acceptance, and size/auto-merge/area labels) and never as jiraKey. When several Jira projects are visible, project --probe and project --record pick the one that contains the plan id. verify prints key stored or key missing, jira.id, and jira.lastAttempt from record --result failed --error.

## 1.3.6

- /warp-jira-check prints why each ticket is unmapped and the one command that fixes it. verify with no flags only reads the beam and does not call Jira. verify --link copies a key already in the map file and writes the same JQL as map --from-jira. verify --apply stores one exact match. A needs mapping line on this report does not mean the lookup already ran.

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
