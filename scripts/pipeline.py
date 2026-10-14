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
import re
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


def mark_shuttle(ticket: dict, step: str, data: Optional[dict] = None) -> None:
    shuttle = _shuttle(ticket)
    shuttle["pending"] = True
    shuttle["step"] = step
    shuttle["hadPr"] = bool((ticket.get("pr") or {}).get("url"))
    shuttle.pop("returned", None)
    shuttle.pop("returnedAt", None)
    shuttle.pop("startedAt", None)
    # Marked, not launched yet: the previous Shuttle's heartbeat does not make this one busy.
    shuttle["launching"] = True
    if isinstance(ticket.get("pr"), dict):
        # A return belongs to one Shuttle. The next one starts out.
        ticket["pr"].pop("shuttleReturned", None)
    if isinstance(data, dict) and data.get("parentSession"):
        shuttle["session"] = data["parentSession"]
    ticket["needsReplacement"] = False


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


def _repo_root(root) -> Optional[Path]:
    if not root:
        return None
    return Path(root)


def _acs_from_doc(doc) -> dict:
    if not isinstance(doc, dict):
        return {}
    for key in ("acResults", "acs", "results"):
        raw = doc.get(key)
        if isinstance(raw, dict) and raw:
            return {str(name): str(value) for name, value in raw.items()}
    values = list(doc.values())
    if doc and all(isinstance(value, str) and value.strip().casefold() in _PASS | {"fail", "failed", "red"} for value in values):
        return {str(name): str(value) for name, value in doc.items()}
    return {}


def load_shuttle_results(root, ticket_id) -> dict:
    """Acceptance results from RESULT.json when the beam never got acResults."""
    base = _repo_root(root)
    if base is None or not ticket_id:
        return {}
    tid = str(ticket_id)
    paths = [
        base / "tasks" / tid / "RESULT.json",
        base / ".warp" / "tickets" / tid / "RESULT.json",
        base / ".warp" / "tickets" / tid / "result.json",
    ]
    for path in paths:
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        found = _acs_from_doc(doc)
        if found:
            return found
    return {}


def acs_pass(ticket: dict, root=None) -> bool:
    """Every acceptance criterion has a pass for the head. With criteria listed, each id needs one."""
    import acceptance

    results = ac_map(ticket)
    if not results:
        loaded = load_shuttle_results(root, ticket.get("id"))
        if loaded:
            pr = ticket.get("pr")
            if not isinstance(pr, dict):
                pr = {}
                ticket["pr"] = pr
            pr["acResults"] = loaded
            results = ac_map(ticket)
    if acceptance.items(ticket):
        return acceptance.all_pass(ticket)
    return bool(results) and all(value in _PASS for value in results.values())


_PLACEHOLDER_SUFFIX = " Bugbot findings (details not loaded)"


def _placeholder_count(text: str):
    if not isinstance(text, str) or not text.endswith(_PLACEHOLDER_SUFFIX):
        return None
    number = text[: -len(_PLACEHOLDER_SUFFIX)]
    if number.isdigit() and int(number) > 0:
        return int(number)
    return None


def _finding_text(item) -> str:
    """One finding's text. A dict contributes message, then body, never its repr."""
    if isinstance(item, dict):
        for key in ("message", "body"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, bool) or item is None:
        return ""
    if isinstance(item, int):
        return str(item)
    return ""


def coerce_findings(raw) -> list:
    """`bugbotFindings` as a list of non-empty strings.

    An int above 0 is a count whose details were not loaded, so the merge
    gate stays closed. 0 and False are no findings. Anything else that is
    not a string, list, or tuple is ignored.
    """
    if isinstance(raw, bool) or raw is None:
        return []
    if isinstance(raw, int):
        if raw > 0:
            return ["%d%s" % (raw, _PLACEHOLDER_SUFFIX)]
        return []
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if not isinstance(raw, (list, tuple)):
        return []
    found = []
    for item in raw:
        text = _finding_text(item)
        if text:
            found.append(text)
    return found


def findings_of(ticket: dict) -> list:
    pr = ticket.get("pr") if isinstance(ticket, dict) else None
    if not isinstance(pr, dict):
        return []
    head = str(pr.get("headSha") or "").strip()
    tied = str(pr.get("bugbotFindingsSha") or "").strip()
    if head and tied and tied != head:
        return []
    if head and not tied and pr.get("bugbotFindings") not in (None, [], "", 0, False):
        return []
    return coerce_findings(pr.get("bugbotFindings"))


def finding_count(raw) -> int:
    """How many findings a stored value represents, including a bare count."""
    if isinstance(raw, bool) or raw is None:
        return 0
    if isinstance(raw, int):
        return raw if raw > 0 else 0
    texts = coerce_findings(raw)
    if len(texts) == 1:
        counted = _placeholder_count(texts[0])
        if counted is not None:
            return counted
    return len(texts)


def _is_count_value(raw) -> bool:
    if raw is None or isinstance(raw, (bool, int)):
        return True
    if isinstance(raw, str):
        text = raw.strip()
        return (not text) or _placeholder_count(text) is not None
    if isinstance(raw, (list, tuple)):
        texts = coerce_findings(raw)
        if not texts:
            return True
        return len(texts) == 1 and _placeholder_count(texts[0]) is not None
    return True


def next_findings(raw) -> list:
    """One more Bugbot failure, always stored as a list."""
    if _is_count_value(raw):
        return coerce_findings(finding_count(raw) + 1)
    return coerce_findings(raw) + ["Bugbot finding (details not loaded)"]


def bugbot_state(ticket: dict) -> str:
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    head = str(pr.get("headSha") or "").strip()
    tied = str(pr.get("bugbotSha") or "").strip()
    if head and tied and tied != head:
        return ""
    return str(pr.get("bugbot") or "").strip().casefold()


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
    ci = orchestrator._for_head(pr, "ci", "ciSha")
    if ci == "red":
        reasons.append("ci red")
    if orchestrator.provider_rollup(ticket) == "red":
        reasons.append("rollup red")
    if orchestrator.check_command(config):
        if orchestrator._for_head(pr, "check", "checkSha") in _RED_WORD:
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


def _sha_of(snap: dict, pr: dict) -> str:
    return str(snap.get("headSha") or snap.get("sha") or pr.get("headSha") or "").strip()


def _drop_stale_results(pr: dict, new_sha: str) -> None:
    """A green rollup, CI, check, or Bugbot result belongs to the sha that produced it."""
    old = str(pr.get("headSha") or "").strip()
    if not new_sha:
        return
    if new_sha and new_sha != old:
        if old:
            pr["bugbotRequested"] = False
            pr["bugbotRequestedSha"] = ""
            pr.pop("bugbotRequestedAt", None)
        elif pr.get("bugbotRequestedAt") or pr.get("bugbotRequested"):
            pr["bugbotRequestedSha"] = new_sha
    if old and old != new_sha:
        for field, sha_field, pending in (
            ("rollup", "rollupSha", "pending"),
            ("ci", "ciSha", "pending"),
            ("check", "checkSha", "pending"),
            ("bugbot", "bugbotSha", "pending"),
        ):
            tied = str(pr.get(sha_field) or "").strip()
            if tied != new_sha:
                pr[field] = pending
                pr[sha_field] = ""
        if str(pr.get("acSha") or "").strip() != new_sha and pr.get("acResults"):
            # Acceptance results belong to the commit that was checked.
            pr["acResults"] = {}
            pr["acSha"] = ""
        tied_findings = str(pr.get("bugbotFindingsSha") or "").strip()
        if tied_findings != new_sha:
            pr["bugbotFindings"] = []
            pr["bugbotFindingsSha"] = ""
            pr["bugbotRequested"] = False
        return
    if not old and not str(pr.get("bugbotFindingsSha") or "").strip():
        if pr.get("bugbotFindings") not in (None, [], "", 0, False):
            pr["bugbotFindings"] = []
            pr["bugbotRequested"] = False


def _conflicted_value(snap: dict, pr: dict) -> bool:
    mergeable = str(snap.get("mergeable") or pr.get("mergeable") or "").upper()
    state = str(snap.get("mergeStateStatus") or pr.get("mergeStateStatus") or "").upper()
    if mergeable == "CONFLICTING" or state in {"DIRTY", "CONFLICTING"}:
        return True
    if snap.get("conflict") not in (None, False, "", []):
        return True
    return False


def _note_ci_absent(pr: dict, snap: dict, now, cfg: dict) -> None:
    count = snap.get("checkCount")
    if count is None and isinstance(snap.get("checks"), list):
        count = len(snap["checks"])
    rollup = str(snap.get("rollup") if "rollup" in snap else pr.get("rollup") or "").strip().casefold()
    if count is None and rollup == "absent":
        count = 0
    if snap.get("ciConfigured") is False:
        # The repo has no workflow at all. There is nothing to start: do not send a
        # Shuttle to push empty commits. The Shuttle's own check and Bugbot decide.
        pr["ciNone"] = True
        pr["ciAbsent"] = False
        if count is not None:
            pr["checkCount"] = count
        return
    if snap.get("ciConfigured") is True:
        pr["ciNone"] = False
    if count is None:
        return
    pr["checkCount"] = count
    if int(count) != 0:
        pr["ciAbsent"] = False
        return
    try:
        grace = int(cfg.get("ciStartGraceMinutes") if cfg.get("ciStartGraceMinutes") is not None else 5)
    except (TypeError, ValueError):
        grace = 5
    if grace < 0:
        grace = 5
    age = snap.get("headAgeMinutes")
    if age is None:
        stamp = snap.get("headAt") or pr.get("headAt")
        if stamp and now:
            import beam as beam_mod

            start = beam_mod.parse_ts(str(stamp))
            end = beam_mod.parse_ts(str(now))
            if start is not None and end is not None:
                age = max(0.0, (end - start).total_seconds() / 60.0)
    if age is None:
        pr["ciAbsent"] = False
        return
    pr["headAgeMinutes"] = age
    pr["ciAbsent"] = float(age) >= grace


def _apply_snapshot(ticket: dict, snap: Optional[dict], now=None, cfg: Optional[dict] = None) -> None:
    if not snap:
        return
    pr = ticket.setdefault("pr", {})
    cfg = cfg or {}
    new_sha = _sha_of(snap, pr)
    _drop_stale_results(pr, new_sha)
    if new_sha:
        pr["headSha"] = new_sha
    if snap.get("headAt"):
        pr["headAt"] = snap["headAt"]
    if snap.get("pr"):
        pr["url"] = snap["pr"]
        pr["opened"] = True
    head_now = new_sha or str(pr.get("headSha") or "")
    by_hand = bool(head_now) and str(pr.get("bugbotRecordedSha") or "") == head_now
    by_shuttle = by_hand and str(pr.get("bugbotSource") or "") == "shuttle"
    for key, sha_field in (("rollup", "rollupSha"), ("bugbot", "bugbotSha"), ("ci", "ciSha"), ("check", "checkSha")):
        value = str(snap.get(key) or "").strip().casefold() if key == "bugbot" else ""
        if key == "bugbot" and by_hand and not by_shuttle and value in {"", "pending"}:
            # A review a person recorded for this head stands until a Bugbot check finishes.
            continue
        if key == "bugbot" and by_shuttle and (value == "" or (value == "pending" and pr.get("bugbot") == "fail")):
            # No Bugbot check yet: the Shuttle's result is kept (and _hold_for_bugbot decides whether to wait).
            # A Shuttle's fail stands while Bugbot runs. Otherwise Cursor Bugbot's own result counts.
            continue
        if key == "bugbot" and value in {"pass", "fail"}:
            pr["bugbotSource"] = "provider"
            pr["bugbotProviderSha"] = new_sha or head_now
        if key in snap and snap[key] is not None:
            pr[key] = snap[key]
            if new_sha:
                pr[sha_field] = new_sha
    if "checkLog" in snap and snap["checkLog"] is not None:
        pr["checkLog"] = snap["checkLog"]
    if "findings" in snap and not (by_hand and not snap.get("findings")):
        pr["bugbotFindings"] = coerce_findings(snap.get("findings"))
        if new_sha:
            pr["bugbotFindingsSha"] = new_sha
    if snap.get("mergeable"):
        pr["mergeable"] = snap["mergeable"]
    if snap.get("mergeStateStatus"):
        pr["mergeStateStatus"] = snap["mergeStateStatus"]
    if _conflicted_value(snap, pr):
        files = snap.get("conflictFiles")
        if not isinstance(files, list):
            raw = snap.get("conflict")
            files = raw if isinstance(raw, list) else []
        cleaned = [str(path) for path in files if str(path).strip()]
        pr["conflict"] = cleaned or True
        pr["behind"] = False
    elif "conflict" in snap or "mergeable" in snap or "mergeStateStatus" in snap:
        pr["conflict"] = False
    if "behind" in snap or snap.get("mergeStateStatus"):
        state = str(snap.get("mergeStateStatus") or "").upper()
        pr["behind"] = bool(snap.get("behind") or state == "BEHIND") and not pr.get("conflict")
    _note_ci_absent(pr, snap, now, cfg)
    if isinstance(snap.get("acs"), dict):
        pr["acResults"] = {str(key): str(value) for key, value in snap["acs"].items()}
    if snap.get("escaped"):
        ticket["escaped"] = [str(path) for path in snap["escaped"] if str(path).strip()]
        ticket["status"] = "alarm"
        ticket["alarm"] = "lock-escape"


def rebase_detail(ticket: dict) -> str:
    """What the rebase Shuttle does, including route files that must keep both sides."""
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    files = pr.get("conflict")
    names = [str(path) for path in files] if isinstance(files, list) else []
    detail = "rebase onto main"
    if names:
        detail += " files %s" % ", ".join(names)
    detail += "; merge main and resolve the conflict"
    if names and any(name.endswith("routes.go") or "route" in name.casefold() for name in names):
        detail += "; route registration files keep both sides"
    else:
        detail += "; when both sides add entries in a route registration file, keep both"
    detail += "; run checkCommand and push"
    return detail


def start_rebase(data: dict, ticket: dict, lines: list) -> None:
    """Start a rebase Shuttle in this pass. A conflict does not wait for the stall timer."""
    import agents

    if agents.shuttle_busy(data, ticket, beam_path=_beam_path(data)):
        lines.append("shuttle: hold %s" % ticket.get("id"))
        return
    ticket["status"] = "fix"
    ticket["phase"] = "fixing"
    mark_shuttle(ticket, "fix", data)
    lines.extend(_start_lines(data, ticket, "fix", rebase_detail(ticket)))
    lines.append("fixer: %s rebase" % ticket.get("id"))


def start_ci(data: dict, ticket: dict, lines: list) -> None:
    """The head has no check runs past the grace period. Ask for the workflow now."""
    import agents

    tid = ticket.get("id")
    if agents.shuttle_busy(data, ticket, beam_path=_beam_path(data)):
        lines.append("shuttle: hold %s" % tid)
        return
    ticket["status"] = "fix"
    ticket["phase"] = "fixing"
    mark_shuttle(ticket, "fix", data)
    lines.append("ci: start %s" % tid)
    lines.extend(
        _start_lines(
            data,
            ticket,
            "fix",
            "ci never started; push an empty commit so pull_request workflows run",
        )
    )
    lines.append("fixer: %s ci-start" % tid)


def _conflicted(ticket: dict) -> bool:
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    if pr.get("conflict") or pr.get("behind"):
        return True
    mergeable = str(pr.get("mergeable") or "").upper()
    state = str(pr.get("mergeStateStatus") or "").upper()
    return mergeable == "CONFLICTING" or state in {"DIRTY", "CONFLICTING"}


_FIX_SKIP = {"queued", "blocked", "merged", "done", "parked", "skipped"}
_FIX_REVIEW = {"reviewing", "fixing"}


def _defer_fix(data: dict, ticket: dict) -> None:
    """Leave this ticket for the priority dispatch. Do not print review: wait."""
    ids = data.setdefault("_fixDefer", [])
    tid = str(ticket.get("id") or "")
    if tid and tid not in ids:
        ids.append(tid)


def _fix_class(ticket: dict, config: Optional[dict] = None) -> Optional[dict]:
    """Rank a fix the cold start and the supervise pass can launch.

    An open pull request that needs a fix comes first: a conflict, CI that
    never started, red CI, then Bugbot findings. A ticket already in fixing
    with no Shuttle out follows those. Ready and merging tickets are left
    for the merge queue. Dead Shuttle replacements are a later pass.
    """
    if not isinstance(ticket, dict):
        return None
    if ticket.get("status") in _FIX_SKIP or pending(ticket):
        return None
    phase = ticket.get("phase")
    if not phase and ticket.get("status") in {"review", "bugbot_running"}:
        phase = "reviewing"
    elif not phase and ticket.get("status") == "fix":
        phase = "fixing"
    if phase in {"ready", "merging", "merged", "parked"}:
        return None
    import beam as beam_mod

    if not beam_mod.ticket_in_progress(ticket) and ticket.get("status") != "alarm":
        return None
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    tid = str(ticket.get("id") or "")
    if _conflicted(ticket):
        return {"id": tid, "rank": 1, "sub": 0, "kind": "rebase"}
    if pr.get("ciAbsent"):
        return {"id": tid, "rank": 1, "sub": 1, "kind": "ci-start"}
    if phase not in _FIX_REVIEW and ticket.get("status") != "alarm":
        return None
    reasons = red_reasons(ticket, config or {})
    alarm = str(ticket.get("alarm") or "")
    if ticket.get("status") == "alarm" and alarm in {"ci-red", "bugbot-failed"}:
        reasons = list(reasons or [alarm])
        if alarm not in reasons:
            reasons.insert(0, alarm)
    bugbot = any(str(item).startswith("bugbot") for item in reasons) if reasons else False
    if reasons and not bugbot:
        return {"id": tid, "rank": 1, "sub": 2, "kind": "ci", "reasons": reasons}
    if bugbot:
        return {"id": tid, "rank": 1, "sub": 3, "kind": "bugbot", "reasons": reasons}
    if phase == "fixing" and ticket.get("status") != "alarm":
        return {"id": tid, "rank": 1, "sub": 4, "kind": "continue"}
    return None


def _launch_fix(data: dict, ticket: dict, item: dict, lines: list) -> None:
    if pending(ticket):
        return
    kind = item.get("kind")
    if kind == "rebase":
        start_rebase(data, ticket, lines)
        return
    if kind == "ci-start":
        start_ci(data, ticket, lines)
        return
    if kind == "continue":
        mark_shuttle(ticket, "fix", data)
        detail = orchestrator._latest_blocker(ticket) or "fix"
        lines.extend(_start_lines(data, ticket, "fix", str(detail)))
        return
    reasons = list(item.get("reasons") or []) or red_reasons(ticket, data.get("config") or {}) or ["fix"]
    begin_fix(data, ticket, reasons, lines)


def dispatch_fix_workers(data: dict, lines: list, scan: bool = False) -> None:
    """Start fix Shuttles up to maxAgents, highest priority first.

    maxInProgress is not consulted. An open pull request that needs a fix
    goes first: a conflict, CI that never started, red CI, then Bugbot
    findings. A supervise pass launches only the tickets this pass already
    decided need a fix. A cold start scans.
    """
    deferred = {str(tid) for tid in (data.pop("_fixDefer", []) or [])}
    if (data.get("runState") or "running") in {"paused", "stopped"} or data.get("paused"):
        return
    import beam as beam_mod

    chosen = []
    seen = set()
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for ticket in tickets.values():
        if not isinstance(ticket, dict):
            continue
        tid = str(ticket.get("id") or "")
        if not scan and tid not in deferred:
            continue
        item = _fix_class(ticket, data.get("config") or {})
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        chosen.append(item)
    chosen.sort(key=lambda item: (item["rank"], item["sub"], item["id"]))
    for item in chosen:
        if beam_mod.fix_worker_room(data) < 1:
            break
        ticket = tickets.get(item["id"])
        if not isinstance(ticket, dict):
            continue
        _launch_fix(data, ticket, item, lines)


def _was_shuttle(ticket: dict) -> bool:
    """A claim that had a Shuttle, or still says it is doing Shuttle work."""
    import beam as beam_mod

    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    if shuttle.get("pending") or ticket.get("needsReplacement"):
        return True
    return ticket.get("status") in beam_mod.SHUTTLE_WORK


def _release_slot(ticket: dict) -> None:
    shuttle = _shuttle(ticket)
    shuttle["pending"] = False
    ticket["needsReplacement"] = True


def open_parent_session(data: dict, now=None) -> list:
    """Start or resume. A new parent session.

    A fresh heartbeat stays, in VM mode and in subagent mode. A stale heartbeat
    loses its slot and waits for a replacement. This does not launch anything.
    """
    import uuid

    import beam as beam_mod

    _now_dt, now_s = beam_mod.coerce_now(now)
    data["parentSession"] = "%s-%s" % (now_s, uuid.uuid4().hex[:8])
    lines = []
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for tid in sorted(tickets, key=lambda item: str(item)):
        ticket = tickets[tid]
        if not isinstance(ticket, dict):
            continue
        if orchestrator.shuttle_is_live(ticket, data, now=now):
            continue
        if not _was_shuttle(ticket):
            continue
        _release_slot(ticket)
        lines.append("shuttle: release %s" % ticket.get("id"))
    return lines


def _replacement_step(ticket: dict) -> str:
    phase = ticket.get("phase")
    status = ticket.get("status")
    shuttle = _shuttle(ticket)
    if status == "fix" or phase == "fixing" or shuttle.get("step") == "fix":
        return "fix"
    if shuttle.get("step") == "restart" and phase == "fixing":
        return "fix"
    return "restart"


def dispatch_replacements(data: dict, lines: list) -> None:
    """Start dead Shuttle replacements after fix workers, up to the live caps.

    maxInProgress does not apply. Past maxRecoveries the ticket alarms and
    does not start.
    """
    if (data.get("runState") or "running") in {"paused", "stopped"} or data.get("paused"):
        return
    import beam as beam_mod

    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    waiting = []
    for tid in sorted(tickets, key=lambda item: str(item)):
        ticket = tickets[tid]
        if not isinstance(ticket, dict) or not ticket.get("needsReplacement"):
            continue
        if pending(ticket) or orchestrator.shuttle_is_live(ticket, data):
            continue
        waiting.append(ticket)
    for ticket in waiting:
        if beam_mod.shuttle_room(data) < 1:
            break
        step = _replacement_step(ticket)
        if step == "fix" and beam_mod.fix_worker_room(data) < 1:
            continue
        _launch_replacement(data, ticket, lines, step)


def _launch_replacement(data: dict, ticket: dict, lines: list, step: str) -> None:
    import beam as beam_mod

    cap = _int((data.get("config") or {}).get("maxRecoveries"), 5)
    count = int(ticket.get("recoveries") or 0)
    tid = ticket.get("id")
    # Pause or stop ended this Shuttle on purpose. That is not a dead worker:
    # no recovery is counted and nobody is told a worker died.
    halted = bool(ticket.pop("haltResume", None))
    if count >= cap and not halted:
        ticket["needsReplacement"] = False
        clear_shuttle(ticket)
        ticket["status"] = "alarm"
        ticket["alarm"] = "worker-died"
        lines.append("shuttle: alarm %s worker-died" % tid)
        lines.append("herald: %s worker died. Recovery cap reached." % tid)
        return
    prev = ticket.get("status")
    if prev != "recovering":
        ticket["recoveryPriorStatus"] = prev
    ticket["status"] = "recovering"
    stamp = beam_mod.utcnow()
    ticket["recoveredAt"] = stamp
    ticket["workerStartedAt"] = stamp
    ticket.pop("lastSeenAt", None)
    ticket["needsReplacement"] = False
    mark_shuttle(ticket, step, data)
    started = _start_lines(data, ticket, step, "resume after halt" if halted else "replace", replacing=True)
    if started and started[0].startswith("spawn:"):
        lines.extend(started)
        return
    if halted:
        lines.extend(started)
        return
    number = int(ticket.get("recoveries") or 0) + 1
    ticket["recoveries"] = number
    branch = ticket.get("branch") or ""
    lines.append("shuttle: replace %s agent=%s branch=%s" % (tid, ticket.get("agent") or "", branch))
    lines.append("herald: %s worker died. A new Shuttle started." % tid)
    lines.extend(started)


def unfinished(data: dict) -> dict:
    """Work this run already started and has not merged or parked, by what it needs next.

    `fix` needs a fix Shuttle. `shuttle` had a Shuttle that was halted or
    died and needs a new one. `out` has a live Shuttle. `waiting` needs no
    agent right now: it waits on review, checks, an approval, or the merge
    queue.
    """
    import beam as beam_mod

    found = {"fix": [], "shuttle": [], "out": [], "waiting": []}
    cfg = data.get("config") or {}
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for tid in sorted(tickets, key=lambda item: str(item)):
        ticket = tickets[tid]
        if not isinstance(ticket, dict) or not beam_mod.ticket_in_progress(ticket):
            continue
        name = str(ticket.get("id") or tid)
        if orchestrator.shuttle_is_live(ticket, data):
            found["out"].append(name)
        elif ticket.get("needsReplacement"):
            found["shuttle"].append(name)
        elif _fix_class(ticket, cfg):
            found["fix"].append(name)
        else:
            found["waiting"].append(name)
    return found


def unfinished_line(data: dict) -> str:
    """What start and resume print before they launch anything."""
    import beam as beam_mod

    found = unfinished(data)
    total = sum(len(ids) for ids in found.values())
    if not total:
        return "unfinished: none. New tickets start from the ready queue."
    cfg = data.get("config") or {}
    return (
        "unfinished: %d open. %d need a fix, %d need a Shuttle, %d have a Shuttle out, %d wait on review or checks. "
        "These start first, up to maxAgents %d. New tickets start after, while fewer than maxInProgress %d are open."
        % (
            total,
            len(found["fix"]),
            len(found["shuttle"]),
            len(found["out"]),
            len(found["waiting"]),
            beam_mod.configured_cap(cfg),
            beam_mod.max_in_progress(cfg),
        )
    )


def cold_start(beam_path) -> list:
    """Start or resume. Unfinished work first, then the ready queue.

    The order is fixed: open pull requests that need a fix, then tickets
    whose Shuttle was halted or died, then new tickets. All three draw on
    one cap, maxAgents, so a new ticket starts only in a slot unfinished
    work did not need. A Shuttle already out keeps its slot. maxInProgress
    holds only the new tickets.
    """
    import beam as beam_mod

    path = Path(beam_path)
    if not path.is_file():
        return []
    data = beam_mod.load_json(path)
    if (data.get("runState") or "running") in {"paused", "stopped"} or data.get("paused"):
        return []
    lines: list = []
    dispatch_fix_workers(data, lines, scan=True)
    dispatch_replacements(data, lines)
    _refill(data, lines)
    if lines:
        beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n")
    return lines


def live_provider(data: dict, beam_path) -> dict:
    """Pull request state from GitHub when supervise was started without --provider."""
    import provider as provider_mod

    root = Path(beam_path).resolve().parent.parent
    has_ci = provider_mod.repo_has_ci(root)
    found = {}
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for ticket in tickets.values():
        if not isinstance(ticket, dict):
            continue
        if ticket.get("status") in {"merged", "done", "parked", "skipped"}:
            continue
        url = (ticket.get("pr") or {}).get("url") if isinstance(ticket.get("pr"), dict) else ""
        if not url:
            continue
        snap = provider_mod.fetch_pr_state(root, url)
        if snap:
            snap["ciConfigured"] = has_ci
            found[ticket.get("id")] = snap
    return found


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
    head = str(pr.get("headSha") or "")
    if shuttle.get("pending") and shuttle.get("step") == "review" and head and str(pr.get("bugbotRecordedSha") or "") == head:
        # A review Shuttle's only output is the review. Once it is recorded for the head, it is done.
        return True
    if shuttle.get("pending") and shuttle.get("step") in {"implement", "restart"} and pr.get("url") and not shuttle.get("hadPr"):
        return True
    return False


def _beam_path(data: dict):
    raw = data.get("_agentBeam") or ""
    if raw:
        return Path(raw)
    root = data.get("_repoRoot")
    if root:
        return Path(root) / ".warp" / "beam.json"
    return None


def _release_ticket(data: dict, ticket: dict, reason: str) -> list:
    """End this ticket's registered agents when it merges or parks."""
    import agents

    return agents.release_ticket(data, ticket, reason, beam_path=_beam_path(data))


def _start_lines(data: dict, ticket: dict, step: str, detail: str = "", replacing: bool = False) -> list:
    """One Shuttle line. `resume` continues the ticket's agent. `start` is a new one."""
    import agents

    lines = agents.launch_lines(
        data,
        ticket,
        step,
        detail,
        replacing=replacing,
        beam_path=_beam_path(data),
    )
    if lines and (lines[0].startswith("spawn: cap") or lines[0].startswith("spawn: closed")):
        shuttle = _shuttle(ticket)
        shuttle["pending"] = False
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
    """One red round. Past maxFixAttempts (default 5) the ticket parks and does not start a Shuttle."""
    if pending(ticket) and _shuttle(ticket).get("step") == "fix":
        lines.append("shuttle: hold %s" % ticket.get("id"))
        return
    text = " | ".join(reasons) or "red"
    outcome = orchestrator.note_failure(ticket, text, data.get("config") or {})
    if outcome == "parked":
        ticket["alarm"] = "parked"
        lines.extend(_release_ticket(data, ticket, "parked"))
        clear_shuttle(ticket)
        _phase(ticket, "parked", lines)
        lines.append("herald: %s parked." % ticket.get("id"))
        return
    ticket["alarm"] = None
    _phase(ticket, "fixing", lines)
    mark_shuttle(ticket, "fix", data)
    lines.extend(_start_lines(data, ticket, "fix", text))


def _queue_in_review(data: dict, ticket: dict, lines: list) -> None:
    """Ask Jira to move to In Review when Bugbot is requested. Empty status does nothing."""
    path = _beam_path(data)
    if not path:
        return
    import jira_sync

    text = jira_sync.queue_in_review(Path(path), data, ticket)
    if text:
        lines.extend(text.splitlines())


def _request_bugbot(ticket: dict, lines: list, data: Optional[dict] = None) -> None:
    """Ask Bugbot once for the current head commit."""
    import agents

    pr = ticket.setdefault("pr", {})
    if not pr.get("url"):
        lines.append("bugbot: wait %s (no pull request yet)" % ticket.get("id"))
        return
    cycle = int(pr.get("reviewCycle") or 0)
    rollup = str(pr.get("rollup") or "").strip().casefold()
    if not agents.allow_bugbot(ticket):
        ticket["status"] = "review" if rollup in _GREEN_ROLLUP else "bugbot_running"
        ticket["phase"] = "reviewing"
        lines.append("bugbot: hold %s" % ticket.get("id"))
        return
    if data is None:
        data = {}
    agents.note_bugbot(data, ticket, beam_path=_beam_path(data))
    pr["bugbotRequestedCycle"] = cycle
    ticket["status"] = "review" if rollup in _GREEN_ROLLUP else "bugbot_running"
    _phase(ticket, "reviewing", lines)
    lines.append("bugbot: request %s %s" % (ticket.get("id"), pr.get("url") or ""))
    import bugbot

    bugbot.on_request(data, ticket, lines)
    _queue_in_review(data, ticket, lines)


def _step_out(data: dict, ticket: dict, step: str, counted_on_head: bool) -> str:
    """This step is already out on this head: `hold` (launched, not back, not dead) or `relaunch` (never launched).

    Empty means it is not out, and a new Shuttle is counted. A review or verify Shuttle is counted once,
    when it is first marked. Before 1.6.13 every pass that did not see it running counted it again,
    so one review Shuttle that was still working reached 2/2 and the ticket fell back to a person.
    """
    import agents

    shuttle = _shuttle(ticket)
    if not counted_on_head or not shuttle.get("pending") or shuttle.get("step") != step or shuttle.get("returned"):
        return ""
    if _returned(ticket, None):
        return ""
    if shuttle.get("launching") and not shuttle.get("startedAt"):
        return "relaunch"
    if agents.confirmed_dead(ticket, data):
        return ""
    return "hold"


def start_review(data: dict, ticket: dict, lines: list) -> None:
    """bugbotTrigger: shuttle. The work is done and no review is recorded for the head: one review Shuttle."""
    import agents
    import bugbot

    tid = ticket.get("id")
    if agents.shuttle_busy(data, ticket, beam_path=_beam_path(data)):
        lines.append("shuttle: hold %s" % tid)
        return
    pr = ticket.setdefault("pr", {})
    out = _step_out(data, ticket, "review", str(pr.get("bugbotReviewSha") or "") == bugbot._head(pr))
    if out == "hold":
        lines.append("shuttle: hold %s" % tid)
        return
    if out == "relaunch":
        # Marked in an earlier pass and never launched: the same review Shuttle, not a second one.
        count = bugbot.review_shuttles_on_head(pr)
        lines.extend(_start_lines(data, ticket, "review", bugbot.review_detail(ticket, data)))
        lines.append("bugbot: review-shuttle %s %d/%d" % (tid, count, bugbot.MAX_REVIEW_SHUTTLES))
        return
    if bugbot.review_shuttles_on_head(pr) >= bugbot.MAX_REVIEW_SHUTTLES:
        # Two review Shuttles came back with nothing recorded for this head. Ask a person.
        bugbot.ask_manual(data, ticket, lines)
        lines.append("review: wait %s" % tid)
        return
    count = bugbot.note_review_shuttle(ticket)
    ticket["status"] = "review"
    _phase(ticket, "reviewing", lines)
    mark_shuttle(ticket, "review", data)
    lines.extend(_start_lines(data, ticket, "review", bugbot.review_detail(ticket, data)))
    lines.append("bugbot: review-shuttle %s %d/%d" % (tid, count, bugbot.MAX_REVIEW_SHUTTLES))


def _acceptance_only_left(data: dict, ticket: dict) -> bool:
    """Bugbot finished, checks green, no findings: only acceptance results are missing."""
    import acceptance

    cfg = data.get("config") or {}
    if not acceptance.missing(ticket) or pending(ticket):
        return False
    if red_reasons(ticket, cfg) or findings_of(ticket):
        return False
    return bugbot_finished(ticket, cfg) and orchestrator.checks_green(ticket, cfg)


def start_verify(data: dict, ticket: dict, lines: list) -> None:
    """One verify Shuttle: check each acceptance criterion against the branch and record it."""
    import acceptance
    import agents

    tid = ticket.get("id")
    if agents.shuttle_busy(data, ticket, beam_path=_beam_path(data)):
        lines.append("shuttle: hold %s" % tid)
        return
    pr = ticket.setdefault("pr", {})
    out = _step_out(data, ticket, "verify", str(pr.get("acVerifySha") or "") == str(pr.get("headSha") or ""))
    if out == "hold":
        lines.append("shuttle: hold %s" % tid)
        return
    if out == "relaunch":
        count = acceptance.verify_shuttles_on_head(pr)
        root = data.get("_repoRoot") or "<parent checkout>"
        lines.extend(_start_lines(data, ticket, "verify", acceptance.verify_detail(ticket, orchestrator.scripts_dir(), root)))
        lines.append("acceptance: verify-shuttle %s %d/%d missing %s" % (tid, count, acceptance.MAX_VERIFY_SHUTTLES, ", ".join(acceptance.missing(ticket))))
        return
    if acceptance.verify_shuttles_on_head(pr) >= acceptance.MAX_VERIFY_SHUTTLES:
        acceptance.ask_by_hand(ticket, lines, str(_beam_path(data) or ".warp/beam.json"))
        lines.append("review: wait %s" % tid)
        return
    count = acceptance.note_verify_shuttle(ticket)
    ticket["status"] = "review"
    _phase(ticket, "reviewing", lines)
    mark_shuttle(ticket, "verify", data)
    root = data.get("_repoRoot") or "<parent checkout>"
    lines.extend(_start_lines(data, ticket, "verify", acceptance.verify_detail(ticket, orchestrator.scripts_dir(), root)))
    lines.append("acceptance: verify-shuttle %s %d/%d missing %s" % (tid, count, acceptance.MAX_VERIFY_SHUTTLES, ", ".join(acceptance.missing(ticket))))


def _hold_for_bugbot(data: dict, ticket: dict, snap: Optional[dict]) -> None:
    """A Shuttle's review pass does not merge on its own where Cursor Bugbot runs.

    Bugbot ran on this repo (a Bugbot check was seen in this run) and has no result for this head yet:
    the Shuttle's pass is kept on pr.shuttleReview and pr.bugbot is cleared, so the ticket waits for
    Bugbot, asks it with a comment after bugbotStartMinutes, and a finding goes to a fix step.
    Before 1.6.14 a ticket merged on the Shuttle's review alone and Bugbot's findings came after the merge.
    """
    if isinstance(snap, dict) and str(snap.get("bugbot") or "").strip():
        data["bugbotSeen"] = True
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    if not data.get("bugbotSeen") or str(pr.get("bugbotSource") or "") != "shuttle":
        return
    if str(pr.get("bugbot") or "").casefold() != "pass" or not bugbot_state(ticket):
        return
    head = str(pr.get("headSha") or "")
    if head and str(pr.get("bugbotProviderSha") or "") == head:
        return
    pr["bugbot"] = ""


def _review_only_left(data: dict, ticket: dict) -> bool:
    """bugbotTrigger: shuttle, the pull request is open, the work came back, and only the review is missing."""
    import bugbot

    cfg = data.get("config") or {}
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    if bugbot.trigger(data) != "shuttle" or not pr.get("url"):
        return False
    if bugbot.shuttle_reviewed(pr):
        return False  # reviewed already; what is left is Cursor Bugbot's own result
    if not (pr.get("agentFinished") or pr.get("opened")):
        return False
    if not orchestrator.bugbot_applies(ticket, cfg) or bugbot_finished(ticket, cfg):
        return False
    if red_reasons(ticket, cfg) or _conflicted(ticket):
        return False
    return True


def _restart(data: dict, ticket: dict, lines: list, step_phase: str, herald: str = "") -> None:
    import agents

    if agents.shuttle_busy(data, ticket, beam_path=_beam_path(data)):
        lines.append("shuttle: hold %s" % ticket.get("id"))
        return
    replacing = agents.confirmed_dead(ticket, data) and not bool(agents.can_resume(data, ticket, beam_path=_beam_path(data)))
    ticket["alarm"] = None
    ticket["status"] = "recovering"
    _phase(ticket, step_phase, lines)
    mark_shuttle(ticket, "restart", data)
    lines.extend(_start_lines(data, ticket, "restart", replacing=replacing))
    if herald and not any(line.startswith("spawn:") for line in lines):
        lines.append(herald)


def _recover_worker(data: dict, ticket: dict, lines: list, already_counted: bool = False) -> None:
    """Restart the step while recoveries is inside maxRecoveries."""
    cap = _int((data.get("config") or {}).get("maxRecoveries"), 5)
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
    cap = _int((data.get("config") or {}).get("maxRecoveries"), 5)
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
    if alarm in {"stuck", "worker-died", "stalled"} and _review_only_left(data, ticket):
        # The work is done; only the Bugbot review is missing. A review Shuttle, not a restart.
        ticket["alarm"] = None
        ticket["status"] = "review"
        ticket["phase"] = "reviewing"
        clear_shuttle(ticket)
        lines.append("alarm: %s %s cleared; the work is done and only the review is missing" % (ticket.get("id"), alarm))
        start_review(data, ticket, lines)
        return True
    if alarm == "lock-escape":
        if not pending(ticket):
            mark_shuttle(ticket, "repair", data)
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
        _defer_fix(data, ticket)
        return True
    if alarm == "gate-red":
        lines.append("alarm: hold %s gate-red" % ticket.get("id"))
        return True
    return False


def _ensure_phase(ticket: dict, data: Optional[dict] = None) -> None:
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
        if ticket.get("needsReplacement"):
            return
        if pending(ticket) and isinstance(data, dict) and not orchestrator.shuttle_is_live(ticket, data):
            return
        if ticket.get("agent") and not pending(ticket):
            ticket["needsReplacement"] = True
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
        if ticket.get("needsReplacement"):
            return
        if pending(ticket) and isinstance(data, dict) and not orchestrator.shuttle_is_live(ticket, data):
            return
        if ticket.get("agent") and not pending(ticket):
            ticket["needsReplacement"] = True


def _on_return(data: dict, ticket: dict, snap: Optional[dict], lines: list) -> str:
    """Consume one Shuttle return. `stop` means do not evaluate the phase again."""
    phase = ticket.get("phase")
    failed = bool((snap or {}).get("workerDied")) or str((snap or {}).get("result") or "").strip().casefold() == "failed"
    import agents

    if not failed:
        agents.note_returned(data, ticket, beam_path=_beam_path(data))
    else:
        agents.note_closed(data, ticket, "dead", beam_path=_beam_path(data))
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
            if data.get("_live") and ticket.get("branch"):
                # The Shuttle may have pushed and been refused the pull request (a 403 on its VM).
                # The parent opens it; only a branch that is not on origin goes back to the Shuttle.
                request_pr_open(ticket, lines)
                return "stop"
            begin_fix(data, ticket, red_reasons(ticket, data.get("config") or {}) or ["no pull request"], lines)
            return "stop"
        _phase(ticket, "pr-open", lines)
        return "continue"
    return "continue"


def _ensure_acs(data: dict, ticket: dict) -> None:
    if ac_map(ticket):
        return
    loaded = load_shuttle_results(data.get("_repoRoot"), ticket.get("id"))
    if not loaded:
        return
    pr = ticket.get("pr")
    if not isinstance(pr, dict):
        pr = {}
        ticket["pr"] = pr
    pr["acResults"] = loaded


def _follow(data: dict, ticket: dict, lines: list, now=None) -> None:
    cfg = data.get("config") or {}
    phase = ticket.get("phase")
    if ticket.get("needsReplacement") and not pending(ticket):
        if _fix_class(ticket, cfg):
            _defer_fix(data, ticket)
        return
    if _conflicted(ticket):
        _defer_fix(data, ticket)
        return
    if (ticket.get("pr") or {}).get("ciAbsent"):
        _defer_fix(data, ticket)
        return
    if phase == "implementing":
        if pending(ticket):
            lines.append("shuttle: hold %s" % ticket.get("id"))
            return
        if not (ticket.get("pr") or {}).get("url"):
            ticket["status"] = "coding"
            _phase(ticket, "implementing", lines)
            mark_shuttle(ticket, "implement", data)
            lines.extend(_start_lines(data, ticket, "implement"))
            return
        _phase(ticket, "pr-open", lines)
        phase = "pr-open"
    if phase == "fixing":
        if pending(ticket):
            lines.append("shuttle: hold %s" % ticket.get("id"))
            return
        _defer_fix(data, ticket)
        return
    if phase == "pr-open":
        if not (ticket.get("pr") or {}).get("url") and (ticket.get("pr") or {}).get("openRequested"):
            if data.get("_live"):
                request_pr_open(ticket, lines)
            else:
                lines.append("pr: wait %s (no pull request yet)" % ticket.get("id"))
            return
        if orchestrator.bugbot_applies(ticket, cfg) and not bugbot_finished(ticket, cfg):
            _request_bugbot(ticket, lines, data)
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
            _defer_fix(data, ticket)
            return
        clean = (
            bugbot_finished(ticket, cfg)
            and orchestrator.checks_green(ticket, cfg)
            and acs_pass(ticket, data.get("_repoRoot"))
            and not findings_of(ticket)
        )
        if clean:
            _assign_auto(ticket, cfg)
            _ensure_records(ticket)
            _phase(ticket, "ready", lines)
            _hold_or_queue(ticket, lines)
            return
        if orchestrator.bugbot_applies(ticket, cfg) and not bugbot_finished(ticket, cfg):
            import bugbot

            if bugbot.trigger(data) == "shuttle":
                pr_now = ticket.get("pr") or {}
                head = str(pr_now.get("headSha") or "")
                if not pr_now.get("bugbotRequested") or (head and str(pr_now.get("bugbotRequestedSha") or "") != head):
                    # Record the request (and the Jira In Review move) for this head first.
                    _request_bugbot(ticket, lines, data)
                    return
                if bugbot.shuttle_reviewed(pr_now):
                    # The Shuttle passed. Cursor Bugbot must report on this head before the merge.
                    if bugbot.check_silence(data, ticket, lines, now=now):
                        return
                    lines.append("bugbot: wait %s (the Shuttle's review passed; waiting for Cursor Bugbot on %s)" % (ticket.get("id"), head[:12]))
                    return
                start_review(data, ticket, lines)
                return
            # Requested and never started: comment `bugbot run`, switching the trigger when it was auto.
            if bugbot.check_silence(data, ticket, lines, now=now):
                return
            _request_bugbot(ticket, lines, data)
            return
        if _conflicted(ticket):
            start_rebase(data, ticket, lines)
            return
        if _acceptance_only_left(data, ticket):
            start_verify(data, ticket, lines)
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


def _advance_one(data: dict, ticket: dict, snap: Optional[dict], lines: list, deferred: list, now=None) -> None:
    status = ticket.get("status")
    if status in orchestrator.SETTLED:
        if status == "parked":
            ticket["phase"] = "parked"
        elif status == "skipped":
            ticket.setdefault("phase", "parked")
        else:
            ticket["phase"] = "merged"
        return
    _apply_snapshot(ticket, snap, now=now, cfg=data.get("config") or {})
    if confirm_merge(data, ticket, snap, lines):
        return
    _hold_for_bugbot(data, ticket, snap)
    import agents

    agents.finish_bugbot(data, ticket, now=now, beam_path=_beam_path(data))
    _ensure_acs(data, ticket)
    if _recover_alarm(data, ticket, lines, deferred):
        return
    _ensure_phase(ticket, data)
    if not ticket.get("phase"):
        return
    if pending(ticket) and _returned(ticket, snap):
        # A Shuttle that came back is not a dead one: consume its return first.
        if _on_return(data, ticket, snap, lines) == "stop":
            return
    elif pending(ticket) and not orchestrator.shuttle_is_live(ticket, data, now=now):
        _release_slot(ticket)
    elif pending(ticket):
        lines.append("shuttle: hold %s" % ticket.get("id"))
        return
    _follow(data, ticket, lines, now=now)


def _refill(data: dict, lines: list) -> None:
    """Start the next ready ticket when a slot is free, up to the caps."""
    import beam as beam_mod

    if (data.get("runState") or "running") in {"paused", "stopped"} or data.get("paused"):
        return
    rows = beam_mod.ready(data)
    hold = beam_mod.take_launch_hold(data)
    if hold:
        lines.append(hold)
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
        mark_shuttle(ticket, "implement", data)
        started = _start_lines(data, ticket, "implement")
        lines.extend(started)
        if started and started[0].startswith("start "):
            _queue_claim(data, ticket, lines)


def _queue_claim(data: dict, ticket: dict, lines: list) -> None:
    """The Jira move to In Progress for a ticket this pass started (once per ticket)."""
    path = _beam_path(data)
    if not path:
        return
    import jira_sync

    text = jira_sync.queue_claim(Path(path), data, ticket)
    if text:
        lines.extend(text.splitlines())


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
    if data.get("_live") and "merge" not in snap:
        # A live pass. Merged means the provider says merged, never "nothing said otherwise".
        request_merge(data, ticket, snap, lines)
        return
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
    finish_merge(data, ticket, lines)


def request_pr_open(ticket: dict, lines: list) -> None:
    ticket.setdefault("pr", {})["openRequested"] = True
    ticket["phase"] = "pr-open"
    lines.append("pr: open %s %s" % (ticket.get("id"), ticket.get("branch") or ""))


_PR_LINE = re.compile(r"^pr: open (\S+) (\S+)")


def run_pr_opens(data: dict, beam_path, lines: list, opener=None) -> list:
    """Open the pull request for each `pr: open` this pass printed (gh), or say how to."""
    import beam as beam_mod
    import provider as provider_mod

    out = []
    root = Path(beam_path).resolve().parent.parent
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    cfg = dict(data.get("config") or {})
    try:
        base = str(cfg.get("baseBranch") or provider_mod.resolve(root, cfg).get("baseBranch") or "main")
    except Exception:
        base = str(cfg.get("baseBranch") or "main")
    for line in list(lines):
        match = _PR_LINE.match(line)
        if not match or match.group(1) not in tickets:
            continue
        tid, branch = match.group(1), match.group(2)
        ticket = tickets[tid]
        key = ticket.get("jiraKey") or ""
        title = "[%s] %s" % (tid, ticket.get("summary") or tid)
        body = "Warp ticket %s%s.\n\nOpened by the Warp parent: the Shuttle pushed `%s` but could not open the pull request.\n\nAcceptance:\n%s" % (
            tid, " (%s)" % key if key else "", branch,
            "\n".join("- %s: %s" % pair for pair in __import__("acceptance").items(ticket)) or "- (none listed)")
        outcome, detail = (opener or provider_mod.open_pr)(root, branch, base, title, body)
        if outcome in {"opened", "found"}:
            orchestrator.note_pr_opened(ticket, detail, beam_mod.utcnow())
            ticket["pr"].pop("openRequested", None)
            if ticket.get("status") in {"claimed", "recovering", "planning", "coding", "fix"}:
                ticket["status"] = "review"
            ticket["phase"] = "pr-open"
            out.append("pr: %s %s %s (%s)" % ("opened" if outcome == "opened" else "found", tid, detail,
                                             "the Shuttle pushed but could not open it" if outcome == "opened" else "already open"))
            continue
        if outcome == "missing":
            ticket["pr"].pop("openRequested", None)
            begin_fix(data, ticket, ["the Shuttle returned without a pull request and %s; push it and open the pull request" % detail], out)
            continue
        scripts = orchestrator.scripts_dir()
        out.append(
            "pr: MUST DO %s: open a pull request from %s into %s with the GitHub MCP create_pull_request tool (title %r), "
            "then record it: python3 %s/orchestrator.py opened --beam %s --id %s --pr <url>. (%s)"
            % (tid, branch, base, title, scripts, Path(beam_path).resolve(), tid, detail)
        )
    return out


def request_merge(data: dict, ticket: dict, snap: dict, lines: list) -> None:
    """Ask for the merge once per head commit. Supervise runs it; a later pass confirms it."""
    import beam as beam_mod

    tid = ticket.get("id")
    pr = ticket.setdefault("pr", {})
    if str(snap.get("prState") or "").upper() == "MERGED":
        finish_merge(data, ticket, lines, sha=str(snap.get("mergeSha") or ""), how="merged on the provider (confirmed)")
        return
    head = str(pr.get("headSha") or "")
    ticket["status"] = "merging"
    ticket["phase"] = "merging"
    if head and str(pr.get("mergeRequestedSha") or "") == head:
        lines.append("merge: wait %s (asked; the provider does not show it merged yet)" % tid)
        return
    pr["mergeRequestedSha"] = head
    pr["mergeRequestedAt"] = beam_mod.utcnow()
    lines.append("phase: %s merging" % tid)
    lines.append("merge: request %s %s" % (tid, pr.get("url") or ""))


def confirm_merge(data: dict, ticket: dict, snap: Optional[dict], lines: list) -> bool:
    """A ticket asked to merge, and the provider now shows its pull request merged."""
    if not isinstance(snap, dict) or str(snap.get("prState") or "").upper() != "MERGED":
        return False
    if ticket.get("status") in orchestrator.SETTLED:
        return False
    finish_merge(data, ticket, lines, sha=str(snap.get("mergeSha") or ""), how="merged on the provider (confirmed)")
    return True


_MERGE_LINE = re.compile(r"^merge: request (\S+) (\S*)")


def run_merges(data: dict, beam_path, lines: list, merger=None) -> list:
    """Merge each `merge: request` this pass printed, through the provider, and confirm it.

    `merger(root, number, cfg)` returns (outcome, detail) like provider.merge_connected. A merge
    that cannot run here prints a MUST DO line and leaves the ticket merging: the next pass marks
    it merged only when the provider shows the pull request merged.
    """
    import provider as provider_mod

    out = []
    root = Path(beam_path).resolve().parent.parent
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    cfg = dict(data.get("config") or {})
    try:
        import prompt_gate

        cfg.update(prompt_gate.effective_config(data, root))
    except Exception:
        pass
    mode = ""
    try:
        mode = str(provider_mod.resolve(root, cfg).get("mode") or "")
    except Exception:
        mode = ""
    for line in list(lines):
        match = _MERGE_LINE.match(line)
        if not match or match.group(1) not in tickets:
            continue
        tid, url = match.group(1), match.group(2)
        ticket = tickets[tid]
        scripts = orchestrator.scripts_dir()
        if mode == "local" or not url:
            out.append("merge: MUST DO %s: local mode, no pull request to merge. Run python3 %s/provider.py merge-local "
                       "--root %s --beam %s --id %s --branch %s" % (tid, scripts, root, Path(beam_path).resolve(), tid, ticket.get("branch") or ""))
            continue
        number = str(url).rstrip("/").split("/")[-1]
        outcome, detail = (merger or provider_mod.merge_connected)(root, number, cfg)
        if outcome == "merged":
            finish_merge(data, ticket, out, sha=detail if re.fullmatch(r"[0-9a-f]{7,40}", detail or "") else "",
                         how="merged on the provider (gh pr merge --squash, confirmed MERGED)")
            continue
        ticket.setdefault("pr", {})["mergeAttempt"] = outcome
        if outcome == "enqueue":
            out.append("merge: %s enqueued (%s). Marked merged when the provider shows it merged." % (tid, detail))
            continue
        ticket["pr"]["mergeError"] = detail
        out.append("merge: not merged %s: %s" % (tid, (detail or "rejected").splitlines()[0]))
        out.append(
            "merge: MUST DO %s: merge %s (squash) with the GitHub MCP merge tool (merge_pull_request) or `gh pr merge %s --squash`, "
            "then let the next pass confirm it. Warp marks it merged only when the provider shows the pull request merged. "
            "Do not mark it merged by hand. If the tool is not allowed, add it with /warp-allow-notify --with-git." % (tid, url, number)
        )
        out.append("herald: %s is ready to merge, but Warp could not merge it here: %s" % (tid, (detail or "rejected").splitlines()[0]))
    return out


def finish_merge(data: dict, ticket: dict, lines: list, sha: str = "", how: str = "") -> None:
    """The pull request is merged on the provider (or locally in local mode). Record it and say so."""
    tid = ticket.get("id")
    pr = ticket.setdefault("pr", {})
    if ticket.get("status") == "merged" and pr.get("mergeAnnounced"):
        # Already recorded and announced. Saying it again asked for the same Jira Done move twice (WV-10).
        if sha and not pr.get("sha"):
            pr["sha"] = sha
        return
    ticket["status"] = "merged"
    pr["mergeAnnounced"] = True
    import beam as beam_mod

    pr.setdefault("mergedAt", beam_mod.utcnow())
    if sha:
        ticket.setdefault("pr", {})["sha"] = sha
    if how:
        # Seen merged on the provider (or merged locally). A merge recorded without this is checked on resume.
        ticket.setdefault("pr", {})["mergeConfirmed"] = True
    ticket["phase"] = "merged"
    lines.extend(_release_ticket(data, ticket, "merged"))
    clear_shuttle(ticket)
    url = (ticket.get("pr") or {}).get("url") or ""
    key = ticket.get("jiraKey") or ""
    if how:
        lines.append("merge: %s %s" % (tid, how))
    lines.append("phase: %s merged" % tid)
    lines.append("merge: %s merged" % tid)
    lines.append("herald: %s merged." % tid)
    done_at = (ticket.get("jira") or {}).get("doneAt") if isinstance(ticket.get("jira"), dict) else None
    if ticket.get("followUpOf"):
        lines.append("jira: %s fixes Bugbot findings on %s after its merge. No Jira move." % (tid, ticket.get("followUpOf")))
    elif done_at:
        lines.append("jira: %s already Done (recorded %s). Nothing to move." % (tid, done_at))
    elif key:
        lines.append("jira: MUST DO %s (%s) transition to Done." % (tid, key))
    else:
        lines.append("jira: MUST DO %s transition to Done." % tid)
    lines.append("slack: %s merged %s" % (tid, url))
    lines.append("beam: push")


LATE_WINDOW_MINUTES = 60


def late_findings(data: dict, beam_path, lines: list, fetch=None, now=None) -> list:
    """Cursor Bugbot findings that arrived after a merge become a follow-up fix ticket.

    Each merged ticket is checked until Bugbot has reported on its merged head: a pass settles it, a
    finding queues `<id>-BB<n>` with the findings as its acceptance criteria and the same locks, and no
    Bugbot check within LATE_WINDOW_MINUTES of the merge settles it as none. Before 1.6.14 a finding on a
    merged pull request (PR #12, #13) was never acted on.
    """
    import beam as beam_mod
    import provider as provider_mod

    out = []
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    cfg = data.get("config") or {}
    root = Path(beam_path).resolve().parent.parent
    fetch = fetch or (lambda url: provider_mod.fetch_pr_state(root, url))
    now_dt, now_s = beam_mod.coerce_now(now)
    for tid in sorted(tickets):
        ticket = tickets[tid]
        if not isinstance(ticket, dict) or ticket.get("status") != "merged":
            continue
        pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
        if not pr.get("url") or pr.get("lateBugbot") or not orchestrator.bugbot_applies(ticket, cfg):
            continue
        head = str(pr.get("headSha") or "")
        if str(pr.get("bugbotSource") or "") == "provider" and pr.get("bugbot") == "pass" and head and pr.get("bugbotProviderSha") == head:
            pr["lateBugbot"] = "pass"
            continue
        try:
            snap = fetch(pr["url"]) or {}
        except Exception:
            snap = {}
        result = str(snap.get("bugbot") or "").strip().casefold()
        if result == "pass":
            pr["lateBugbot"] = "pass"
        elif result == "fail":
            findings = coerce_findings(snap.get("findings")) or ["Cursor Bugbot found issues on %s" % pr["url"]]
            new_id = _follow_up(data, ticket, findings, now_s)
            pr["lateBugbot"] = "followup"
            pr["lateFindings"] = findings
            pr["followUp"] = new_id
            out.append("bugbot: late %s %s %d finding(s) after the merge. Follow-up ticket %s queued." % (tid, pr["url"], len(findings), new_id))
            out.append("herald: %s Bugbot found %d issue(s) after the merge; %s fixes them." % (tid, len(findings), new_id))
        else:
            since = beam_mod.parse_ts(pr.get("mergedAt") or pr.get("lateSince"))
            if since is None and not result:
                pr["lateBugbot"] = "none"  # merged before 1.6.14 and Bugbot never ran on it
            elif since is None:
                pr["lateSince"] = now_s
            elif (now_dt - since).total_seconds() / 60.0 >= LATE_WINDOW_MINUTES and not result:
                pr["lateBugbot"] = "none"
    return out


def _follow_up(data: dict, ticket: dict, findings: list, now_s: str) -> str:
    """Queue a fix ticket for findings on a merged pull request. Returns its id."""
    tickets = data.setdefault("tickets", {})
    tid = str(ticket.get("id") or "T")
    n = 1
    while "%s-BB%d" % (tid, n) in tickets:
        n += 1
    new_id = "%s-BB%d" % (tid, n)
    locks = list(ticket.get("locks") or [])
    for finding in findings:
        path = finding.split(" ", 1)[0].split(":", 1)[0]
        if "/" in path and path not in locks and not any(path.startswith(lock.rstrip("/") + "/") for lock in locks if lock.endswith("/")):
            locks.append(path)
    url = (ticket.get("pr") or {}).get("url") or ""
    acs = [{"id": "AC-%d" % (i + 1), "kind": "fix", "text": "Bugbot finding fixed: %s" % finding} for i, finding in enumerate(findings)]
    acs.append({"id": "AC-%d" % (len(acs) + 1), "kind": "fix", "text": "Cursor Bugbot passes on this pull request with no findings"})
    tickets[new_id] = {
        "id": new_id,
        "status": "queued",
        "summary": "Fix Cursor Bugbot findings on %s (%s, merged)" % (url, tid),
        "size": "S",
        "hours": 1,
        "module": ticket.get("module"),
        "repo": ticket.get("repo"),
        "lane": ticket.get("lane"),
        "locks": locks,
        "deps": [],
        "gate": None,
        "autoMerge": bool(ticket.get("autoMerge", True)),
        "acs": acs,
        "plan": {"body": "Bugbot reviewed %s after it merged and found:\n%s\nFix each on a new branch from the base branch. "
                          "Do not change what the finding does not need." % (url, "\n".join("- " + f for f in findings))},
        "followUpOf": tid,
        "jiraKey": None,
        "jiraKeySource": "manual",
        "jiraMapping": "follow-up",
        "jira": {"startedAt": now_s, "inReviewAt": now_s, "status": None, "lastCommentAt": None},
        "pr": {},
        "attempts": 0,
        "alarm": None,
        "priority": "high",
        "updatedAt": now_s,
    }
    return new_id


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
    owned_root = False
    if beam_path is not None:
        data["_repoRoot"] = str(Path(beam_path).resolve().parent.parent)
        owned_root = True
    try:
        tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
        for ticket in list(tickets.values()):
            if not isinstance(ticket, dict):
                continue
            tid = ticket.get("id")
            _advance_one(data, ticket, provider.get(tid) or {}, lines, deferred, now=now)
        dispatch_fix_workers(data, lines)
        dispatch_replacements(data, lines)
        if deferred and beam_path is not None:
            _run_repairs(data, beam_path, lines, now)
        elif deferred:
            lines.append("alarm-repair: no beam")
        _refill(data, lines)
        _merge_one(data, provider, lines)
        _refill(data, lines)
        import watch

        lines.extend(watch.apply(data, provider=provider, beam_path=beam_path, now=now))
        import agents

        try:
            lines.extend(agents.retire(data, beam_path=beam_path, now=now))
            if not agents.spawns_open(data):
                lines.extend(agents.finish(data, beam_path=beam_path, now=now))
        except Exception as exc:
            # Archiving is housekeeping. It never costs the pass its result.
            lines.append("cleanup: skipped (%s)" % exc)
        return lines
    finally:
        data.pop("_fixDefer", None)
        if owned_root:
            data.pop("_repoRoot", None)
        data.pop("_live", None)
