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


def mark_shuttle(ticket: dict, step: str, data: Optional[dict] = None) -> None:
    shuttle = _shuttle(ticket)
    shuttle["pending"] = True
    shuttle["step"] = step
    shuttle["hadPr"] = bool((ticket.get("pr") or {}).get("url"))
    shuttle.pop("returned", None)
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
    for key, sha_field in (("rollup", "rollupSha"), ("bugbot", "bugbotSha"), ("ci", "ciSha"), ("check", "checkSha")):
        if key in snap and snap[key] is not None:
            pr[key] = snap[key]
            if new_sha:
                pr[sha_field] = new_sha
    if "checkLog" in snap and snap["checkLog"] is not None:
        pr["checkLog"] = snap["checkLog"]
    if "findings" in snap:
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
    ticket["status"] = "fix"
    ticket["phase"] = "fixing"
    mark_shuttle(ticket, "fix", data)
    lines.extend(_start_lines(data, ticket, "fix", rebase_detail(ticket)))
    lines.append("fixer: %s rebase" % ticket.get("id"))


def start_ci(data: dict, ticket: dict, lines: list) -> None:
    """The head has no check runs past the grace period. Ask for the workflow now."""
    tid = ticket.get("id")
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
    """Start fix Shuttles up to the fix-worker cap, highest priority first.

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
    """Start or resume. A new parent session. Earlier subagent Shuttles are dead.

    VM mode keeps a Shuttle with a fresh heartbeat or a start that is not
    stale. Every released ticket loses its slot and waits for a replacement.
    This does not launch anything.
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

    maxInProgress does not apply. A fix replacement also stays inside
    maxFixWorkers. Past maxRecoveries the ticket alarms and does not start.
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
    if count >= cap:
        ticket["needsReplacement"] = False
        clear_shuttle(ticket)
        ticket["status"] = "alarm"
        ticket["alarm"] = "worker-died"
        lines.append("shuttle: alarm %s worker-died" % tid)
        lines.append("herald: %s worker died. Recovery cap reached." % tid)
        return
    number = count + 1
    new_agent, number = beam_mod._unique_id("shuttle-%s" % tid, number, ticket.get("agent"))
    prev = ticket.get("status")
    if prev != "recovering":
        ticket["recoveryPriorStatus"] = prev
    ticket["status"] = "recovering"
    ticket["agent"] = new_agent
    ticket["recoveries"] = number
    stamp = beam_mod.utcnow()
    ticket["recoveredAt"] = stamp
    ticket["workerStartedAt"] = stamp
    ticket.pop("lastSeenAt", None)
    mark_shuttle(ticket, step, data)
    branch = ticket.get("branch") or ""
    lines.append("shuttle: replace %s agent=%s branch=%s" % (tid, new_agent, branch))
    lines.append("herald: %s worker died. A new Shuttle started." % tid)
    lines.extend(_start_lines(data, ticket, step, "replace"))


def cold_start(beam_path) -> list:
    """Start or resume. Fill live slots: fixes, then dead Shuttle replacements.

    A Shuttle already out keeps its slot. Room left under the caps is filled
    immediately. maxInProgress does not hold either of these.
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
    """One red round. Past maxFixAttempts (default 5) the ticket parks and does not start a Shuttle."""
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
    mark_shuttle(ticket, "fix", data)
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
    mark_shuttle(ticket, "restart", data)
    lines.extend(_start_lines(data, ticket, "restart"))
    if herald:
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


def _follow(data: dict, ticket: dict, lines: list) -> None:
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
            _request_bugbot(ticket, lines)
            return
        if _conflicted(ticket):
            start_rebase(data, ticket, lines)
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
    _ensure_acs(data, ticket)
    if _recover_alarm(data, ticket, lines, deferred):
        return
    _ensure_phase(ticket, data)
    if not ticket.get("phase"):
        return
    if pending(ticket) and not orchestrator.shuttle_is_live(ticket, data, now=now):
        _release_slot(ticket)
    elif pending(ticket):
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
        return lines
    finally:
        data.pop("_fixDefer", None)
        if owned_root:
            data.pop("_repoRoot", None)
