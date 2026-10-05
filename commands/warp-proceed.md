---
name: warp-proceed
description: Approve a held L/XL PR for merge
---

Run `proceed.py` with the token the person wrote. It accepts a plan id (`WV-01`), a Jira key (`WAR-1`), or a `#` number (`#01`).

```bash
python3 <plugin>/scripts/proceed.py --beam .warp/beam.json WV-01
python3 <plugin>/scripts/proceed.py --beam .warp/beam.json --by <who> "warp:proceed WAR-1"
```

If the ticket is not `awaiting_approval`, or Bugbot and CI are not green, reply with the current status and do not merge. There is no `--force`.

A Slack or Teams `warp:proceed` is handled by the one `warp-listen` listener, which posts `Received warp:proceed <id>. Merging and moving Jira to Done.` in that channel before the merge. This chat command is the same merge when the listener is not the thing that heard it.

When it accepts, merge in that same turn. Do not stop after the provider merge. Local mode runs `provider.py merge-local`. Connected mode squash-merges, then `beam.py set --status merged --sha <sha> --via connected`. That set prints one post-merge MUST DO: move Jira to Done, post the merged comments, record them, and post the Slack reply with the merge sha and the Jira status. Locks release and dependents unblock as part of the status change. `proceed.py ?` prints the options.
