# Runbook

## Pause overnight but keep the board

`/warp-pause` with a reason. In-flight Shuttles finish the current step and checkpoint. The HTML board stays valid. `/warp-resume` reconciles, then ticks.

## A PR will not go green

After `maxFixAttempts` (default 3) Reed sets `alarm` / `bugbot-failed` and Herald posts. Warp will not retry it. Reply `warp:retry <id>` after adding a note on the Jira issue. There is no `warp:hold` command. `/warp-pause` stops new claims.

## L/XL waiting on you

Herald posts the pull request, or the local branch when `pushMerge` is false, and `warp:proceed <id>`. An approval on the provider (GitHub review or Bitbucket APPROVED) is enough. The chat command is the override, and it is the only signal in local-only mode. Jira is at QA Ready while this waits.

Do not proceed a red PR. Reed will refuse.

## Gate red

Fix on the member branch (G0 is L-01, M-01, D-01). Dependents stay out of `ready()` until `beam.py gate --status green --evidence "..."`. A red critical-path gate is the one case to pause the program: later agents will only pile up lock-free work that cannot ship.

## Jira status did not move

`/warp-jira-check` runs `jira_sync.py verify`, then `catchup`. `verify` with no flags only prints. It does not call Jira. For each ticket: `status: keyed` or `status: unmapped`, `source`, beam status, `startedAt`, `qaReadyAt`, `doneAt`, comment ids, `jira.id`, and `lastAttempt` when a move failed. An unmapped ticket also has `reason` and one `fix` command. When every ticket is unmapped the report ends with `why nothing linked`.

Walk the first line that matches. Stop when the ticket has a stored key and `catchup` prints the move that is still owed.

1. `jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC)`. `project --list` prints candidates and writes nothing. One project you recognize: `project --set WAR`. That also fills an empty `jiraKeyPrefixes`. Jira is connected and more than one project is visible: `project --probe` writes the search, then `project --record` stores the single project that contains the plan id. Several hits are listed and nothing is guessed. One Atlassian site from `project --apply` is stored in `jiraSite`.
2. `why nothing linked: jiraProject is empty`. Same as step 1. No transition runs until a project is set.
3. `why nothing linked: the map file has keys the beam never stored`. `verify --link` copies `.warp/jira-map.json` or `jiraKeyMap` onto the beam and writes `.warp/jira-resolve.json`. Then `catchup`.
4. `status: unmapped` and `fix` is `verify --link`. Run it. If tickets are still unmapped and Jira is connected, run that file's queries: external-id field, then label `warp:<id>`, then a remote link. `verify --apply results.json` stores one exact match. Two matches are named and neither is stored. A summary match is not stored on this path. A manual key is not overwritten.
5. The check prints `summary candidate: WAR-1 (exact 1.00)`. `/warp-jira-match --apply --id WV-01` stores an exact or 60-character prefix hit. A fuzzy proposal stays unstored until `--apply --yes`. Ambiguous rows are listed and not stored. Done issues and issues already mapped to another ticket are skipped. The full flags are `--chars`, `--min-score`, `--include-done`, and `--all`.
6. Still unmapped, and you know the key. `map --set WV-01=WAR-1`, or `beam.py set --jira`. `--force` is only for a prefix that is not configured. Then `catchup`.
7. `key missing: claim lookup result was not recorded`. The search may already have found `WAR-1`. Record it before any transition: `resolve --ticket WV-01 --key WAR-1 --issue-id <id> --cloud-id <cloudId>`. That writes the beam and `.warp/jira-map.json` together. A plan id is refused. `transitionJiraIssue` uses only the stored key.
8. `key stored` and `lastAttempt: claim failed — ...`. That line is `record --result failed --error "..."`. `jira.startedAt` was not set. Fix the error (workflow name, missing transition, connector), then `record` the success. The same text is in `.warp/outbox.md`.
9. `key stored` and catch-up still lists a move. The agent calls `getTransitionsForJiraIssue`, `jira_sync.py pick`, then `transitionJiraIssue`, then `addOrEditJiraIssueComment`, then `record` and `record-comment`. An already-merged auto-merge ticket is asked for Done, not a move back to In Progress.
10. `key stored` and nothing is missing, but Jira still did not move. `jiraTransition` is not false. `jiraMcp` is the server name Cursor shows. `jiraInProgressStatus`, `jiraQaReadyStatus`, and `jiraDoneStatus` match the workflow. Re-run `/warp-init` if the config file is missing keys. The script cannot see whether the server is connected.

`/warp-jira-view WAR-1` (or a plan id) prints the live fields when you need to see why a transition is absent. It does not write the beam.

**External ID not written.** `jiraWriteExternalId: true` makes `/warp-scan` and `/warp-jira-check` print `MUST DO write External ID` for mappings that already exist. The flag is the consent. `--yes` is not required. The agent calls `editJiraIssue` for each line, then `jira_sync.py record-external-id`. To do it without a scan: `/warp-jira-external-id`, then `/warp-jira-external-id --apply --yes`. A key whose source is `external` is skipped. A different non-empty value needs `--force-external-id`. No comment is added. `jira.externalId` on the beam is not proof the Jira field was written. `jira.externalIdWritten` is, and its `method` says `field`, `label`, or `remote-link`.

**External ID field missing.** One line, not one skip per ticket. `.warp/jira-field.json` stores the miss. The default fallback adds label `warp:<id>`. Create the field in Jira admin (Short text, name External ID, add it to the project screens), then `/warp-jira-external-id --recheck`. Or `/warp-jira-external-id --create-field --yes` to probe for a create tool. The Rovo catalog does not have one. `jira mapping:` on `/warp-jira-check` and `stored in Jira:` on `/warp-jira-view` say which method landed.

## Nothing linked

`needs mapping` on `/warp-jira-check` does not mean the external-id search already ran. Read `why nothing linked` before running another search.

| Line | What to do |
|---|---|
| `jiraProject is empty` | `project --list`, then `--set`, or `--probe` and `--record` |
| `the map file has keys the beam never stored` | `verify --link` |
| `no Jira key in the plan, export, or map file` | Connect Jira and let scan or `verify --link` search, or `/warp-jira-match` when the summaries correspond, or `map --set` |
| `ambiguous matches` | Do not pick one. `map --set WV-01=WAR-1` |
| `stored WV-01 is the plan id` | The lookup was never recorded. `resolve --ticket` with the issue key |

A Jira JSON import stores `externalId` as the plan id. `h2. Size`, `h2. Locks`, `h2. Blocked by`, `h2. Acceptance`, and labels `size:S`, `auto-merge`, and `area:*` describe the ticket. None of those fields are the issue key. The issue `key` in an export is the only export field that becomes `jiraKey`.

## Stuck agent

Reconcile marks `stuck` when `updatedAt` is older than `stuckAfterMinutes` (default 90) and the ticket is not `awaiting_approval`. Reattach by spawning a Shuttle on the same id; it must reuse the branch.

## Cap change

Edit `.warp/config.yaml` `maxAgents`. Next tick picks it up if the skill re-reads yaml. Also set the copy inside the beam if a tick reads only the beam: re-ingest is wrong for a live run; edit `beam.config.maxAgents` with a journal note, or set it in both places.

## Reinstall or reset

`/warp-init` is idempotent. It never overwrites `.warp/config.yaml` values or existing plugin files, so it will not upgrade an installed copy. It does append config keys the file is missing, and it records the installed version in `.warp/version`. When that copy is older than the plugin you ran, it prints `plugin is vOLD, repo copy is vNEW: run /warp-uninstall then /warp-init`. To start clean, copy `.warp/` aside if you want the journal, run `/warp-stop`, then `/warp-uninstall` and confirm, reload Cursor, and run `/warp-init`. See the README upgrade section.

## Scan one folder

`/warp-scan <folder>` limits the search to that folder. If the name matches several folders the scan stops and lists them; re-run with the full path. The beam and `scan.json` record the folder.

## Rebuild the graph

Only when `schedule.json` is regenerated. Copy `.warp/` aside first. Ingest overwrites live status.
