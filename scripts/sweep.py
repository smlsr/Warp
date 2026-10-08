#!/usr/bin/env python3
"""Run-wide broken-state sweep on a timer.

supervise already fixes one stalled or alarmed ticket at a time. This pass
asks two questions about the whole run: what is broken, and what can be
fixed that is not already queued or running. It reuses the fixer actions
and stays inside the slot cap. Slack is posted only when the sweep starts
something new or escalates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_ORPHAN = {"merged", "done", "parked", "skipped"}
_RED_CI = {"red", "fail", "failed", "failure", "stuck"}
_PENDING_CI = {"pending", "running", "queued"}
_SLOT_ACTIONS = {"rebase", "shuttle", "lock-escape", "fix"}


def _cfg_int(data: dict, key: str, default: int) -> int:
    import watch

    return watch._cfg_int(data, key, default)


def _minutes(older: Optional[str], newer: str) -> float:
    import watch

    return watch._minutes_between(older, newer)


def _now(now) -> str:
    import watch

    return watch._now(now)


def due(data: dict, now: str) -> bool:
    """True when repairSweepMinutes have passed since the last sweep."""
    every = _cfg_int(data, "repairSweepMinutes", 15)
    if every < 1:
        every = 15
    raw = data.get("repairSweep")
    last = raw.get("at") if isinstance(raw, dict) else None
    if not last:
        return True
    return _minutes(str(last), now) >= every


def _pending_state(data: dict) -> dict:
    raw = data.get("repairSweep")
    pending = raw.get("pending") if isinstance(raw, dict) else None
    if not isinstance(pending, dict):
        pending = {}
    return pending


def _busy(ticket: dict, now: str) -> bool:
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    if shuttle.get("pending"):
        return True
    watch = ticket.get("watch") if isinstance(ticket.get("watch"), dict) else {}
    if str(watch.get("lastFixAt") or "") == now:
        return True
    agent = str(ticket.get("agent") or "")
    if agent.startswith("repair-") and ticket.get("status") == "claimed":
        return True
    return False


def _repair_running(data: dict, ticket: dict) -> bool:
    repair = data.get("alarmRepair") if isinstance(data.get("alarmRepair"), dict) else {}
    return repair.get("active") == ticket.get("id")


def _bugbot_findings(source: dict) -> list:
    """Findings for this head. A pr dict or a ticket both honor the sha."""
    import pipeline

    if isinstance(source, dict) and isinstance(source.get("pr"), dict):
        return pipeline.findings_of(source)
    return pipeline.findings_of({"pr": source if isinstance(source, dict) else {}})


def _ticket_action(data: dict, ticket: dict, snap: dict) -> str:
    import watch

    chosen = watch.choose_fix(data, ticket, snap)
    if chosen:
        return chosen
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    if pr.get("conflict") or pr.get("behind") or snap.get("conflict") or snap.get("behind"):
        return "rebase"
    findings = _bugbot_findings(pr)
    bugbot = str(pr.get("bugbot") or "").strip().casefold()
    if findings or bugbot in {"fail", "failed", "failure"}:
        return "fix"
    ci = str(pr.get("ci") or snap.get("ci") or "").strip().casefold()
    rollup = str(pr.get("rollup") or snap.get("rollup") or "").strip().casefold()
    if ci in _RED_CI or rollup in _RED_CI:
        return "ci"
    if ticket.get("stalled") and (ci in _PENDING_CI or rollup in _PENDING_CI):
        return "ci"
    return ""


def _detail(ticket: dict, action: str, snap: dict) -> str:
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    if action == "ci":
        return "ci %s" % (pr.get("ci") or pr.get("rollup") or snap.get("ci") or "red")
    if action == "rebase":
        return "conflict" if pr.get("conflict") or snap.get("conflict") else "behind"
    if action == "fix":
        findings = _bugbot_findings(pr)
        return "bugbot findings" if findings else "bugbot fail"
    if action == "lock-escape":
        return "lock-escape"
    if action == "shuttle":
        return "shuttle silent"
    if action == "bugbot":
        return "bugbot"
    return action


def _room(data: dict) -> int:
    """Slots left for a new Shuttle. Waiting reviews do not fill them.

    maxInProgress is not a slot. Only Shuttles that are actually out count,
    against maxAgents.
    """
    import beam as beam_mod

    return beam_mod.shuttle_room(data)


def _orphan_locks(data: dict, findings: list, actions: list, lines: list) -> None:
    for ticket in (data.get("tickets") or {}).values():
        if not isinstance(ticket, dict):
            continue
        locks = [item for item in (ticket.get("locks") or []) if str(item).strip()]
        if not locks or ticket.get("status") not in _ORPHAN:
            continue
        tid = ticket.get("id")
        findings.append({"kind": "lock", "id": tid, "detail": "orphaned lock"})
        ticket["locks"] = []
        actions.append({"kind": "lock", "id": tid})
        lines.append("sweep: action %s lock removed" % tid)
        lines.append("lock: remove %s" % tid)


def _listener(data: dict, beam_path: Optional[Path], now: str, findings: list, actions: list, skipped: list, lines: list) -> None:
    """Notice a listener that is not mid-read. Do not start one.

    The return stamp is the only start. A stale heartbeat and this sweep
    used to issue `listener: poll`, which is how a second copy appeared.
    """
    if beam_path is None:
        return
    raw = data.get("listener") if isinstance(data.get("listener"), dict) else {}
    holder = str(raw.get("holder") or "")
    if holder or raw.get("pollStartedAt"):
        findings.append({"kind": "listener", "id": "listener", "detail": "live"})
        skipped.append({"kind": "listener", "id": "listener", "reason": "running"})
        lines.append("sweep: skip listener running")
        return
    last = str(raw.get("lastSeenAt") or raw.get("returnedAt") or "")
    if not last or _minutes(last, now) <= 0:
        return
    findings.append({"kind": "listener", "id": "listener", "detail": "idle"})
    lines.append("sweep: finding listener idle")


def _beam_sync(data: dict, pending: dict, findings: list, actions: list, skipped: list, lines: list) -> None:
    if str(data.get("beamSync") or "") != "behind":
        pending.pop("beam", None)
        return
    findings.append({"kind": "beam", "id": "beam", "detail": "behind main"})
    if pending.get("beam"):
        skipped.append({"kind": "beam", "id": "beam", "reason": "running"})
        lines.append("sweep: skip beam running")
        return
    pending["beam"] = True
    actions.append({"kind": "beam", "id": "beam"})
    lines.append("beam: sync")
    lines.append("sweep: action beam sync")


def _base_ci(data: dict, now: str, pending: dict, findings: list, actions: list, skipped: list, escalated: list, lines: list, room: list) -> None:
    import orchestrator
    import watch

    ci = str(data.get("baseCi") or "").strip().casefold()
    red_base = not orchestrator.base_is_green(data)
    if not red_base and ci not in _RED_CI | _PENDING_CI:
        return
    kind = "base" if red_base else "ci"
    detail = "base red" if red_base else "ci %s" % (data.get("baseCi") or "pending")
    findings.append({"kind": kind, "id": "base", "detail": detail})
    slot = data.get("watch") if isinstance(data.get("watch"), dict) else {}
    if str(slot.get("baseAnnouncedAt") or "") == now or pending.get("base"):
        skipped.append({"kind": kind, "id": "base", "reason": "running"})
        lines.append("sweep: skip base running")
        return
    if red_base and not orchestrator.needs_base_fix(data):
        skipped.append({"kind": "base", "id": "base", "reason": "running"})
        lines.append("sweep: skip base running")
        return
    if red_base and int(slot.get("baseFixes") or 0) >= watch._cap(data):
        key = "base:base-red"
        posted = data.setdefault("postedAlarms", [])
        escalated.append({"kind": "base", "id": "base", "detail": "base-red"})
        if key not in posted:
            posted.append(key)
            lines.append("slack: alarm base base-red")
            lines.append("sweep: escalate base base-red")
        return
    if red_base:
        if room[0] < 1:
            skipped.append({"kind": "base", "id": "base", "reason": "slot"})
            lines.append("sweep: skip base slot")
            return
        room[0] -= 1
        watch_slot = data.setdefault("watch", {})
        if not isinstance(watch_slot, dict):
            watch_slot = {}
            data["watch"] = watch_slot
        watch_slot["baseFixes"] = int(watch_slot.get("baseFixes") or 0) + 1
        watch_slot["baseAnnouncedAt"] = now
        checkout = orchestrator.dispatch_checkout(data.get("config") or {}, data)
        actions.append({"kind": "base", "id": "base"})
        lines.append("fixer: base red")
        lines.append("dispatch-base-fix checkout=%s" % checkout)
        lines.append("sweep: action base fix")
        return
    pending["base"] = True
    actions.append({"kind": "ci", "id": "base"})
    lines.append("ci: rerun base")
    lines.append("sweep: action base ci")


def _gates(data: dict, now: str, findings: list, actions: list, escalated: list, lines: list) -> None:
    import beam as beam_mod

    previous = data.get("repairSweep") if isinstance(data.get("repairSweep"), dict) else {}
    seen = set(previous.get("stuckGates") or [])
    flipped = beam_mod.advance_pending_gates(data, now)
    if flipped:
        lines.extend(beam_mod.gate_flip_lines(flipped, data, run_dispatch=False))
        for gate in flipped:
            findings.append({"kind": "gate", "id": gate.get("key"), "detail": "stale"})
            actions.append({"kind": "gate", "id": gate.get("key")})
            lines.append("sweep: action %s gate" % gate.get("key"))
    stuck = []
    for gate in data.get("gates") or []:
        if not isinstance(gate, dict) or gate.get("status") not in {"pending", "red"}:
            continue
        key = str(gate.get("key") or "")
        findings.append({"kind": "gate", "id": key, "detail": "stuck"})
        stuck.append(key)
        if key in seen:
            alarm_key = "gate:%s" % key
            posted = data.setdefault("postedAlarms", [])
            escalated.append({"kind": "gate", "id": key, "detail": "stuck"})
            if alarm_key not in posted:
                posted.append(alarm_key)
                lines.append("slack: alarm %s gate stuck" % key)
                lines.append("sweep: escalate %s gate stuck" % key)
        else:
            lines.append("sweep: finding %s gate stuck" % key)
    data["_stuckGates"] = stuck


def _act_ticket(data: dict, ticket: dict, action: str, lines: list, beam_path: Optional[Path], now: str) -> None:
    import pipeline
    import watch

    if action == "fix":
        pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
        reasons = ["bugbot: %s" % item for item in _bugbot_findings(pr)]
        if not reasons:
            reasons = ["bugbot fail"]
        before = ticket.get("status")
        pipeline.begin_fix(data, ticket, reasons, lines)
        if ticket.get("status") == "parked" and before != "parked":
            lines.append("sweep: escalate %s parked" % ticket.get("id"))
        else:
            lines.append("sweep: action %s fix" % ticket.get("id"))
        return
    watch._run_action(data, ticket, action, lines, Path(beam_path) if beam_path else None, now)
    lines.append("sweep: action %s %s" % (ticket.get("id"), action))


def _tickets(data: dict, provider: Optional[dict], beam_path: Optional[Path], now: str, findings, actions, skipped, escalated, lines, room: list) -> None:
    import watch

    provider = provider or {}
    cap = watch._cap(data)
    rows = [ticket for ticket in (data.get("tickets") or {}).values() if isinstance(ticket, dict)]
    rows.sort(key=lambda ticket: str(ticket.get("id")))
    for ticket in rows:
        if ticket.get("status") in _ORPHAN | {"parked"}:
            continue
        tid = ticket.get("id")
        snap = provider.get(tid) or {}
        if not isinstance(snap, dict):
            snap = {}
        if _busy(ticket, now) or _repair_running(data, ticket):
            action = _ticket_action(data, ticket, snap)
            if action:
                findings.append({"kind": action, "id": tid, "detail": _detail(ticket, action, snap)})
                skipped.append({"kind": action, "id": tid, "reason": "running"})
                lines.append("sweep: skip %s running" % tid)
            continue
        action = _ticket_action(data, ticket, snap)
        if not action:
            continue
        findings.append({"kind": action, "id": tid, "detail": _detail(ticket, action, snap)})
        attempts = int((ticket.get("watch") or {}).get("fixerAttempts") or 0) if isinstance(ticket.get("watch"), dict) else 0
        if attempts >= cap:
            watch._escalate(data, ticket, action, lines)
            escalated.append({"kind": action, "id": tid, "detail": "fixer cap: %s" % action})
            lines.append("sweep: escalate %s fixer cap: %s" % (tid, action))
            continue
        if action in _SLOT_ACTIONS:
            import beam as beam_mod

            if beam_mod.fix_worker_room(data) < 1:
                skipped.append({"kind": action, "id": tid, "reason": "slot"})
                lines.append("sweep: skip %s slot" % tid)
                continue
        _act_ticket(data, ticket, action, lines, beam_path, now)
        if ticket.get("status") == "parked":
            escalated.append({"kind": action, "id": tid, "detail": ticket.get("parkReason") or "parked"})
        else:
            actions.append({"kind": action, "id": tid})


def _log(beam_path: Optional[Path], record: dict) -> None:
    if beam_path is None:
        return
    path = Path(beam_path).parent / "sweep.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def run(data: dict, provider: Optional[dict] = None, beam_path: Optional[Path] = None, now=None) -> list:
    """One sweep when the interval has elapsed. Mutates data."""
    if (data.get("runState") or "running") in {"paused", "stopped"} or data.get("paused"):
        return []
    stamp = _now(now)
    if not due(data, stamp):
        return []
    pending = _pending_state(data)
    findings: list = []
    actions: list = []
    skipped: list = []
    escalated: list = []
    lines: list = []
    room = [_room(data)]
    _orphan_locks(data, findings, actions, lines)
    _listener(data, Path(beam_path) if beam_path else None, stamp, findings, actions, skipped, lines)
    _beam_sync(data, pending, findings, actions, skipped, lines)
    _gates(data, stamp, findings, actions, escalated, lines)
    _base_ci(data, stamp, pending, findings, actions, skipped, escalated, lines, room)
    _tickets(data, provider, Path(beam_path) if beam_path else None, stamp, findings, actions, skipped, escalated, lines, room)
    stuck = data.pop("_stuckGates", [])
    record = {
        "at": stamp,
        "findings": findings,
        "actions": actions,
        "skipped": skipped,
        "escalated": escalated,
        "pending": pending,
        "stuckGates": stuck,
    }
    data["repairSweep"] = record
    _log(Path(beam_path) if beam_path else None, record)
    lines.insert(0, "sweep: findings=%d actions=%d skipped=%d escalated=%d" % (len(findings), len(actions), len(skipped), len(escalated)))
    if actions or escalated:
        lines.append("slack: sweep started %d escalated %d" % (len(actions), len(escalated)))
    return lines
