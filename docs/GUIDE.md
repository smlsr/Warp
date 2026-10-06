# Guide

Commands, flags, and config keys are in [COMMANDS.md](COMMANDS.md) and [CONFIG.md](CONFIG.md). Install, upgrade, and the short troubleshooting tree are in the README. The decision tree for a stuck transition is in [RUNBOOK.md](RUNBOOK.md). What each version changed is [CHANGELOG.md](../CHANGELOG.md).

## Jira, from import to merge

Warp never calls Jira itself. A script prints the search or the transition. The agent calls the Atlassian server named `jiraMcp` (default `atlassian`) and then records the result. A plan id such as `WV-01` is not an issue key such as `WAR-1`.

1. **Import or scan.** `/warp-scan` reads `WARP_PLAN.json`, `schedule.json`, `CURSOR_PLAN.md`, a Jira ticket export, or a markdown table with `id` and `deps`. A Jira JSON import uses `externalId` as the plan id. Sections `h2. Size`, `h2. Locks`, `h2. Blocked by`, and `h2. Acceptance`, and labels `size:S`, `auto-merge`, and `area:*`, describe the ticket. `externalId` is not stored as `jiraKey`. An export field named `key` is the issue key. So are `schedule.json` `jiraKey`, a markdown `Jira: WAR-1`, a `Jira Key` column, and `[WAR-1]` on the heading.
2. **Project.** Leave `jiraProject` empty. `/warp-init` and `/warp-scan` write it when one prefix is clear from the plan, branches, or recent commits, ignoring plan-id prefixes such as `WV` that never appear as a Jira key. When Jira is connected, one visible project or one match is stored, and a single site is stored in `jiraSite`. Matched issue keys are enough on their own: if every stored key is in one project, scan writes it even when `getVisibleJiraProjects` is missing. The line is `jira: set jiraProject to WAR (every stored key is in project WAR)`. Do not run `project --set` for that result. Keys in more than one project stay unset and the line names them. A different value already set is left alone. Several candidates that are not backed by one agreed key are not guessed: the warning is `jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC)`. Then `project --list`, `project --set WAR`, or `project --probe` and `project --record`. An empty `jiraKeyPrefixes` becomes that project. `jiraKeyMap` and `.warp/jira-map.json` supply pairs you already know. The map file wins.
3. **Resolve keys.** For each ticket that is still open, scan writes `.warp/jira-resolve.json`. The agent searches in this order and stops at the first single hit: `jiraExternalIdField` (a name or `customfield_NNNNN`), then External ID, External Id, ExternalId, External Key, Plan ID, and Ticket ID, then a label `warp:<id>`, then a remote-link id. One hit is stored with `resolve --ticket WV-01 --key WAR-1 --issue-id <id> --cloud-id <cloudId>` (or `resolve --apply`). Source and confidence are recorded on the beam and in `.warp/jira-map.json`. Two hits are ambiguous and neither is stored. A summary match is not stored on this path. The scan line is `N tickets: K keyed, U need mapping`.
4. **Summary, when that is all you have.** `/warp-jira-match` compares unmapped summaries locally after a bounded Jira search (`summary ~` is fuzzy, so the agent fetches at most 2 pages of 50). Exact, then a 60-character prefix (`--chars`), then a score at or above 0.9 (`--min-score`) as a proposal. `--apply` stores exact and prefix. `--apply --yes` also stores one fuzzy proposal. Ambiguous matches, Done issues, and issues already mapped to another ticket are not stored. A manual key is never overwritten.
5. **Claim.** The first claim of an unmapped ticket runs the same lookup. It must record the key before `transitionJiraIssue`. The move is In Progress (`jiraInProgressStatus`) when `jiraTransition` is true, plus a comment. `jira_sync.py pick` chooses the transition id from the live list. `record` stores the move. `record --result failed --error "..."` stores `jira.lastAttempt` and does not set `jira.startedAt`. `record --result not-found` is the result when Jira says that issue does not exist. The first claimed ticket of a run is the claim that happens while no ticket has `jira.startedAt`: no earlier ticket was linked and moved to In Progress. If that ticket is not found, or no real issue key can be resolved, Warp releases the claim back to `queued` (clears `agent`, `branch`, `jira.startedAt`, and `jira.previousStatus`), does not start implementation or open a pull request, and stops the run the same way as `/warp-stop`. The reason is `tickets are not linked to Jira (WV-01 / WAR-1)`. Herald posts one message: `Run stopped because Jira issues are not linked (WV-01 / WAR-1). Fix jiraProject, /warp-jira-match, or /warp-jira-external-id, then /warp-resume.` It does not also post that the claim still stands. A later miss, after at least one ticket has `jira.startedAt`, stays a per-ticket alarm and does not stop the run. `jiraTransition: false` does not stop the run.
6. **Review and merge.** A pull request comment goes out in connected mode only. Bugbot runs for every ticket (`bugbotRequired`, and `bugbotManual` for the manual path). Red Bugbot goes back to the Shuttle up to `maxFixAttempts`, then an alarm. CI must be green. A size in `autoMergeSizes` then auto-merges and Jira moves to Done (`jiraDoneStatus`), including a local merge. A size not in `autoMergeSizes` (`autoMerge` false) moves to QA Ready (`jiraQaReadyStatus`) only after that gate, with a comment that Bugbot is clean, and waits for a provider approval or `/warp-proceed`. Slack and Teams `warp:proceed` is read by one `warp-listen` listener for the beam, not one per ticket. Herald posts `Received warp:proceed <id>. Merging and moving Jira to Done.` in that channel before the merge. A bad id is acknowledged and nothing else is merged. After that merge, Jira moves to Done as well, unless `jiraDoneOnManualMerge` is false. Opening the pull request does not move Jira to QA Ready. Pause and stop do not move Jira. Releasing a claim moves it back only when `jiraRestoreOnRelease` is true. New commits after QA Ready re-run Bugbot and leave the Jira status alone. The merge step prints one post-merge MUST DO: the Done transition (name, then status, then one done-category transition), the merged comments, lock release, dependents that are now ready, and the Slack reply with the sha. A missing Done transition is `no-transition` in `.warp/outbox.md` and a Herald post.
7. **Write the plan id back.** Off by default (`jiraWriteExternalId: false`). Set it to true and re-run `/warp-scan` or `/warp-jira-check`. Both print `MUST DO write External ID` for every mapped ticket that is not confirmed, including keys that were already in the map. The config flag is the consent, so `--yes` is not required. A key found by the external-id search is already equal and is skipped. If the project has no External ID field, Warp remembers that in `.warp/jira-field.json` and, by default, adds the label `warp:<id>` (`editJiraIssue` `update.labels` add). It does not create the field unless `jiraCreateExternalIdField` is true or you run `/warp-jira-external-id --create-field --yes`. The Rovo MCP catalog has no create-field tool, so that probe usually falls back to the label. `remote-link` and `none` are the other fallbacks. `/warp-jira-external-id --recheck` looks the field up again. No comment is added. `jira.externalIdWritten.method` makes a second run skip the edit.
8. **Completion report.** When nothing is left queued or active, or the run is stopped, Warp writes `.warp/warp-complete.html` (`reportOnComplete`, default true) and Herald posts the totals and the local path. The file is gitignored. `/warp-report --partial` writes a snapshot earlier. Waves group tickets by who was active together, from claim until the status leaves the working set. A gap is not a wave. Token and cost cells are n/a until Shuttle or Reed records `beam.py usage`. Nothing is invented.

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

The speed plan's lever still holds: an L ticket that is not in `autoMergeSizes`, and that gets a review within the hour, does not wait for the next window. Reed polls APPROVED instead of waiting for a human to paste a preamble. L and XL are not special. A size in `autoMergeSizes` auto-merges, including L and XL when they are listed.

## Graph

Source of truth is `HOS/plan/schedule.json` (deps, locks, gates, size, owner, critical path). `CURSOR_PLAN.md` is the wave narrative. `BUILD_MAP.html` is the human board. Ingest reads the JSON.

Critical path: L-01, M-01, M-02, M-04, M-07, C-01, C-02, C-07, C-10, C-30, C-31, C-32, C-51, C-49, C-60, C-62, C-66.

Sizes map to complexity:

| Size | Hours | Class | Merge |
|---|---|---|---|
| S | 4 | LOW | auto after Bugbot + CI when listed in `autoMergeSizes` (the default) |
| M | 7 | MEDIUM | auto after Bugbot + CI when listed (the default) |
| L | 11 | HIGH | auto when listed in `autoMergeSizes`; otherwise wait for APPROVED |
| XL | 16 | CRITICAL | same as L |

Counts in the generated graph: S 73, M 109, L 111, XL 73.

## Tick

```
watchdog → reconcile in-flight → advance gates → ready() → claim → Shuttle → Reed → Herald if needed → board
```

`beam.py watchdog` runs first. A Shuttle or the one listener is dead when `lastSeenAt` is older than `staleMinutes` (default 15), or it never heartbeated and the claim or listener start is older than that. The Shuttle writes that heartbeat with `beam.py heartbeat` at claim, at each status change, and at least every 5 minutes. The listener writes it with `inbound.py heartbeat` on every read. A fresh heartbeat is left alone. Pause and stop print `watchdog: skipped` and do not start a replacement.

A dead listener is cleared and exactly one new `warp-listen` agent is launched (`listener: replace`). Herald posts `Listener died. A new one started.` A dead Shuttle stays on the same branch, pull request, `jira.startedAt`, and locks. Status becomes `recovering`. Exactly one new Shuttle is dispatched (`shuttle: replace`). Herald posts `<id> worker died. A new Shuttle started.` It is not queued, so another ticket cannot take the files. A second tick does not start a second replacement. Past `maxRecoveries` (default 3) the ticket is `alarm` / `worker-died` and no Shuttle starts. Plugin hooks do not run on cloud runners. A dead turn does not notify Warp. Cursor does not restart it.

The same listener repairs `lock-escape` while the run is running. Every `alarmRepairMinutes` (default 15) it calls `alarm_repair.py next` and starts one repair. The repair widens that ticket's locks to the paths that escaped (`escaped` on the alarm). It does not take a path an in-flight ticket holds. It waits, then widens and starts. A queued ticket's overlapping lock can be widened. A failure moves to the next lock-escape ticket in that call. Success waits until the ticket is merged, or back on the normal path with the alarm cleared, then starts the next. `maxAlarmRepairs` (default 3) leaves the alarm and later passes skip it. Pause and stop do not repair. Other alarm reasons are left alone.

The orchestrator is the only merger. Reed reviews and does not merge. A Shuttle never merges and never pushes to the base branch (`baseBranch`, or the detected default).

`ready()` refuses a ticket when:

- the beam is paused
- a dependency or an `after` id is not merged on the base branch. An open pull request does not count, even when its checks are green
- a blocking upstream gate is not green
- a lock path overlaps a lock held by an in-flight ticket, including when one path is a parent folder of the other. The check uses the ticket's full lock list
- `maxAgents` is full. A green pull request has already freed its slot. Locks stay until merge or park. There is no per-person cap. A project may set 18.

Dispatch runs at start and again after every merge, failure, and freed slot. Starred tickets first, then priority, then the lowest `rank`. It does not wait for a whole dependency level, and it does not hold a slot for a ticket that is not ready. One ticket, one branch, one checkout. Cloud is one Cursor cloud agent the Warp session launches (the plugin has no API for that, and it refuses to implement the ticket in-process). Local is one git worktree (`git worktree add` / `git worktree remove`). The branch starts at the head of the base branch and keeps Warp's `warp/<id>` name. The slot stays occupied until the provider check rollup is green. Opening the pull request records that the Shuttle finished. Bugbot runs on that pull request after the push. Locks stay until merge or park. `mergeQueue: true`, or a GitHub merge queue the provider can see, enqueues the pull request instead of merging it from the agent.

The merge queue is serial: rebase, run `checkCommand` when it is set (otherwise Bugbot and CI), merge, delete the branch, then dispatch. Sizes not in `autoMergeSizes` wait for `/warp-proceed` or `warp:proceed`. `appendOnlyPaths` is the only shared-file conflict the orchestrator resolves, by keeping both sides. Any other conflict is sent back. The third red check parks the ticket. Parked tickets and what they block are in `warp-complete.html`. A red base branch stops merging and a fix is dispatched ahead of every rank. On start or resume, rebuild from git: merged on the base branch, in flight when a branch or pull request is open, otherwise pending. Done means every ticket is merged or explicitly parked, and the base branch is green.

## Gates

G0–G11 from the schedule. A gate stays pending until every member is merged and someone records check evidence. Red blocks dependents. Members of the gate may still run; that is how G0 gets built.

Do not mark a gate green because the member PRs merged. The schedule's `checks` are the evidence.

## Model

Coding steps use `config.model`, default `claude-sonnet-5-5-high` (Claude Sonnet 5.5 High). The slug must match the Cursor model picker. Change the yaml, then the next Shuttle picks it up. In-flight tickets keep the model they started with; the branch records it in the PR body.

## 24/7 board

`.warp/BOARD.md` and `.warp/board.html` regenerate every tick. Leave a cloud agent on `/warp` overnight, or run a tick from a scheduled Cursor automation. That tick is not a Slack reader, except when `beam.py watchdog` prints `listener: replace` because the one listener's heartbeat is older than `staleMinutes`. Then the tick launches exactly one replacement and does not launch a second on the next tick. `/warp-start` and `/warp-resume` launch one `warp-listen` listener, or that replacement when the watchdog already reserved the id. `/warp-pause` and `/warp-stop` stop it. It must not keep reading while paused or stopped. Cursor cannot wake it from a Slack message, and there is no webhook. Plugin hooks do not run on cloud runners. Each tick runs `scripts/resume_hint.py` before dispatch, `scripts/beam.py watchdog` before `ready()`, and `scripts/session_note.py --type session-stop` when the turn ends. A Shuttle runs `scripts/beam.py heartbeat` while it works and `scripts/session_note.py --type subagent-stop` when it finishes. MCP calls go through `scripts/mcp_allow.py`, which allows only Warp's list. The beam is local to the repo, so a second machine resumes by pulling `.warp/` (commit the beam and journal; they contain no secrets).
