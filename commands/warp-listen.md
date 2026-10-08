---
name: warp-listen
description: Take the listener lease and read Slack and Teams on this checkout
---

`/warp-listen` is the listener you open yourself, in this chat. It is the fallback when a background listener is not staying up. It runs the same loop as the one the parent starts. `subagentVm` does not apply. You stay on the parent's checkout.

Take the lease first. That bumps the generation and makes you the holder, so any other copy exits at its next `poll` or `wait` with `reap: exit superseded` and does not read or ack.

```bash
python3 <plugin>/scripts/inbound.py take --beam .warp/beam.json --holder chat
```

The line is `listener: lease generation=<n> holder=chat`. Use that generation and `chat` as `--lease` and `--holder` in every `poll`, `wait`, and `returned`. Then follow `skills/warp-listen/SKILL.md`.

You do not apply the queue. The parent runs `inbound.py apply-pending` when the pending file wakes it (`pass: command`). You do not merge and you do not implement tickets. You never start another agent of any kind.

`reap: exit`, `listen: rotate`, and `listen: stopped` mean return that line. The script wrote the return stamp. Do not start a second. The parent starts the next background listener only from that stamp.
