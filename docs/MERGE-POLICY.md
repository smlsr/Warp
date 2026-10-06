# Merge policy

The orchestrator is the only merger. A size in `autoMergeSizes` enters the merge queue when checks are green. A size not in `autoMergeSizes` waits for `/warp-proceed` or `warp:proceed`. L and XL are not special. `checkCommand` empty means Bugbot and CI. `appendOnlyPaths` conflicts keep both sides.

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
- A gate member merging. The gate still needs its checks.
- An approval on a red pipeline.

## Jira

Which status depends on the ticket's `autoMerge` flag. `beam.auto_merge` sets it from the normalized size and `autoMergeSizes` (`.warp/config.yaml`, then the beam copy, then the default S, M). Scan and each `beam.py set` refresh it. Labels such as `L`, `size:L`, and `L — …` all match `L`. A schedule `autoMerge` flag does not override the list.

- Auto-merge (sizes in `autoMergeSizes`; S and M by default): after the merge sha is known, connected or local, move the issue to `jiraDoneStatus` (default Done). Comment the sha, the PR link, and whether the merge was local or connected. If the transition fails, leave the beam at `merged` and retry next tick. Do not move Jira to QA Ready and do not wait for `warp:proceed`.
- Manual (sizes not in `autoMergeSizes`): Bugbot and CI run first, including the fix loop. Only then does `awaiting_approval` move the issue to `jiraQaReadyStatus` (default QA Ready) and comment "Bugbot clean, ready for manual review" plus the findings-fixed count. `bugbotManual: false` skips Bugbot on this path and still requires CI. After the person merges it (`warp:proceed` or a provider approval), move the issue to `jiraDoneStatus` and post the merged comment. `jiraDoneOnManualMerge: false` keeps the old behavior: leave Jira at QA Ready and let QA set Done. A new commit while waiting sets `bugbot_running`, comments "new commits, re-running Bugbot", and does not change the Jira status. A later failure does not move Jira backwards.

`beam.py set` writes the transition and the comment into `.warp/jira-todo.json`. Reed does that file, then `jira_sync.py record` and `record-comment`. `provider.py merge-local` sets the beam to `merged` itself, so the Done request is printed on a local merge too. Connected mode also comments on the pull request. Local mode does not.

Same lookup as the claim move: transition name, then target status name, then (for Done) the done status category. Never a fixed transition id. No Jira key, no connector, or no matching transition: `.warp/outbox.md` and a Herald message, and the merge stands. `/warp-jira-check` shows which of these steps the beam never recorded.

## Local-only

`pushMerge: false`, no `origin` remote, or an origin host that is not GitHub or Bitbucket: never push and never open a pull request. Reed squash-merges the ticket branch into `baseBranch` with `scripts/provider.py merge-local`, which also sets the beam status to `merged` and prints the Jira move. The Jira moves above still happen, because they follow the beam status, not the provider. A missing connector in connected mode falls back to this for that ticket instead of failing. Pull-request comments are skipped in local mode; Jira comments are not.
