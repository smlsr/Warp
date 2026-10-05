# Connectors

Warp uses Cursor's native MCP connections. Names in `.warp/config.yaml` are hints for which connected server to call. If a server is missing, say so and write Herald text to `.warp/outbox.md`. Do not mint tokens.

## Jira

Server: `jiraMcp` (Atlassian). Project `HOS`.

- Fetch issue by key, or by tempId label if the key is not on the beam yet.
- Transition to In Progress when a ticket is claimed (`jiraTransition`, default on), only if the ticket has a Jira key. Read the issue's available transitions and choose with `scripts/jira_sync.py pick`, which matches the transition name, then the target status name (`jiraInProgressStatus`), then an in-progress status category. Never hardcode a transition id. An issue already in progress is left alone, and a Done issue is never reopened. Record the outcome with `jira_sync.py record`. If Jira is not connected or no transition fits, the note goes to `.warp/outbox.md` and the claim stands.
- Release (claim back to `queued`): leave the issue, unless `jiraRestoreOnRelease` is true, then move it back to the status it had. Pause and stop never move an issue.
- Comment: status, PR url, AC evidence, Bugbot result.
- Manual path (`autoMerge` false, L and XL by default): when the ticket moves to `awaiting_approval`, transition to `jiraQaReadyStatus` (default QA Ready). After the merge, leave it there.
- Auto-merge path (`autoMerge` true, S and M by default): after the merge, connected or local, transition to `jiraDoneStatus` (default Done). Retry next tick if it fails; the beam stays `merged`.
- Both use `scripts/jira_sync.py` the same way as the claim move: `plan`, `pick` (by transition name, target status name, then status category; `--kind qa` is name-only, `--kind done` may use the done category), `record`. Never a fixed transition id. A failure is a note in `.warp/outbox.md`, not a failed merge or claim.
- Do not create tickets.

## GitHub and Bitbucket

`scripts/provider.py resolve` chooses the mode from `.warp/config.yaml`.

| Config | Effect |
|---|---|
| `gitProvider: auto` | GitHub if `origin` is a GitHub host, Bitbucket if it is Bitbucket. `/warp-init` writes the detected value and leaves a custom one alone. |
| `gitProvider: github` | Pull requests through `githubMcp`, then the `gh` CLI if `ghCli` is true and `gh` is already logged in. |
| `gitProvider: bitbucket` | Pull requests through `bitbucketMcp`. |
| `pushMerge: false` | Local-only. No push, no pull request, no provider call. |
| no `origin`, or another host | Local-only. `/warp-init` sets `pushMerge: false` when there is no remote. |

Connected mode, in order:

- Open the pull request from `warp/<id>-<jiraKey>` into `baseBranch` (empty detects origin's default, then `main` or `master`).
- Read CI and approvals. A GitHub approving review, or a Bitbucket participant status APPROVED, is the L/XL merge signal, along with `warp:proceed <id>`.
- Comment Bugbot evidence and acceptance-criterion ids.
- Squash-merge only from Reed, only on the policy in MERGE-POLICY.md.

Warp does not install `gh`, create SSH keys, or connect an app. If the connector and the CLI are both missing or error, do not raise an alarm and do not scrape credentials: run `scripts/provider.py note` and take the local path for that ticket.

Local-only: stay on the ticket branch. Reed squash-merges it into `baseBranch` with `scripts/provider.py merge-local`, which also writes `.warp/outbox.md` and asks Herald to post that it was local. A conflict leaves the branches untouched.

## Slack and Teams

`messenger: slack | teams | both`.

There is no webhook or token in Warp. Herald posts through these connected servers only. `/warp-init` and `/warp-scan` write `.warp/notify-post.json` and tell Herald to post it; with no channel or no server the text goes to `.warp/outbox.md`. `notify: quiet` skips both. Warp does not create channels. The shared `warp` channel must exist, and the connected app must be able to post to it.

Every message starts with the header `Warp | <repo> / <project>` because one channel serves many repos. Build messages with `scripts/herald_fmt.py` so the format stays the same; see `agents/herald.md`.

Post alarms, approval requests, gate flips, pause/resume, and digests. Command loopback is a message the next tick reads:

- `warp:proceed HOS-14` or `warp:proceed L-01`
- `warp:retry L-01`
- `warp:pause`
- `warp:resume`

A PR comment with the same verb counts. Record the source.

## Bugbot

Use Cursor Bugbot on the PR. Reed stores pass/fail and the evidence string. A pass with no AC ids is not a pass.
