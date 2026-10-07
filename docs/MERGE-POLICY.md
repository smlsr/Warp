# Merge policy

The orchestrator is the only merger. Shuttles and Reed do not merge. The cap is `maxAgents`. A project may set 18. Ready means every blocker is merged on the base branch and no lock overlaps an in-flight ticket, including a parent folder. A green pull request frees the slot and keeps the locks until merge or park. The slot stays occupied until the provider check rollup is green. One checkout per ticket: a cloud agent, or a local git worktree.

A size in `autoMergeSizes` enters the merge queue when the provider check rollup is green. Sizes outside `autoMergeSizes` still wait for `/warp-proceed` or `warp:proceed`. L and XL are not special. The merge queue is serial: rebase, run `checkCommand`, merge, delete the branch, dispatch. `checkCommand` empty means Bugbot and CI. `make ci` is kept as the full check command. A red result is stored on `pr.check` and the ticket is sent back with the log (`send-back <id> fix` and a `start` line). Past `maxFixAttempts` (default 5) the ticket parks. The name alone is not a red check. `appendOnlyPaths` conflicts keep both sides. Any other conflict is sent back. `mergeQueue: true`, or a GitHub merge queue the provider can read, enqueues the pull request instead of merging it from the agent. A direct merge that branch protection rejects is not reported as merged. Bugbot runs on the pull request after push. A red base branch stops merging. On start or resume, restart classifies merged, in flight, and pending from git.

`state_commit.py commit` commits the beam, journal, STATUS, and BOARD and strips tokens, cost, API keys, and webhook URLs. `.warp/config.yaml` stays gitignored. On a cloud runner, `/warp-start` refuses when `transitionJiraIssue`, `addOrEditJiraIssueComment`, or `slack_send_message` is missing from the MCP allow list unless `--force`. A local runner does not block.

A pending or stale-red gate clears when every member is merged or done and no check is actually red, then the tick runs. G1 with members P-002, P-003, P-004, and P-018 stores `members merged: P-002, P-003, P-004, P-018`. Herald posts `G1 pending cleared. Members merged. Tick ran.` A green gate stays green.

The parent tick repairs `lock-escape` only, one ticket at a time. The listener does not start the repair Agent, every `alarmRepairMinutes` (default 15). The repair widens that ticket's locks to the escaped paths. It waits when an in-flight ticket holds a path. Past `maxAlarmRepairs` (default 5) the alarm stays. `bugbot-failed`, `worker-died`, `stuck`, `ci-red`, and `gate-red` are left alone by `alarm_repair.py`. `orchestrator.py supervise` restarts a `worker-died` step, starts a fix Shuttle for `ci-red` and `bugbot-failed`, and restarts `stuck` up to `maxRecoveries`. `gate-red` stays on the gate. Pause and stop do not repair. That repair is not a merge.

`/warp-upgrade` replaces `.cursor/plugins/warp`. Run it in the repo that has Warp installed. When the source is a git checkout, it fetches the default branch. If the fetch fails, it prints `fetch failed` and `keeping the installed copy`. It does not overwrite `.warp/config.yaml` or the beam. It prints `Warp vX.Y.Z`. Reload Cursor, then confirm with `/warp-version`. `/warp-init` does not upgrade an existing copy. This command does. `--force` is not an upgrade flag. The flags are `--root` and `--source`. Init and upgrade write every `commands/*.md` file to `.cursor/commands/`, including `.cursor/commands/warp-upgrade.md`, so a reload lists `/warp-upgrade`, `/warp-update-state`, `/warp-status`, and the other commands when the plugin command index still omits that path.

| Size | Class | Auto | Required before the last step |
|---|---|---|---|
| S | LOW | when listed | Bugbot pass, CI green, every AC evidenced on PR and Jira, then merge |
| M | MEDIUM | when listed | same |
| L | HIGH | when listed | same Bugbot and CI gate when listed; otherwise `awaiting_approval` and QA Ready, then a provider approval (GitHub review or Bitbucket APPROVED) or `warp:proceed` |
| XL | CRITICAL | when listed | same as L |

`autoMergeSizes` is the cut. `beam.auto_merge` checks that list. Default `[S, M]`, so L and XL wait unless you add them.

## Evidence comment

Reed posts this shape on the PR and the Jira issue:

```
Bugbot: pass
AC-1: go test ./internal/loom/token -run TestMint
AC-2: curl probe → 401 without header
CI: green <pipeline url>
Decision: auto-merge (size M)
```

## What is not a merge signal

- A human emoji on Slack.
- A green Bugbot with an AC missing.
- A gate member merging. The gate stays pending until every member is merged or done.
- An approval on a red pipeline.

## Jira

Which status depends on the ticket's `autoMerge` flag. `beam.auto_merge` sets it from the normalized size and `autoMergeSizes` (`.warp/config.yaml`, then the beam copy, then the default S, M). Scan and each `beam.py set` refresh it. Labels such as `L`, `size:L`, and `L — …` all match `L`. A schedule `autoMerge` flag does not override the list.

- Auto-merge (sizes in `autoMergeSizes`; S and M by default): after the merge sha is known, connected or local, move the issue to `jiraDoneStatus` (default Done). Comment the sha, the PR link, and whether the merge was local or connected. If the transition fails, leave the beam at `merged` and retry next tick. Do not move Jira to QA Ready and do not wait for `warp:proceed`.
- Manual (sizes not in `autoMergeSizes`): Bugbot and CI run first, including the fix loop. Only then does `awaiting_approval` move the issue to `jiraQaReadyStatus` (default QA Ready) and comment "Bugbot clean, ready for manual review" plus the findings-fixed count. `bugbotManual: false` skips Bugbot on this path and still requires CI. After the person merges it (`warp:proceed` or a provider approval), move the issue to `jiraDoneStatus` and post the merged comment. `jiraDoneOnManualMerge: false` keeps the old behavior: leave Jira at QA Ready and let QA set Done. A new commit while waiting sets `bugbot_running`, comments "new commits, re-running Bugbot", and does not change the Jira status. A later failure does not move Jira backwards.

`beam.py set` writes the transition and the comment into `.warp/jira-todo.json`. Reed does that file, then `jira_sync.py record` and `record-comment`. `provider.py merge-local` sets the beam to `merged` itself, so the Done request is printed on a local merge too. Connected mode also comments on the pull request. Local mode does not.

Same lookup as the claim move: transition name, then target status name, then (for Done) the done status category. Never a fixed transition id. No Jira key, no connector, or no matching transition: `.warp/outbox.md` and a Herald message, and the merge stands. `/warp-jira-check` shows which of these steps the beam never recorded.

## Local-only

`pushMerge: false`, no `origin` remote, or an origin host that is not GitHub or Bitbucket: never push and never open a pull request. Reed squash-merges the ticket branch into `baseBranch` with `scripts/provider.py merge-local`, which also sets the beam status to `merged` and prints the Jira move. The Jira moves above still happen, because they follow the beam status, not the provider. A missing connector in connected mode falls back to this for that ticket instead of failing. Pull-request comments are skipped in local mode; Jira comments are not.
