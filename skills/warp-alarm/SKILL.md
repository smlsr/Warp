---
name: warp-alarm
description: "Raise, record, and clear Warp alarms when a PR cannot pass Bugbot, a lock is escaped, or a ticket is stuck. Use for failed reviews and manual intervention."
---

# Warp alarm

An alarm stops the ticket. It does not stop the program unless the ticket is on the critical path and the user pauses.

## Raise

```bash
python3 <plugin>/scripts/beam.py set --beam .warp/beam.json \
  --id C-30 --status alarm --alarm "bugbot-failed after 3 attempts"
```

Herald posts id, reason, PR url, last Bugbot line, and `warp:retry <id>`.

## Reasons

| Reason | Typical cause | Human move |
|---|---|---|
| `bugbot-failed` | AC still red after maxFixAttempts | Fix notes, then `warp:retry` |
| `lock-escape` | Diff left the lock paths | Split a ticket or widen locks in the plan, then retry |
| `stuck` | No beam update past stuckAfterMinutes | Reattach or retry |
| `ci-red` | Pipeline red with no agent left | `warp:retry` after the cause is known |
| `gate-red` | Gate checks failed | Fix on the member branch; do not retry dependents |

## Clear

`warp:retry <id>` sets status `queued`, clears `alarm`, leaves `attempts` as history. The one channel listener acks it in channel (`Received warp:retry <id>. Requeueing <id>.`) before that change. `warp:proceed <id>` is only for an L/XL PR whose Bugbot pass and green CI are already on the beam, not for a red one.

Do not auto-retry an alarm on the next tick. The human signal is the gate.
