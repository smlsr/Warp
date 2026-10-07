#!/usr/bin/env python3
"""Per-ticket pipeline the parent runs inside supervise and the tick.

Opening a pull request is not done. The parent moves each ticket through
implementing, pr-open, reviewing, fixing, ready, merging, then merged or
parked. A Shuttle subagent does the steps that change code. Bugbot, the
check rollup, and the serial merge queue run on every pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import orchestrator

_PASS = {"pass", "passed", "ok", "green"}
_FAIL_BUG = {"fail", "failed", "failure"}
_GREEN_ROLLUP = {"green", "success", "pass"}
_RED_WORD = {"red", "fail", "failed", "failure"}
_ALARM_FIX = {"ci-red", "bugbot-failed"}


def _int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _shuttle(ticket: dict) -> dict:
    raw = ticket.get("shuttle")
    if not isinstance(raw, dict):
        raw = {}
        ticket["shuttle"] = raw
    return raw


def pending(ticket: dict) -> bool:
    return bool(_shuttle(ticket).get("pending"))


def mark_shuttle(ticket: dict, step: str) -> None:
    shuttle = _shuttle(ticket)
    shuttle["pending"] = True
    shuttle["step"] = step
    shuttle["hadPr"] = bool((ticket.get("pr") or {}).get("url"))


def clear_shuttle(ticket: dict) -> None:
    shuttle = _shuttle(ticket)
    shuttle["pending"] = False
    report = ticket.get("report")
    if isinstance(report, dict):
        report["returned"] = False
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else None
    if pr is not None:
        pr.pop("shuttleReturned", None)


def ac_map(ticket: dict) -> dict:
    raw = (ticket.get("pr") or {}).get("acResults")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value).strip().casefold() for key, value in raw.items()}


def acs_pass(ticket: dict) -> bool:
    results = ac_map(ticket)
    return bool(results) and all(value in _PASS for value in results.values())


def findings_of(ticket: dict) -> list:
    raw = (ticket.get("pr") or {}).get("bugbotFindings") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip() for item in raw if str(item).strip()]


def bugbot_state(ticket: dict) -> str:
    return str((ticket.get("pr") or {}).get("bugbot") or "").strip().casefold()


def bugbot_finished(ticket: dict, config: Optional[dict]) -> bool:
    if not orchestrator.bugbot_applies(ticket, config):
        return True
    return bugbot_state(ticket) in {"pass", "fail"}


def red_reasons(ticket: dict, config: Optional[dict]) -> list:
    """Bugbot findings, red checks, and failed acceptance criteria."""
    pr = ticket.get("pr") or {}
    reasons = []
    if bugbot_state(ticket) in _FAIL_BUG:
        reasons.append("bugbot fail")
    for item in findings_of(ticket):
        reasons.append("bugbot: %s" % item)
    if str(pr.get("ci") or "").strip().casefold() == "red":
        reasons.append("ci red")
    rollup = str(pr.get("rollup") or "").strip().casefold()
    if rollup in _RED_WORD:
        reasons.append("rollup red")
    if orchestrator.check_command(config):
        if str(pr.get("check") or "").strip().casefold() in _RED_WORD:
            reasons.append("check red")
    for name, value in ac_map(ticket).items():
        if value not in _PASS:
            reasons.append("ac %s %s" % (name, value))
    return reasons


def gate_open(ticket: dict, config: Optional[dict]) -> bool:
    """Bugbot finished clean, the rollup is green, and every AC passed.

    Sizes outside autoMergeSizes still need a person to proceed.
    """
    if red_reasons(ticket, config):
        return False
    if not bugbot_finished(ticket, config):
        return False
    if orchestrator.bugbot_applies(ticket, config) and bugbot_state(ticket) != "pass":
        return False
    if findings_of(ticket):
        return False
    if not orchestrator.checks_green(ticket, config):
        return False
    if not acs_pass(ticket):
        return False
    if ticket.get("autoMerge") or orchestrator._proceeded(ticket):
        return True
    return False


def _apply_snapshot(ticket: dict, snap: Optional[dict]) -> None:
    if not snap:
        return
    pr = ticket.setdefault("pr", {})
    if snap.get("pr"):
        pr["url"] = snap["pr"]
        pr["opened"] = True
    for key in ("rollup", "bugbot", "ci", "check", "checkLog"):
        if key in snap and snap[key] is not None:
            pr[key] = snap[key]
    if "findings" in snap:
        pr["bugbotFindings"] = list(snap.get("findings") or [])
    if isinstance(snap.get("acs"), dict):
        pr["acResults"] = {str(key): str(value) for key, value in snap["acs"].items()}
    if snap.get("escaped"):
        ticket["escaped"] = [str(path) for path in snap["escaped"] if str(path).strip()]
        ticket["status"] = "alarm"
        ticket["alarm"] = "lock-escape"


def _returned(ticket: dict, snap: Optional[dict]) -> bool:
    snap = snap or {}
    if snap.get("returned") or snap.get("workerDied"):
        return True
    if str(snap.get("result") or "").strip().casefold() == "failed":
        return True
    report = ticket.get("report")
    if isinstance(report, dict) and report.get("returned"):
        return True
    pr = ticket.get("pr") or {}
    if pr.get("shuttleReturned"):
        return True
    shuttle = _shuttle(ticket)
    if shuttle.get("pending") and shuttle.get("step") in {"implement", "restart"} and pr.get("url") and not shuttle.get("hadPr"):
        return True
    return False


def _start_lines(data: dict, ticket: dict, step: str, detail: str = "") -> list:
    cfg = data.get("config") or {}
    kind = orchestrator.dispatch_checkout(cfg, data)
    branch = orchestrator.branch_name(ticket, cfg)
    launch = orchestrator.launch_for(cfg, data)
    extra = " launch=%s" % launch.get("launch")
    if launch.get("refuseInProcess"):
        extra += " refuse-in-process"
    lines = [
        "start %s checkout=%s branch=%s%s step=%s" % (ticket.get("id"), kind, branch, extra, step)
    ]
    if detail:
        lines.append("fix: %s %s" % (ticket.get("id"), detail))
    return lines


def _ensure_records(ticket: dict) -> None:
    plan = orchestrator.ensure_plan(ticket)
    if not str(plan.get("body") or "").strip():
        plan["body"] = "plan %s" % ticket.get("id")
    result = ticket.get("result")
    if not isinstance(result, dict):
        result = {}
    if not str(result.get("body") or "").strip():
        pairs = ["%s=%s" % (key, value) for key, value in sorted(ac_map(ticket).items())]
        result["body"] = "acs " + ", ".join(pairs) if pairs else "result %s" % ticket.get("id")
    ticket["result"] = result


def _assign_auto(ticket: dict, config: Optional[dict]) -> None:
    if ticket.get("autoMerge") is not None:
        return
    import beam as beam_mod

    sizes = beam_mod.resolve_auto_merge_sizes(config)
    ticket["autoMerge"] = beam_mod.auto_merge(ticket.get("size") or "", sizes)


def _phase(ticket: dict, name: str, lines: list) -> None:
    ticket["phase"] = name
    lines.append("phase: %s %s" % (ticket.get("id"), name))


def begin_fix(data: dict, ticket: dict, reasons: list, lines: list) -> None:
    """One red round. The third parks the ticket and does not start a Shuttle."""
    if pending(ticket) and _shuttle(ticket).get("step") == "fix":
        lines.append("shuttle: hold %s" % ticket.get("id"))
        return
    text = " | ".join(reasons) or "red"
    outcome = orchestrator.note_failure(ticket, text, data.get("config") or {})
    if outcome == "parked":
        ticket["alarm"] = "parked"
        clear_shuttle(ticket)
        _phase(ticket, "parked", lines)
        lines.append("herald: %s parked." % ticket.get("id"))
        return
    ticket["alarm"] = None
    _phase(ticket, "fixing", lines)
    mark_shuttle(ticket, "fix")
    lines.extend(_start_lines(data, ticket, "fix", text))


def _request_bugbot(ticket: dict, lines: list) -> None:
    pr = ticket.setdefault("pr", {})
    cycle = int(pr.get("reviewCycle") or 0)
    rollup = str(pr.get("rollup") or "").strip().casefold()
    if pr.get("bugbotRequested") and pr.get("bugbotRequestedCycle") == cycle:
        ticket["status"] = "review" if rollup in _GREEN_ROLLUP else "bugbot_running"
        ticket["phase"] = "reviewing"
        lines.append("review: wait %s" % ticket.get("id"))
        return
    pr["bugbotRequested"] = True
    pr["bugbotRequestedCycle"] = cycle
    ticket["status"] = "review" if rollup in _GREEN_ROLLUP else "bugbot_running"
    _phase(ticket, "reviewing", lines)
    lines.append("bugbot: request %s %s" % (ticket.get("id"), pr.get("url") or ""))


def _restart(data: dict, ticket: dict, lines: list, step_phase: str, herald: str = "") -> None:
    ticket["alarm"] = None
    ticket["status"] = "recovering"
    _phase(ticket, step_phase, lines)
    mark_shuttle(ticket, "restart")
    lines.extend(_start_lines(data, ticket, "restart"))
    if herald:
        lines.append(herald)


def _recover_worker(data: dict, ticket: dict, lines: list, already_counted: bool = False) -> None:
    """Restart the step while recoveries is inside maxRecoveries."""
    cap = _int((data.get("config") or {}).get("maxRecoveries"), 3)
    used = int(ticket.get("recoveries") or 0)
    if not already_counted and used < 1:
        ticket["recoveries"] = 1
        used = 1
    blocked = used > cap if already_counted else used >= cap
    if blocked:
        lines.append("worker-died: cap %s" % ticket.get("id"))
        return
    phase = "fixing" if (ticket.get("pr") or {}).get("url") else "implementing"
    if ticket.get("recoveryPriorStatus") == "fix":
        phase = "fixing"
    _restart(
        data,
        ticket,
        lines,
        phase,
        herald="herald: %s worker died. A new Shuttle started." % ticket.get("id"),
    )


def _recover_stuck(data: dict, ticket: dict, lines: list) -> None:
    cap = _int((data.get("config") or {}).get("maxRecoveries"), 3)
    used = int(ticket.get("stuckRestarts") or 0)
    if used >= cap:
        lines.append("stuck: cap %s" % ticket.get("id"))
        return
    ticket["stuckRestarts"] = used + 1
    phase = ticket.get("phase") or ("fixing" if (ticket.get("pr") or {}).get("url") else "implementing")
    if phase in {"merged", "parked", "ready", "reviewing", "pr-open"}:
        phase = "fixing" if (ticket.get("pr") or {}).get("url") else "implementing"
    _restart(data, ticket, lines, phase)


def _recover_alarm(data: dict, ticket: dict, lines: list, deferred: list) -> bool:
    if ticket.get("status") != "alarm":
        return False
    alarm = ticket.get("alarm")
    if alarm == "lock-escape":
        if not pending(ticket):
            mark_shuttle(ticket, "repair")
        deferred.append(ticket.get("id"))
        return True
    if alarm == "worker-died":
        _recover_worker(data, ticket, lines)
        return True
    if alarm == "stuck":
        _recover_stuck(data, ticket, lines)
        return True
    if alarm in _ALARM_FIX:
        ticket["phase"] = "reviewing"
        reasons = red_reasons(ticket, data.get("config") or {}) or [alarm]
        if alarm not in reasons:
            reasons.insert(0, alarm)
        begin_fix(data, ticket, reasons, lines)
        return True
    if alarm == "gate-red":
        lines.append("alarm: hold %s gate-red" % ticket.get("id"))
        return True
    return False


def _ensure_phase(ticket: dict) -> None:
    if ticket.get("phase"):
        return
    pr = ticket.get("pr") or {}
    status = ticket.get("status")
    if pr.get("url") or pr.get("opened"):
        ticket["phase"] = "reviewing"
        ticket["adopted"] = True
        return
    if status == "fix":
        ticket["phase"] = "fixing"
        if ticket.get("agent"):
            mark_shuttle(ticket, "fix")
        return
    if status == "merging":
        ticket["phase"] = "merging"
        return
    if status == "awaiting_approval":
        ticket["phase"] = "ready"
        return
    if status in {"review", "bugbot_running"}:
        ticket["phase"] = "reviewing"
        return
    if status in {"claimed", "planning", "coding", "recovering"}:
        ticket["phase"] = "implementing"
        if ticket.get("agent"):
            mark_shuttle(ticket, "implement")


def _on_return(data: dict, ticket: dict, snap: Optional[dict], lines: list) -> str:
    """Consume one Shuttle return. `stop` means do not evaluate the phase again."""
    phase = ticket.get("phase")
    failed = bool((snap or {}).get("workerDied")) or str((snap or {}).get("result") or "").strip().casefold() == "failed"
    clear_shuttle(ticket)
    if failed:
        orchestrator.note_subagent_failure(ticket)
        _recover_worker(data, ticket, lines, already_counted=True)
        return "stop"
    pr = ticket.setdefault("pr", {})
    if pr.get("url"):
        orchestrator.note_pr_opened(ticket, pr["url"])
    if phase == "fixing":
        pr["reviewCycle"] = int(pr.get("reviewCycle") or 0) + 1
        pr["bugbot"] = ""
        pr["bugbotFindings"] = []
        pr["bugbotRequested"] = False
        _phase(ticket, "pr-open", lines)
        return "continue"
    if phase in {"implementing", "pr-open"} or not phase:
        if not pr.get("url"):
            begin_fix(data, ticket, red_reasons(ticket, data.get("config") or {}) or ["no pull request"], lines)
            return "stop"
        _phase(ticket, "pr-open", lines)
        return "continue"
    return "continue"


def _follow(data: dict, ticket: dict, lines: list) -> None:
    cfg = data.get("config") or {}
    phase = ticket.get("phase")
    if phase == "implementing":
        if pending(ticket):
            lines.append("shuttle: hold %s" % ticket.get("id"))
            return
        if not (ticket.get("pr") or {}).get("url"):
            ticket["status"] = "coding"
            _phase(ticket, "implementing", lines)
            mark_shuttle(ticket, "implement")
            lines.extend(_start_lines(data, ticket, "implement"))
            return
        _phase(ticket, "pr-open", lines)
        phase = "pr-open"
    if phase == "fixing":
        if pending(ticket):
            lines.append("shuttle: hold %s" % ticket.get("id"))
            return
        mark_shuttle(ticket, "fix")
        detail = orchestrator._latest_blocker(ticket) or "fix"
        lines.extend(_start_lines(data, ticket, "fix", str(detail)))
        return
    if phase == "pr-open":
        if orchestrator.bugbot_applies(ticket, cfg) and not bugbot_finished(ticket, cfg):
            _request_bugbot(ticket, lines)
            return
        ticket["phase"] = "reviewing"
        phase = "reviewing"
    if phase == "reviewing":
        pr = ticket.get("pr") or {}
        rollup = str(pr.get("rollup") or "").strip().casefold()
        if rollup in _GREEN_ROLLUP:
            ticket["status"] = "review"
        reasons = red_reasons(ticket, cfg)
        if reasons:
            begin_fix(data, ticket, reasons, lines)
            return
        clean = (
            bugbot_finished(ticket, cfg)
            and orchestrator.checks_green(ticket, cfg)
            and acs_pass(ticket)
            and not findings_of(ticket)
        )
        if clean:
            _assign_auto(ticket, cfg)
            _ensure_records(ticket)
            _phase(ticket, "ready", lines)
            _hold_or_queue(ticket, lines)
            return
        if orchestrator.bugbot_applies(ticket, cfg) and not bugbot_finished(ticket, cfg):
            _request_bugbot(ticket, lines)
            return
        lines.append("review: wait %s" % ticket.get("id"))
        return
    if phase == "ready":
        _assign_auto(ticket, cfg)
        _ensure_records(ticket)
        _hold_or_queue(ticket, lines)


def _hold_or_queue(ticket: dict, lines: list) -> None:
    if ticket.get("autoMerge") or orchestrator._proceeded(ticket):
        ticket["status"] = "review"
        ticket["phase"] = "ready"
        return
    already = ticket.get("status") == "awaiting_approval"
    ticket["status"] = "awaiting_approval"
    ticket["phase"] = "ready"
    if not already:
        lines.append("approve: wait %s" % ticket.get("id"))


def _advance_one(data: dict, ticket: dict, snap: Optional[dict], lines: list, deferred: list) -> None:
    status = ticket.get("status")
    if status in orchestrator.SETTLED:
        if status == "parked":
            ticket["phase"] = "parked"
        elif status == "skipped":
            ticket.setdefault("phase", "parked")
        else:
            ticket["phase"] = "merged"
        return
    _apply_snapshot(ticket, snap)
    if _recover_alarm(data, ticket, lines, deferred):
        return
    _ensure_phase(ticket)
    if not ticket.get("phase"):
        return
    if pending(ticket):
        if not _returned(ticket, snap):
            lines.append("shuttle: hold %s" % ticket.get("id"))
            return
        if _on_return(data, ticket, snap, lines) == "stop":
            return
    _follow(data, ticket, lines)


def _refill(data: dict, lines: list) -> None:
    """Start the next ready ticket when a slot is free, up to the caps."""
    import beam as beam_mod

    if (data.get("runState") or "running") in {"paused", "stopped"} or data.get("paused"):
        return
    rows = beam_mod.ready(data)
    cap = beam_mod.configured_cap(data.get("config") or {})
    actions = orchestrator.dispatch_actions(data, rows, cap=cap)
    for action in actions:
        if action.get("action") == "dispatch-base-fix":
            lines.append("dispatch-base-fix checkout=%s" % action.get("checkout"))
            slot = data.get("watch")
            if not isinstance(slot, dict):
                slot = {}
                data["watch"] = slot
            slot["baseFixStarted"] = True
            continue
        if action.get("action") != "start":
            continue
        tid = action.get("id")
        ticket = (data.get("tickets") or {}).get(tid)
        if not isinstance(ticket, dict):
            continue
        if ticket.get("phase") or pending(ticket):
            continue
        if ticket.get("status") not in {"queued", "blocked"}:
            continue
        ticket["status"] = "coding"
        _phase(ticket, "implementing", lines)
        mark_shuttle(ticket, "implement")
        lines.extend(_start_lines(data, ticket, "implement"))


def _merge_one(data: dict, provider: Optional[dict], lines: list) -> None:
    """One candidate from the serial merge queue."""
    if not orchestrator.base_is_green(data):
        return
    nxt = orchestrator.next_merge(data)
    if not nxt or not nxt.get("ids"):
        return
    cfg = data.get("config") or {}
    ticket = None
    tid = None
    for candidate in nxt["ids"]:
        row = (data.get("tickets") or {}).get(candidate)
        if isinstance(row, dict) and gate_open(row, cfg):
            ticket = row
            tid = candidate
            break
    if ticket is None:
        return
    _ensure_records(ticket)
    snap = (provider or {}).get(tid) or {}
    wanted = str(snap.get("merge") or "merged").strip().casefold()
    if wanted not in {"merged", "enqueue", "rejected"}:
        wanted = "merged"
    reported = orchestrator.merge_report(
        cfg,
        detected_queue=True if snap.get("mergeQueue") else None,
        protection_reject=wanted == "rejected",
        succeeded=wanted == "merged",
    )
    if wanted == "enqueue":
        reported = "enqueue"
    ticket["phase"] = "merging"
    ticket["status"] = "merging"
    lines.append("phase: %s merging" % tid)
    applied = orchestrator.apply_reported_merge(ticket, reported, sha=snap.get("sha") or "merged")
    if not applied:
        lines.append("merge: %s %s" % (tid, reported))
        lines.append("not merged")
        return
    ticket["phase"] = "merged"
    clear_shuttle(ticket)
    url = (ticket.get("pr") or {}).get("url") or ""
    key = ticket.get("jiraKey") or ""
    lines.append("phase: %s merged" % tid)
    lines.append("merge: %s merged" % tid)
    lines.append("herald: %s merged." % tid)
    if key:
        lines.append("jira: MUST DO %s (%s) transition to Done." % (tid, key))
    else:
        lines.append("jira: MUST DO %s transition to Done." % tid)
    lines.append("slack: %s merged %s" % (tid, url))
    lines.append("beam: push")


def _run_repairs(data: dict, beam_path: Optional[Path], lines: list, now) -> None:
    if beam_path is None:
        return
    import alarm_repair
    import beam as beam_mod

    path = Path(beam_path)
    beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n")
    lines.extend(alarm_repair.next_repair(path, now=now))
    if path.is_file():
        loaded = beam_mod.load_json(path)
        data.clear()
        data.update(loaded)


def advance(data: dict, provider: Optional[dict] = None, beam_path: Optional[Path] = None, now=None) -> list:
    """One pass. Mutates data. Opening a PR does not set merged."""
    provider = provider or {}
    lines: list = []
    deferred: list = []
    if (data.get("runState") or "running") in {"paused", "stopped"} or data.get("paused"):
        return ["pipeline: idle"]
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for ticket in list(tickets.values()):
        if not isinstance(ticket, dict):
            continue
        tid = ticket.get("id")
        _advance_one(data, ticket, provider.get(tid) or {}, lines, deferred)
    if deferred and beam_path is not None:
        _run_repairs(data, beam_path, lines, now)
    elif deferred:
        lines.append("alarm-repair: no beam")
    _refill(data, lines)
    _merge_one(data, provider, lines)
    _refill(data, lines)
    import watch

    lines.extend(watch.apply(data, provider=provider, beam_path=beam_path, now=now))
    return lines
