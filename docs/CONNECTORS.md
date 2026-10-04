# Connectors

Warp uses Cursor's native MCP connections. Names in `.warp/config.yaml` are hints for which connected server to call. If a server is missing, say so and write Herald text to `.warp/outbox.md`. Do not mint tokens.

## Jira

Server: `jiraMcp` (Atlassian). Project `HOS`.

- Fetch issue by key, or by tempId label if the key is not on the beam yet.
- Transition to In Progress when a ticket is claimed (`jiraTransition`, default on), only if the ticket has a Jira key. Read the issue's available transitions and choose with `scripts/jira_sync.py pick`, which matches the transition name, then the target status name (`jiraInProgressStatus`), then an in-progress status category. Never hardcode a transition id. An issue already in progress is left alone, and a Done issue is never reopened. Record the outcome with `jira_sync.py record`. If Jira is not connected or no transition fits, the note goes to `.warp/outbox.md` and the claim stands.
- Release (claim back to `queued`): leave the issue, unless `jiraRestoreOnRelease` is true, then move it back to the status it had. Pause and stop never move an issue.
- Comment: status, PR url, AC evidence, Bugbot result.
- Transition to Done only from Reed, only after merge.
- Do not create tickets. The import already has 366.

## Bitbucket

Server: `bitbucketMcp`. Repos `HumanifyOS` and `HumanifyOS-UI`.

- Open PR from `warp/<id>-<jiraKey>` into `main`.
- Read CI and participant status. APPROVED is the L/XL merge signal.
- Comment Bugbot evidence and AC ids.
- Squash merge only from Reed, only on the policy in MERGE-POLICY.md.

If the Bitbucket MCP cannot open a PR, `git push` and the web UI are a human fallback — raise an alarm rather than scraping credentials into the repo.

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
