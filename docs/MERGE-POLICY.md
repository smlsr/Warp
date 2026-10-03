# Merge policy

MEDIUM and below auto-merge. Above MEDIUM waits.

| Size | Class | Auto | Required before merge |
|---|---|---|---|
| S | LOW | yes | Bugbot pass, CI green, every AC evidenced on PR and Jira |
| M | MEDIUM | yes | same |
| L | HIGH | no | same, plus Bitbucket APPROVED or `warp:proceed` |
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

Done only after the merge sha is known. Comment the sha. If the transition fails, leave the beam at `merged` and retry next tick.
