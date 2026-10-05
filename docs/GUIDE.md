# Guide

Commands, flags, and config keys are in [COMMANDS.md](COMMANDS.md) and [CONFIG.md](CONFIG.md). Install, upgrade, and the short troubleshooting tree are in the README. The decision tree for a stuck transition is in [RUNBOOK.md](RUNBOOK.md). What each version changed is [CHANGELOG.md](../CHANGELOG.md).

## Jira, from import to merge

Warp never calls Jira itself. A script prints the search or the transition. The agent calls the Atlassian server named `jiraMcp` (default `atlassian`) and then records the result. A plan id such as `WV-01` is not an issue key such as `WAR-1`.

1. **Import or scan.** `/warp-scan` reads `WARP_PLAN.json`, `schedule.json`, `CURSOR_PLAN.md`, a Jira ticket export, or a markdown table with `id` and `deps`. A Jira JSON import uses `externalId` as the plan id. Sections `h2. Size`, `h2. Locks`, `h2. Blocked by`, and `h2. Acceptance`, and labels `size:S`, `auto-merge`, and `area:*`, describe the ticket. `externalId` is not stored as `jiraKey`. An export field named `key` is the issue key. So are `schedule.json` `jiraKey`, a markdown `Jira: WAR-1`, a `Jira Key` column, and `[WAR-1]` on the heading.
2. **Project.** Leave `jiraProject` empty. `/warp-init` and `/warp-scan` write it when one prefix is clear from the plan, branches, or recent commits, ignoring plan-id prefixes such as `WV` that never appear as a Jira key. When Jira is connected, one visible project or one match is stored, and a single site is stored in `jiraSite`. Several candidates are not guessed: the warning is `jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC)`. Then `project --list`, `project --set WAR`, or `project --probe` and `project --record`. An empty `jiraKeyPrefixes` becomes that project. `jiraKeyMap` and `.warp/jira-map.json` supply pairs you already know. The map file wins.
3. **Resolve keys.** For each ticket that is still open, scan writes `.warp/jira-resolve.json`. The agent searches in this order and stops at the first single hit: `jiraExternalIdField` (a name or `customfield_NNNNN`), then External ID, External Id, ExternalId, External Key, Plan ID, and Ticket ID, then a label `warp:<id>`, then a remote-link id. One hit is stored with `resolve --ticket WV-01 --key WAR-1 --issue-id <id> --cloud-id <cloudId>` (or `resolve --apply`). Source and confidence are recorded on the beam and in `.warp/jira-map.json`. Two hits are ambiguous and neither is stored. A summary match is not stored on this path. The scan line is `N tickets: K keyed, U need mapping`.
4. **Summary, when that is all you have.** `/warp-jira-match` compares unmapped summaries locally after a bounded Jira search (`summary ~` is fuzzy, so the agent fetches at most 2 pages of 50). Exact, then a 60-character prefix (`--chars`), then a score at or above 0.9 (`--min-score`) as a proposal. `--apply` stores exact and prefix. `--apply --yes` also stores one fuzzy proposal. Ambiguous matches, Done issues, and issues already mapped to another ticket are not stored. A manual key is never overwritten.
5. **Claim.** The first claim of an unmapped ticket runs the same lookup. It must record the key before `transitionJiraIssue`. The move is In Progress (`jiraInProgressStatus`) when `jiraTransition` is true, plus a comment. `jira_sync.py pick` chooses the transition id from the live list. `record` stores the move. `record --result failed --error "..."` stores `jira.lastAttempt` and does not set `jira.startedAt`.
6. **Review and merge.** A pull request comment goes out in connected mode only. Bugbot runs for every ticket (`bugbotRequired`, and `bugbotManual` for the manual path). Red Bugbot goes back to the Shuttle up to `maxFixAttempts`, then an alarm. CI must be green. S and M then auto-merge and Jira moves to Done (`jiraDoneStatus`), including a local merge. L and XL (`autoMerge` false) move to QA Ready (`jiraQaReadyStatus`) only after that gate, with a comment that Bugbot is clean, and wait for a provider approval or `/warp-proceed`. After that merge, Jira moves to Done as well, unless `jiraDoneOnManualMerge` is false. Opening the pull request does not move Jira to QA Ready. Pause and stop do not move Jira. Releasing a claim moves it back only when `jiraRestoreOnRelease` is true. New commits after QA Ready re-run Bugbot and leave the Jira status alone. The merge step prints one post-merge MUST DO: the Done transition (name, then status, then one done-category transition), the merged comments, lock release, dependents that are now ready, and the Slack reply with the sha. A missing Done transition is `no-transition` in `.warp/outbox.md` and a Herald post.
7. **Write the plan id back.** Off by default (`jiraWriteExternalId: false`). Set it to true and re-run `/warp-scan` or `/warp-jira-check`. Both print `MUST DO write External ID` for every mapped ticket that is not confirmed, including keys that were already in the map. The config flag is the consent, so `--yes` is not required. A key found by the external-id search is already equal and is skipped. If the project has no External ID field, Warp remembers that in `.warp/jira-field.json` and, by default, adds the label `warp:<id>` (`editJiraIssue` `update.labels` add). It does not create the field unless `jiraCreateExternalIdField` is true or you run `/warp-jira-external-id --create-field --yes`. The Rovo MCP catalog has no create-field tool, so that probe usually falls back to the label. `remote-link` and `none` are the other fallbacks. `/warp-jira-external-id --recheck` looks the field up again. No comment is added. `jira.externalIdWritten.method` makes a second run skip the edit.

`/warp-jira-check` is the place to read this state. `/warp-jira-view` prints one issue and does not write the beam. `/warp-jira-map` is only for a ticket that stayed unmapped or ambiguous, or for an override.

## When nothing is linked

`/warp-jira-check` with no flags does not search Jira. `needs mapping` means the beam has no confirmed key. Read `why nothing linked`, then the `fix` line on the ticket.

- Empty `jiraProject`: set it before any move.
- Map file has keys the beam never copied: `verify --link`.
- No key in the plan, the export, or the map: run the resolve search, then `/warp-jira-match` if a summary candidate exists, or `map --set`.
- Ambiguous: do not guess. Set one pair.
- The stored value equals the plan id: that lookup was not recorded. `resolve --ticket` with the issue key.

The same steps, as a numbered tree, are in [RUNBOOK.md](RUNBOOK.md).

## What Warp replaces

The manual plan is 3 people, 6 Cursor windows each, merge windows at 08:30, 13:00, and 17:00. Warp keeps the caps and the gates, and drops the calendar as a blocker. Dispatch is continuous. Windows are digest times unless `respectMergeWindows` is true.

The speed plan's lever still holds: an L ticket that gets a review within the hour does not wait for the next window. Reed polls APPROVED instead of waiting for a human to paste a preamble.

## Graph

Source of truth is `HOS/plan/schedule.json` (deps, locks, gates, size, owner, critical path). `CURSOR_PLAN.md` is the wave narrative. `BUILD_MAP.html` is the human board. Ingest reads the JSON.

Critical path: L-01, M-01, M-02, M-04, M-07, C-01, C-02, C-07, C-10, C-30, C-31, C-32, C-51, C-49, C-60, C-62, C-66.

Sizes map to complexity:

| Size | Hours | Class | Merge |
|---|---|---|---|
| S | 4 | LOW | auto after Bugbot + CI |
| M | 7 | MEDIUM | auto after Bugbot + CI |
| L | 11 | HIGH | wait for APPROVED |
| XL | 16 | CRITICAL | wait for APPROVED |

Counts in the generated graph: S 73, M 109, L 111, XL 73.

## Tick

```
reconcile in-flight → advance gates → ready() → claim → Shuttle → Reed → Herald if needed → board
```

`ready()` refuses a ticket when:

- the beam is paused
- a dep is not merged
- a blocking upstream gate is not green
- a lock path overlaps an active ticket
- `maxAgents` is full. There is no per-person cap.

## Gates

G0–G11 from the schedule. A gate stays pending until every member is merged and someone records check evidence. Red blocks dependents. Members of the gate may still run; that is how G0 gets built.

Do not mark a gate green because the member PRs merged. The schedule's `checks` are the evidence.

## Model

Coding steps use `config.model`, default `claude-sonnet-5-5-high` (Claude Sonnet 5.5 High). The slug must match the Cursor model picker. Change the yaml, then the next Shuttle picks it up. In-flight tickets keep the model they started with; the branch records it in the PR body.

## 24/7 board

`.warp/BOARD.md` and `.warp/board.html` regenerate every tick. Leave a cloud agent on `/warp` overnight, or run a tick from a scheduled Cursor automation. The beam is local to the repo, so a second machine resumes by pulling `.warp/` (commit the beam and journal; they contain no secrets).
