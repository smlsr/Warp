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
| `lock-escape` | Diff left the lock paths. The Shuttle stores those paths on `escaped`. | The parent tick repairs this one. It widens that ticket's locks to the escaped paths, one ticket at a time. The listener does not start the repair Agent. It does not take a path an in-flight ticket holds. |
| `stuck` | No beam update past stuckAfterMinutes | Reattach or retry |
| `worker-died` | Shuttle heartbeat older than staleMinutes, and maxRecoveries is spent | Read the branch, then `warp:retry` |
| `ci-red` | Pipeline red with no agent left | `warp:retry` after the cause is known |
| `gate-red` | Gate checks failed | Fix on the member branch; do not retry dependents |

## Clear

`warp:retry <id>` sets status `queued`, clears `alarm`, leaves `attempts` as history. The one channel listener acks it in channel (`Received warp:retry <id>. Requeueing <id>.`) before that change. `warp:proceed <id>` is only for a size not in `autoMergeSizes` whose Bugbot pass and green CI are already on the beam, not for a red one.

`alarm_repair.py` repairs only `lock-escape`, up to `maxAlarmRepairs`, by widening the lock to the paths that escaped. `orchestrator.py supervise` runs that repair when `checkout.py verify` raises `lock-escape`, then reruns the Shuttle. The same pass restarts a `worker-died` step, starts a fix Shuttle for `ci-red` and `bugbot-failed`, and restarts `stuck` up to `maxRecoveries`. `gate-red` stays on the gate. After `maxFixAttempts` the ticket is `parked`. `warp:retry` is the way to start it again.
