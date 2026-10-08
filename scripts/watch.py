#!/usr/bin/env python3
"""Open-work status, stall detection, and the fixer pass.

supervise calls apply() every pass. Status, start, and resume print the
same rows from the live beam.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# Minutes with no progress before a state is stalled. Config keys override these.
PHASE_LIMITS = {
    "implementing": ("stallImplementingMinutes", 90),
    "pr-open": ("stallPrOpenMinutes", 20),
    "reviewing": ("stallReviewingMinutes", 45),
    "fixing": ("stallFixingMinutes", 90),
    "ready": ("stallReadyMinutes", 30),
    "merging": ("stallMergingMinutes", 20),
    "queued": ("stallQueuedMinutes", 180),
    "awaiting_approval": ("stallApprovalMinutes", 240),
    "claimed": ("stallImplementingMinutes", 90),
    "planning": ("stallImplementingMinutes", 90),
    "coding": ("stallImplementingMinutes", 90),
    "review": ("stallReviewingMinutes", 45),
    "bugbot_running": ("stallReviewingMinutes", 45),
    "fix": ("stallFixingMinutes", 90),
    "recovering": ("stallMinutes", 45),
    "alarm": ("stallMinutes", 45),
    "blocked": ("stallApprovalMinutes", 240),
}
_SHUTTLE_PHASES = {"implementing", "fixing", "coding", "claimed", "planning", "recovering", "fix"}
_REVIEW_PHASES = {"reviewing", "pr-open", "review", "bugbot_running", "ready"}
_HIDDEN = {"merged", "done", "skipped"}
_PASS = {"pass", "passed", "ok", "green"}


def _cfg_int(data: dict, key: str, default: int) -> int:
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    raw = cfg.get(key) if isinstance(cfg, dict) else None
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def limit_minutes(data: dict, state: str) -> int:
    """Configured limit for this state, else stallMinutes, else 45."""
    key, default = PHASE_LIMITS.get(state, ("stallMinutes", 45))
    fallback = _cfg_int(data, "stallMinutes", 45)
    if key == "stallMinutes":
        return fallback
    if key in (data.get("config") or {}):
        return _cfg_int(data, key, default)
    return default


def _now(now) -> str:
    import beam as beam_mod

    _dt, text = beam_mod.coerce_now(now)
    return text


def _minutes_between(older: Optional[str], newer: str) -> float:
    import beam as beam_mod

    start = beam_mod.parse_ts(older) if older else None
    end = beam_mod.parse_ts(newer)
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 60.0)


def _age_label(minutes: float) -> str:
    whole = int(minutes)
    hours, mins = divmod(whole, 60)
    if hours:
        return "%dh%dm" % (hours, mins)
    return "%dm" % mins


def _watch(ticket: dict) -> dict:
    raw = ticket.get("watch")
    if not isinstance(raw, dict):
        raw = {}
        ticket["watch"] = raw
    return raw


def _state_of(ticket: dict) -> str:
    return str(ticket.get("phase") or ticket.get("status") or "queued")


def _step_of(ticket: dict) -> str:
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    return str(shuttle.get("step") or ticket.get("phase") or ticket.get("status") or "")


def _pr(ticket: dict) -> dict:
    pr = ticket.get("pr")
    return pr if isinstance(pr, dict) else {}


def _bugbot_label(ticket: dict) -> str:
    raw = str(_pr(ticket).get("bugbot") or "").strip().casefold()
    if raw in {"pass", "passed"}:
        return "pass"
    if raw in {"fail", "failed", "failure"}:
        return "fail"
    if raw in {"running", "pending", "queued"}:
        return "pending"
    if raw:
        return raw
    return "none"


def _ci_label(ticket: dict) -> str:
    pr = _pr(ticket)
    for key in ("ci", "rollup", "check"):
        raw = str(pr.get(key) or "").strip().casefold()
        if raw in {"green", "success", "pass", "passed"}:
            return "green"
        if raw in {"red", "fail", "failed", "failure"}:
            return "red"
        if raw in {"pending", "running", "queued"}:
            return "pending"
    return "none"


def _ac_label(ticket: dict) -> str:
    raw = _pr(ticket).get("acResults")
    if not isinstance(raw, dict) or not raw:
        return "unknown"
    values = [str(value).strip().casefold() for value in raw.values()]
    if all(value in _PASS for value in values):
        return "pass"
    return "fail"


def _retries(ticket: dict) -> int:
    watch = _watch(ticket)
    return int(ticket.get("attempts") or 0) + int(watch.get("fixerAttempts") or 0)


def _open_tickets(data: dict) -> list:
    rows = []
    for ticket in (data.get("tickets") or {}).values():
        if isinstance(ticket, dict) and ticket.get("status") not in _HIDDEN:
            rows.append(ticket)
    rows.sort(key=lambda ticket: str(ticket.get("id")))
    return rows


def _progress_token(ticket: dict) -> str:
    """A new commit, a phase change, or a check result. A heartbeat is not one."""
    pr = _pr(ticket)
    conflict = pr.get("conflict")
    if isinstance(conflict, list):
        conflict_text = ",".join(str(path) for path in conflict)
    else:
        conflict_text = "1" if conflict else ""
    return "|".join(
        [
            _state_of(ticket),
            str(pr.get("headSha") or ""),
            str(pr.get("rollup") or ""),
            str(pr.get("ci") or ""),
            str(pr.get("check") or ""),
            str(pr.get("bugbot") or ""),
            conflict_text,
            "1" if pr.get("behind") else "",
            "1" if pr.get("ciAbsent") else "",
        ]
    )


def _touch(ticket: dict, now: str) -> None:
    """Remember the last real change. A start or a heartbeat does not reset it.

    The first look keeps an existing clock. A later commit, phase, or check
    result moves it. The stall timer is only for a stall that is not already
    classified.
    """
    watch = _watch(ticket)
    token = _progress_token(ticket)
    previous = watch.get("progressToken")
    state = _state_of(ticket)
    changed = False
    if previous is None:
        watch["progressToken"] = token
        recorded = watch.get("state")
        if recorded is not None and recorded != state:
            changed = True
    elif previous != token:
        watch["progressToken"] = token
        changed = True
    if changed:
        watch["since"] = now
        watch["progressAt"] = now
        watch["stalled"] = False
        ticket["stalled"] = False
    watch["state"] = state
    last = ticket.get("lastSeenAt") or ticket.get("updatedAt") or ""
    if not watch.get("since"):
        watch["since"] = now
    if not watch.get("progressAt"):
        watch["progressAt"] = watch["since"]
    watch["lastActive"] = last or watch.get("progressAt") or now


def _mark_stalls(data: dict, now: str, beam_path: Optional[Path], lines: list) -> None:
    for ticket in _open_tickets(data):
        if ticket.get("status") in {"parked"}:
            continue
        _touch(ticket, now)
        watch = _watch(ticket)
        state = _state_of(ticket)
        age = _minutes_between(str(watch.get("progressAt") or ""), now)
        stalled = age > limit_minutes(data, state)
        was = bool(watch.get("stalled"))
        watch["stalled"] = stalled
        ticket["stalled"] = stalled
        if stalled and not was:
            reason = "no progress in %s for %s" % (state, _age_label(age))
            watch["stallReason"] = reason
            lines.append("stall: %s %s" % (ticket.get("id"), reason))
            _log_stall(beam_path, ticket, now, reason)
            _post_alarm(data, ticket, "stalled", lines)


def _log_stall(beam_path: Optional[Path], ticket: dict, now: str, reason: str) -> None:
    if beam_path is None:
        return
    folder = Path(beam_path).parent / "tickets" / str(ticket.get("id"))
    folder.mkdir(parents=True, exist_ok=True)
    line = {
        "at": now,
        "type": "stall",
        "id": ticket.get("id"),
        "state": _state_of(ticket),
        "reason": reason,
    }
    with (folder / "log.jsonl").open("a") as handle:
        handle.write(json.dumps(line, separators=(",", ":")) + "\n")


def _post_alarm(data: dict, ticket: dict, reason: str, lines: list) -> None:
    """One Slack alarm per ticket and reason. A repeat is not posted."""
    key = "%s:%s" % (ticket.get("id"), reason)
    posted = data.get("postedAlarms")
    if not isinstance(posted, list):
        posted = []
        data["postedAlarms"] = posted
    if key in posted:
        return
    posted.append(key)
    lines.append("slack: alarm %s %s" % (ticket.get("id"), reason))
    lines.append("herald: %s %s." % (ticket.get("id"), reason))


def _note_new_alarms(data: dict, lines: list) -> None:
    for ticket in _open_tickets(data):
        alarm = str(ticket.get("alarm") or "").strip()
        watch = _watch(ticket)
        if not alarm or alarm == watch.get("seenAlarm"):
            continue
        watch["seenAlarm"] = alarm
        if ticket.get("status") == "alarm" or alarm:
            _post_alarm(data, ticket, "alarm %s" % alarm, lines)


def _approved(ticket: dict) -> bool:
    pr = _pr(ticket)
    return bool(pr.get("approvedAt") or pr.get("approvedBy") or pr.get("proceededBy"))


def _needs_queue(ticket: dict) -> bool:
    import orchestrator

    if ticket.get("status") in _HIDDEN or ticket.get("status") in {"merging", "parked"}:
        return False
    if not _approved(ticket):
        return False
    return not orchestrator.in_merge_queue(ticket, {})


def _recovery_open(data: dict, ticket: dict) -> bool:
    """stuck and worker-died stop at maxRecoveries. Other alarms are open."""
    alarm = str(ticket.get("alarm") or "")
    cap = _cfg_int(data, "maxRecoveries", 5)
    if cap < 1:
        cap = 3
    if alarm == "stuck":
        return int(ticket.get("stuckRestarts") or 0) < cap
    if alarm == "worker-died":
        return int(ticket.get("recoveries") or 0) < cap
    return True


def _merge_conflict(ticket: dict, snap: Optional[dict]) -> bool:
    """GitHub CONFLICTING or DIRTY, or a conflict flag already on the ticket."""
    snap = snap or {}
    pr = _pr(ticket)
    if pr.get("conflict") or snap.get("conflict"):
        return True
    for source in (snap, pr):
        mergeable = str(source.get("mergeable") or "").upper()
        state = str(source.get("mergeStateStatus") or "").upper()
        if mergeable == "CONFLICTING" or state in {"DIRTY", "CONFLICTING"}:
            return True
    return False


def _ci_never_started(ticket: dict, snap: Optional[dict]) -> bool:
    snap = snap or {}
    if _merge_conflict(ticket, snap) or _pr(ticket).get("behind") or snap.get("behind"):
        return False
    if _pr(ticket).get("ciAbsent") or snap.get("ciAbsent"):
        return True
    return False


def choose_fix(data: dict, ticket: dict, snap: Optional[dict]) -> str:
    """One fixer action, or empty when this ticket should wait."""
    snap = snap or {}
    if ticket.get("status") in _HIDDEN or ticket.get("status") == "parked":
        return ""
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    alarm = str(ticket.get("alarm") or "")
    stalled = bool(ticket.get("stalled"))
    pr = _pr(ticket)
    phase = _state_of(ticket)
    if shuttle.get("pending") and ticket.get("status") != "alarm":
        silent = stalled and (shuttle.get("step") == "repair" or phase in _SHUTTLE_PHASES or alarm in {"worker-died", "stuck"})
        if not silent:
            return ""
    if alarm == "lock-escape" or snap.get("lockEscape"):
        return "lock-escape"
    if _merge_conflict(ticket, snap) or snap.get("behind") or pr.get("behind"):
        return "rebase"
    if _ci_never_started(ticket, snap):
        return "ci-start"
    if phase in _SHUTTLE_PHASES and _recovery_open(data, ticket) and (stalled or alarm in {"worker-died", "stuck"}):
        return "shuttle"
    bugbot = _bugbot_label(ticket)
    if phase in _REVIEW_PHASES and bugbot not in {"pass", "fail"} and (stalled or not pr.get("bugbotRequested")):
        if stalled or phase == "pr-open":
            return "bugbot"
    ci = _ci_label(ticket)
    flaked = bool(snap.get("flake") or pr.get("flake"))
    if alarm == "ci-red" or (stalled and (ci == "pending" or (flaked and ci == "red"))):
        return "ci"
    if _needs_queue(ticket):
        return "queue"
    if stalled and phase in _REVIEW_PHASES and bugbot != "pass":
        return "bugbot"
    if stalled and phase in _SHUTTLE_PHASES and _recovery_open(data, ticket):
        return "shuttle"
    if alarm == "gate-red":
        return ""
    if alarm == "bugbot-failed":
        return "bugbot"
    if alarm and ticket.get("status") == "alarm" and not shuttle.get("pending") and _recovery_open(data, ticket):
        return "shuttle"
    return ""


def _cap(data: dict) -> int:
    return max(1, _cfg_int(data, "maxStallFixes", 5))


def _escalate(data: dict, ticket: dict, action: str, lines: list) -> None:
    reason = "fixer cap: %s" % action
    ticket["status"] = "parked"
    ticket["phase"] = "parked"
    ticket["alarm"] = "stalled"
    ticket["parkReason"] = reason
    import agents

    lines.extend(agents.release_ticket(data, ticket, "parked"))
    watch = _watch(ticket)
    watch["seenAlarm"] = "stalled"
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else None
    if isinstance(shuttle, dict):
        shuttle["pending"] = False
    lines.append("phase: %s parked" % ticket.get("id"))
    lines.append("park: %s %s" % (ticket.get("id"), reason))
    _post_alarm(data, ticket, reason, lines)


def _run_action(data: dict, ticket: dict, action: str, lines: list, beam_path: Optional[Path], now: str) -> None:
    import pipeline

    tid = ticket.get("id")
    if action == "bugbot":
        import agents

        if agents.same_commit_bugbot(ticket):
            lines.append("bugbot: hold %s" % tid)
            return
    watch = _watch(ticket)
    watch["fixerAttempts"] = int(watch.get("fixerAttempts") or 0) + 1
    watch["lastFix"] = action
    watch["lastFixAt"] = now
    if action == "bugbot":
        pipeline._request_bugbot(ticket, lines, data)
        lines.append("fixer: %s bugbot" % tid)
        return
    if action == "ci":
        lines.append("ci: rerun %s" % tid)
        lines.append("fixer: %s ci" % tid)
        return
    if action == "ci-start":
        pipeline.start_ci(data, ticket, lines)
        return
    if action == "rebase":
        pipeline.start_rebase(data, ticket, lines)
        return
    if action == "shuttle":
        phase = "fixing" if _pr(ticket).get("url") else "implementing"
        pipeline._restart(data, ticket, lines, phase, herald="herald: %s shuttle restarted." % tid)
        lines.append("fixer: %s shuttle" % tid)
        return
    if action == "lock-escape":
        lines.append("fixer: %s lock-escape" % tid)
        if beam_path is not None:
            import alarm_repair

            import beam as beam_mod

            beam_mod.atomic_write(Path(beam_path), json.dumps(data, indent=2) + "\n")
            lines.extend(alarm_repair.next_repair(Path(beam_path), now=now))
            if Path(beam_path).is_file():
                loaded = beam_mod.load_json(Path(beam_path))
                fresh = (loaded.get("tickets") or {}).get(tid)
                if isinstance(fresh, dict):
                    data.setdefault("tickets", {})[tid] = fresh
                if isinstance(loaded.get("alarmRepair"), dict):
                    data["alarmRepair"] = loaded["alarmRepair"]
        return
    if action == "queue":
        pr = ticket.setdefault("pr", {})
        if not pr.get("proceededBy"):
            pr["proceededBy"] = pr.get("approvedBy") or "provider"
        ticket["status"] = "review"
        ticket["phase"] = "ready"
        pipeline._ensure_records(ticket)
        lines.append("merge: queue %s" % tid)
        lines.append("fixer: %s queue" % tid)


def _fix_globals(data: dict, lines: list, now: str) -> None:
    import beam as beam_mod
    import orchestrator

    flipped = beam_mod.advance_pending_gates(data, now)
    if flipped:
        lines.extend(beam_mod.gate_flip_lines(flipped, data, run_dispatch=False))
        lines.append("fixer: gates")
    watch = data.get("watch")
    if not isinstance(watch, dict):
        watch = {}
        data["watch"] = watch
    started = bool(watch.pop("baseFixStarted", False))
    if orchestrator.base_is_green(data) or not orchestrator.needs_base_fix(data) or started:
        return
    used = int(watch.get("baseFixes") or 0)
    if used >= _cap(data):
        key = "base:base-red"
        posted = data.setdefault("postedAlarms", [])
        watch["baseAnnouncedAt"] = now
        if key not in posted:
            posted.append(key)
            lines.append("slack: alarm base base-red")
            lines.append("herald: base base-red.")
        return
    watch["baseFixes"] = used + 1
    watch["baseAnnouncedAt"] = now
    checkout = orchestrator.dispatch_checkout(data.get("config") or {}, data)
    lines.append("fixer: base red")
    lines.append("dispatch-base-fix checkout=%s" % checkout)


def _fix_tickets(data: dict, provider: Optional[dict], lines: list, beam_path: Optional[Path], now: str) -> None:
    """One action per stalled or alarmed ticket, in id order."""
    provider = provider or {}
    for ticket in _open_tickets(data):
        if ticket.get("status") == "parked":
            continue
        snap = provider.get(ticket.get("id")) or {}
        action = choose_fix(data, ticket, snap)
        if not action:
            continue
        watch = _watch(ticket)
        if int(watch.get("fixerAttempts") or 0) >= _cap(data):
            _escalate(data, ticket, action, lines)
            continue
        if action in {"rebase", "ci-start", "shuttle"}:
            import beam as beam_mod

            if beam_mod.fix_worker_room(data) < 1:
                continue
        _run_action(data, ticket, action, lines, beam_path, now)


def _digest(data: dict, lines: list, now: str) -> None:
    every = _cfg_int(data, "statusDigestMinutes", 60)
    if every < 1:
        return
    slot = data.get("statusDigest")
    if not isinstance(slot, dict):
        slot = {}
        data["statusDigest"] = slot
    last = slot.get("at")
    if last and _minutes_between(str(last), now) < every:
        return
    slot["at"] = now
    rows = [_row(ticket, now) for ticket in _open_tickets(data)]
    counts = _counts(rows)
    alarms = sum(1 for row in rows if row["alarm"] not in {"", "none"})
    stalls = sum(1 for row in rows if row["stalled"])
    import beam as beam_mod

    bits = ["alarms=%d" % alarms, "stalls=%d" % stalls, "open=%d" % len(rows), beam_mod.in_progress_label(data)]
    bits.extend("%s=%d" % (key, counts[key]) for key in sorted(counts))
    text = " ".join(bits)
    lines.append("digest: %s" % text)
    lines.append("slack: digest %s" % text)


def _row(ticket: dict, now: str) -> dict:
    watch = ticket.get("watch") if isinstance(ticket.get("watch"), dict) else {}
    since = str(watch.get("since") or ticket.get("updatedAt") or now)
    last = str(watch.get("lastActive") or ticket.get("lastSeenAt") or ticket.get("updatedAt") or since)
    state = _state_of(ticket)
    alarm = str(ticket.get("alarm") or "none")
    return {
        "id": ticket.get("id"),
        "state": state,
        "step": _step_of(ticket),
        "age": _age_label(_minutes_between(since, now)),
        "last": last,
        "pr": _pr(ticket).get("url") or "none",
        "bugbot": _bugbot_label(ticket),
        "ci": _ci_label(ticket),
        "ac": _ac_label(ticket),
        "stalled": "yes" if ticket.get("stalled") or watch.get("stalled") else "no",
        "retries": _retries(ticket),
        "alarm": alarm,
        "next": next_action(ticket),
    }


def next_action(ticket: dict) -> str:
    """What Warp will do for this ticket on the next fixer or pipeline pass."""
    if ticket.get("status") == "parked":
        return "parked: %s" % (ticket.get("parkReason") or ticket.get("alarm") or "parked")
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    if shuttle.get("pending"):
        if ticket.get("stalled") and _state_of(ticket) in _SHUTTLE_PHASES:
            return "restart the step"
        return "wait for the Shuttle"
    if str(ticket.get("alarm") or "") == "lock-escape":
        return "widen locks and rerun"
    if _merge_conflict(ticket, {}) or _pr(ticket).get("behind"):
        return "rebase onto main"
    if _pr(ticket).get("ciAbsent"):
        return "start CI"
    if _needs_queue(ticket):
        return "return to the merge queue"
    phase = _state_of(ticket)
    if ticket.get("stalled") and phase in _REVIEW_PHASES and _bugbot_label(ticket) != "pass":
        return "ask Bugbot again"
    if ticket.get("stalled") and _ci_label(ticket) == "pending":
        return "rerun CI"
    if ticket.get("stalled") and phase in _SHUTTLE_PHASES:
        return "restart the step"
    if phase in _REVIEW_PHASES and _bugbot_label(ticket) not in {"pass", "fail"}:
        return "wait for Bugbot"
    if _ci_label(ticket) == "pending":
        return "wait for CI"
    if phase == "ready":
        return "merge when the gate is open"
    if phase == "queued" or ticket.get("status") == "queued":
        return "start when a slot is free"
    if ticket.get("status") == "alarm":
        return "fix %s" % (ticket.get("alarm") or "alarm")
    return "continue %s" % phase


def _counts(rows: list) -> dict:
    counts = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return counts


def _format_row(row: dict) -> str:
    return (
        "%s state=%s step=%s age=%s last=%s pr=%s bugbot=%s ci=%s ac=%s stalled=%s retries=%s alarm=%s next=%s"
        % (
            row["id"],
            row["state"],
            row["step"],
            row["age"],
            row["last"],
            row["pr"],
            row["bugbot"],
            row["ci"],
            row["ac"],
            row["stalled"],
            row["retries"],
            row["alarm"],
            row["next"],
        )
    )


def status_lines(data: dict, now=None) -> list:
    """Alarms and stalls first, then a count of each state, then every open ticket."""
    stamp = _now(now)
    rows = [_row(ticket, stamp) for ticket in _open_tickets(data)]
    alarms = [row for row in rows if row["alarm"] not in {"", "none"}]
    stalls = [row for row in rows if row["stalled"] == "yes" and row["alarm"] in {"", "none"}]
    counts = _counts(rows)
    import beam as beam_mod

    progress = beam_mod.in_progress_label(data)
    live = beam_mod.live_shuttle_label(data)
    lines = ["open: %d" % len(rows), progress, live, "alarms: %d" % len(alarms)]
    lines.extend(_format_row(row) for row in alarms)
    lines.append("stalls: %d" % len(stalls))
    lines.extend(_format_row(row) for row in stalls)
    count_text = " ".join("%s=%d" % (key, counts[key]) for key in sorted(counts)) or "none"
    lines.append("counts: %s" % count_text)
    lines.append("open-work:")
    lines.extend(_format_row(row) for row in rows)
    lines.append(_sweep_status(data))
    data["openWork"] = {
        "at": stamp,
        "alarms": [row["id"] for row in alarms],
        "stalls": [row["id"] for row in rows if row["stalled"] == "yes"],
        "counts": counts,
        "inProgress": progress,
        "liveShuttles": live,
        "tickets": rows,
        "sweep": data.get("repairSweep") if isinstance(data.get("repairSweep"), dict) else None,
    }
    return lines


def _sweep_status(data: dict) -> str:
    raw = data.get("repairSweep")
    if not isinstance(raw, dict) or not raw.get("at"):
        return "sweep: none"
    return "sweep: at=%s findings=%d actions=%d skipped=%d escalated=%d" % (
        raw.get("at"),
        len(raw.get("findings") or []),
        len(raw.get("actions") or []),
        len(raw.get("skipped") or []),
        len(raw.get("escalated") or []),
    )


def snapshot(data: dict, now=None) -> list:
    """Store open work on the beam for a new session. Does not start a fix."""
    return status_lines(data, now=now)


def apply(data: dict, provider: Optional[dict] = None, beam_path: Optional[Path] = None, now=None) -> list:
    """Stall check, one fixer action per stalled or alarmed ticket, then the digest."""
    if (data.get("runState") or "running") in {"paused", "stopped"} or data.get("paused"):
        return ["watch: idle"]
    stamp = _now(now)
    lines: list = []
    _note_new_alarms(data, lines)
    _mark_stalls(data, stamp, Path(beam_path) if beam_path else None, lines)
    _fix_globals(data, lines, stamp)
    _fix_tickets(data, provider, lines, Path(beam_path) if beam_path else None, stamp)
    _digest(data, lines, stamp)
    import sweep

    lines.extend(sweep.run(data, provider=provider, beam_path=beam_path, now=stamp))
    lines.extend(status_lines(data, now=stamp))
    return lines
