# Merge policy

MEDIUM and below auto-merge. Above MEDIUM waits.

| Size | Class | Auto | Required before merge |
|---|---|---|---|
| S | LOW | yes | Bugbot pass, CI green, every AC evidenced on PR and Jira |
| M | MEDIUM | yes | same |
| L | HIGH | no | same, plus a provider approval (GitHub review or Bitbucket APPROVED) or `warp:proceed`. Jira moves to QA Ready while it waits. |
| XL | CRITICAL | no | same as L |

`autoMergeSizes` in config changes the cut. Default `[S, M]`.

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

Which status depends on the ticket's `autoMerge` flag, set at ingest from its size and `autoMergeSizes`. Warp does not decide the path again later.

- Auto-merge (S and M by default): after the merge sha is known, connected or local, move the issue to `jiraDoneStatus` (default Done). Comment the sha, the PR link, and whether the merge was local or connected. If the transition fails, leave the beam at `merged` and retry next tick.
- Manual (L and XL by default): when the ticket moves to `awaiting_approval`, move the issue to `jiraQaReadyStatus` (default QA Ready) and comment what the person has to do. After the person merges it, leave Jira there and still post the merged comment. QA moves it on. Warp does not set Done for these.

`beam.py set` writes the transition and the comment into `.warp/jira-todo.json`. Reed does that file, then `jira_sync.py record` and `record-comment`. `provider.py merge-local` sets the beam to `merged` itself, so the Done request is printed on a local merge too. Connected mode also comments on the pull request. Local mode does not.

Same lookup as the claim move: transition name, then target status name, then (for Done) the done status category. Never a fixed transition id. No Jira key, no connector, or no matching transition: `.warp/outbox.md` and a Herald message, and the merge stands. `/warp-jira-check` shows which of these steps the beam never recorded.

## Local-only

`pushMerge: false`, no `origin` remote, or an origin host that is not GitHub or Bitbucket: never push and never open a pull request. Reed squash-merges the ticket branch into `baseBranch` with `scripts/provider.py merge-local`, which also sets the beam status to `merged` and prints the Jira move. The Jira moves above still happen, because they follow the beam status, not the provider. A missing connector in connected mode falls back to this for that ticket instead of failing. Pull-request comments are skipped in local mode; Jira comments are not.
