#!/usr/bin/env python3
"""Per-ticket status the parent can read after the Agent is gone.

The live beam stays on the orchestrator. A ticket Agent does not commit
updates to it. The launch snapshot of `.warp/beam.json` on the ticket branch
is claim-in: the Agent reads it and leaves it alone.

Status comes out in that ticket's own directory. Only that Agent writes it:

  .warp/tickets/<id>/state.json   current state
  .warp/tickets/<id>/log.jsonl    one line per event

A worktree subagent writes that directory straight into the parent checkout
by absolute path and does not push it. The parent reads the files where they
are. Nothing pushes that state around. An optional launch: agent worker may
still commit and push only that directory.

Every tick reads a worktree ticket's directory from the parent checkout. It
fetches an agent-mode branch and reads only that ticket's directory. It
patches the live beam. It does not copy a branch beam. A second tick applies
nothing again. Paused and stopped runs do not fetch.

  python3 scripts/ticket_state.py append --id T-1 --state planning --root . --push
  python3 scripts/ticket_state.py append --id T-1 --state heartbeat --push
  python3 scripts/ticket_state.py append --id T-1 --state check-red --name "make ci" --error "boom" --push
  python3 scripts/ticket_state.py append --id T-1 --state pr-opened --pr https://github.com/org/repo/pull/1 --push
  python3 scripts/ticket_state.py append --id T-1 --state lock-escape --escaped src/extra --alarm lock-escape --agent shuttle-1 --at 2026-10-06T12:00:00Z --push
  python3 scripts/ticket_state.py observe --beam .warp/beam.json --root .

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beam  # noqa: E402
import orchestrator  # noqa: E402

HELP = __doc__

# Log states. heartbeat updates the clock and does not replace the work state
# stored on state.json. The others are the state the parent heralds.
STATES = (
    "started",
    "heartbeat",
    "planning",
    "coding",
    "review",
    "bugbot",
    "fix",
    "pr-opened",
    "check-red",
    "check-green",
    "lock-escape",
    "alarm",
    "failed",
    "done",
    "review-pass",
    "review-fail",
    "acceptance",
)
# A Bugbot review recorded from the ticket folder: by the Shuttle (bugbotTrigger: shuttle)
# or by a person (bugbot.py record). It carries the head sha it reviewed.
REVIEW_STATES = {"review-pass", "review-fail"}
_WORK = {
    "planning": "planning",
    "coding": "coding",
    "review": "review",
    "bugbot": "bugbot_running",
    "fix": "fix",
}
# Not in flight. Nothing to fetch.
_QUIET = {"queued", "merged", "done", "skipped", "parked"}
# A red check means the coding turn is over. fix is already the send-back
# status, so it is not promoted again.
_PROMOTE = {"claimed", "planning", "coding", "recovering"}


def _check_id(tid: str) -> str:
    text = str(tid or "").strip()
    if not text or "/" in text or text in {".", ".."} or ".." in text:
        sys.exit("refuse: bad ticket id %r" % tid)
    return text


def ticket_dir(root: Path, tid: str) -> Path:
    """The only directory this ticket's Agent writes."""
    return Path(root) / ".warp" / "tickets" / _check_id(tid)


def _git(root: Path, *args: str, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "maintenance.auto=false", *args],
        capture_output=True,
        text=True,
        input=input_text,
    )


def _has_origin(root: Path) -> bool:
    proc = _git(root, "remote")
    return proc.returncode == 0 and "origin" in proc.stdout.split()


def _repo_root(beam_path: Path) -> Optional[Path]:
    start = beam_path.resolve().parent
    if start.name == ".warp":
        start = start.parent
    proc = _git(start, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    return Path(text) if text else None


def _base_branch(root: Path) -> str:
    config = root / ".warp" / "config.yaml"
    if config.is_file():
        for line in config.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("baseBranch:"):
                name = stripped.split(":", 1)[1].split("#", 1)[0].strip().strip("\"'")
                if name:
                    return name
    return "main"


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _blank_state(tid: str) -> dict:
    return {
        "id": tid,
        "state": "",
        "startedAt": "",
        "updatedAt": "",
        "heartbeatAt": "",
        "pr": {"url": ""},
        "check": {"result": "", "name": "", "log": ""},
        "error": "",
        "alarm": "",
        "escaped": [],
        "agent": "",
    }


def _read_log(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _line_key(row: dict) -> str:
    blob = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def _state_key(state: dict) -> str:
    keep = {
        "state": state.get("state"),
        "heartbeatAt": state.get("heartbeatAt"),
        "check": state.get("check"),
        "error": state.get("error"),
        "alarm": state.get("alarm"),
        "escaped": state.get("escaped"),
        "pr": state.get("pr"),
        "startedAt": state.get("startedAt"),
        "review": state.get("review"),
        "acResults": state.get("acResults"),
    }
    blob = json.dumps(keep, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def _remote(ticket: dict) -> dict:
    raw = ticket.get("remote")
    if not isinstance(raw, dict):
        raw = {}
        ticket["remote"] = raw
    seen = raw.get("seen")
    if not isinstance(seen, list):
        raw["seen"] = []
    return raw


def _newer(candidate, current) -> bool:
    left = beam.parse_ts(candidate)
    if left is None:
        return False
    right = beam.parse_ts(current)
    if right is None:
        return True
    return left > right


def _pr(ticket: dict) -> dict:
    pr = ticket.get("pr")
    if not isinstance(pr, dict):
        pr = {}
        ticket["pr"] = pr
    return pr


def _yaml(beam_path: Path) -> str:
    path = beam.config_yaml_path(beam_path)
    if not path.is_file():
        return ""
    try:
        return path.read_text()
    except OSError:
        return ""


def _herald(lines: list, seen: set, tid: str, state: str) -> None:
    key = "%s %s" % (tid, state)
    if key in seen:
        return
    seen.add(key)
    lines.append("herald: %s" % key)


def _note_status(beam_path: Path, ticket: dict, prev: str, now: str) -> None:
    if ticket.get("status") == prev:
        return
    beam.append_ticket_event(
        beam_path,
        ticket,
        {"at": now, "type": "status", "from": prev, "to": ticket.get("status")},
    )


def _apply_heartbeat(ticket: dict, stamp: str) -> bool:
    """Copy a directory heartbeat onto lastSeenAt. Never move the clock backward.

    A send-back or a recovery clears lastSeenAt and stamps workerStartedAt.
    An older heartbeat still sitting in the directory must not make that new
    worker look dead in the same tick.
    """
    if not stamp:
        return False
    anchor = ticket.get("workerStartedAt")
    if not ticket.get("lastSeenAt") and anchor and _newer(anchor, stamp):
        return False
    if not _newer(stamp, ticket.get("lastSeenAt")):
        return False
    ticket["lastSeenAt"] = stamp
    return True


def _url(value) -> str:
    if isinstance(value, dict):
        return str(value.get("url") or "").strip()
    return str(value or "").strip()


def _escaped(value) -> list:
    import alarm_repair

    if isinstance(value, str):
        value = [value]
    return alarm_repair.normalize_paths(value if isinstance(value, list) else [])


def uncovered(ticket: dict, paths) -> list:
    """Escaped paths the ticket's locks do not already cover (a repair may have widened them)."""
    import alarm_repair

    locks = [lock for lock in (ticket.get("locks") or []) if isinstance(lock, str) and lock.strip()]
    return [path for path in _escaped(paths) if not alarm_repair._already_locked(locks, path)]


def _apply_lock_escape(beam_path: Path, ticket: dict, paths: list, now: str) -> bool:
    paths = _escaped(paths)
    if paths and not uncovered(ticket, paths):
        # Every path is inside the locks now. Nothing escaped.
        return False
    same = (
        ticket.get("status") == "alarm"
        and ticket.get("alarm") == "lock-escape"
        and _escaped(ticket.get("escaped")) == paths
    )
    if same:
        return False
    prev = ticket.get("status")
    ticket["status"] = "alarm"
    ticket["alarm"] = "lock-escape"
    ticket["escaped"] = paths
    ticket["updatedAt"] = now
    _note_status(beam_path, ticket, prev, now)
    if ticket.get("alarm"):
        beam.append_ticket_event(beam_path, ticket, {"at": now, "type": "alarm", "value": "lock-escape"})
    return True


def _store_check_row(pr: dict, name: str, result: str, log: str) -> None:
    conclusion = "failure" if result == "red" else "success"
    rows = pr.get("checks")
    if not isinstance(rows, list):
        rows = []
        pr["checks"] = rows
    for row in rows:
        if isinstance(row, dict) and orchestrator.norm_check_name(row.get("name")) == orchestrator.norm_check_name(name):
            row["name"] = name
            row["conclusion"] = conclusion
            row["log"] = log
            return
    rows.append({"name": name, "conclusion": conclusion, "log": log})


def _apply_check(beam_path: Path, data: dict, ticket: dict, result: str, name: str, log: str, now: str) -> list:
    """Record check-red or check-green. Red uses the existing send-back path.

    claimed, planning, coding, or recovering is moved to review first so a
    dead Agent's red check is not dropped. fix stays fix. Past maxFixAttempts (default 5) the ticket parks.
    """
    result = "red" if str(result).strip().casefold() == "red" else "green"
    name = (name or "").strip() or "make ci"
    log = (log or "").strip() or ("%s %s" % (name, result))
    pr = _pr(ticket)
    prev_status = ticket.get("status")
    if result == "green" and pr.get("check") == "green" and prev_status == ticket.get("status"):
        if not name or pr.get("checkName") in {None, "", name}:
            return []
    if result == "red" and prev_status in _PROMOTE:
        ticket["status"] = "review"
        _note_status(beam_path, ticket, prev_status, now)
    _store_check_row(pr, name, result, log)
    if "bugbot" in name.casefold():
        pr["bugbot"] = "fail" if result == "red" else "pass"
    if orchestrator.is_make_ci(name) or not pr.get("checkName"):
        pr["checkName"] = name
    pr["check"] = result
    if result == "red":
        pr["checkLog"] = log
        pr["rollup"] = "red"
    else:
        pr.pop("checkLog", None)
        if orchestrator.provider_rollup(ticket) != "red":
            pr["rollup"] = "green"
    ticket["updatedAt"] = now
    if result != "red":
        return []
    sent = orchestrator.send_back_red_checks(data, _yaml(beam_path), only_id=ticket.get("id"))
    for action in sent:
        if action.get("outcome") == "fix":
            # The send-back start line is the one launch. Do not also declare
            # the worker dead off the heartbeat it just stopped writing.
            ticket["workerStartedAt"] = now
            ticket.pop("lastSeenAt", None)
    return orchestrator.format_send_back(sent, data)


# Steps whose Shuttle works on a pull request that may already exist. Their return cannot be
# read from "a pull request appeared", so the folder says it.
_RETURN_STEPS = {"implement", "restart", "fix", "repair", "review", "verify"}
_RETURN_STATES = {"done", "check-green", "check-red", "review-pass", "review-fail"}


def note_shuttle_return(ticket: dict, event: dict, stamp: str, lines: Optional[list] = None) -> bool:
    """The Shuttle that is out wrote its last state: it has returned. True when that is new.

    `done` is the Shuttle's explicit last line. A check result or a recorded review from the
    same agent, after this Shuttle started, also ends its step: the step's work is pushed and
    checked. Without this, a Shuttle on an existing pull request (restart, fix, repair) on a
    cloud runner was never seen to return; it held its slot until its heartbeat went stale and
    was then called dead and replaced.
    """
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else None
    if not shuttle or not shuttle.get("pending") or shuttle.get("returned"):
        return False
    state = str(event.get("state") or "")
    step = str(shuttle.get("step") or "")
    if state not in _RETURN_STATES or (state != "done" and step not in _RETURN_STEPS):
        return False
    if state in {"review-pass", "review-fail"} and step != "review":
        # The working Shuttle records its review before it returns its own line; "done" or the
        # check ends that step. Only a review Shuttle's job ends with the review.
        return False
    agent = str(event.get("agent") or "")
    current = str(ticket.get("agent") or "")
    if agent and current and agent != current and not current.startswith("worktree:"):
        return False
    since = shuttle.get("startedAt") or ticket.get("workerStartedAt")
    started = beam.parse_ts(str(since)) if since else None
    at = beam.parse_ts(str(event.get("at") or stamp))
    if started is not None and at is not None and at < started:
        return False  # a line from an earlier Shuttle on this ticket
    shuttle["returned"] = True
    shuttle["returnedAt"] = str(event.get("at") or stamp)
    _pr(ticket)["shuttleReturned"] = True
    if lines is not None:
        lines.append("shuttle: returned %s step=%s (%s)" % (ticket.get("id"), step or "?", state))
    return True


def _apply_event(beam_path: Path, data: dict, ticket: dict, event: dict, now: str, heralded: set, lines: list) -> bool:
    """Patch one log line onto this ticket. Returns True when the beam changed."""
    state = str(event.get("state") or "").strip()
    if state not in STATES:
        return False
    noted = _apply_acs(ticket, event, lines, heralded)
    returned = note_shuttle_return(ticket, event, str(event.get("at") or now), lines)
    return _apply_event_state(beam_path, data, ticket, event, now, heralded, lines, state) or returned or noted


def _apply_acs(ticket: dict, event: dict, lines: list, heralded: set) -> bool:
    """Acceptance results on a folder line (`--ac AC-1=pass`), stored for the head commit."""
    raw = event.get("acs")
    if not isinstance(raw, dict) or not raw:
        return False
    pr = _pr(ticket)
    head = str(pr.get("headSha") or "")
    current = pr.get("acResults") if isinstance(pr.get("acResults"), dict) and str(pr.get("acSha") or head) == head else {}
    merged = dict(current)
    merged.update({str(k): str(v) for k, v in raw.items()})
    if merged == pr.get("acResults") and str(pr.get("acSha") or "") == head:
        return False
    pr["acResults"] = merged
    pr["acSha"] = head
    tid = ticket.get("id")
    failed = sorted(k for k, v in merged.items() if str(v).casefold() not in {"pass", "passed", "ok", "green"})
    key = (tid, "acs", tuple(sorted(merged.items())))
    if key not in heralded:
        heralded.add(key)
        if failed:
            lines.append("herald: %s acceptance: %s failed." % (tid, ", ".join(failed)))
        else:
            lines.append("herald: %s acceptance: %d recorded, all pass." % (tid, len(merged)))
    return True


def _apply_event_state(beam_path: Path, data: dict, ticket: dict, event: dict, now: str, heralded: set, lines: list, state: str) -> bool:
    tid = ticket.get("id")
    stamp = str(event.get("at") or now)
    changed = False
    if event.get("agent") and not ticket.get("agent"):
        ticket["agent"] = event.get("agent")
        changed = True
    if state == "heartbeat":
        if _apply_heartbeat(ticket, stamp):
            ticket["updatedAt"] = stamp
            return True
        return changed
    if _apply_heartbeat(ticket, stamp):
        # Any line the Shuttle writes (planning, coding, a check, a review) shows it is alive.
        changed = True
    if state == "started":
        remote = _remote(ticket)
        if not remote.get("startedAt"):
            remote["startedAt"] = stamp
            remote["state"] = "started"
            changed = True
            _herald(lines, heralded, tid, "started")
        return changed
    if state in _WORK:
        prev = ticket.get("status")
        wanted = _WORK[state]
        if prev != wanted:
            ticket["status"] = wanted
            ticket["updatedAt"] = stamp
            _note_status(beam_path, ticket, prev, stamp)
            _remote(ticket)["state"] = state
            _herald(lines, heralded, tid, state)
            return True
        return changed
    if state == "pr-opened":
        url = _url(event.get("pr") or event.get("url"))
        before = _pr(ticket).get("url")
        orchestrator.note_pr_opened(ticket, url, stamp)
        if url and url != before:
            _herald(lines, heralded, tid, "pr-opened")
            ticket["updatedAt"] = stamp
            return True
        return changed
    if state in {"check-red", "check-green"}:
        result = "red" if state == "check-red" else "green"
        name = str(event.get("name") or event.get("check") or "").strip()
        before = (ticket.get("status"), _pr(ticket).get("check"), _pr(ticket).get("bugbot"), ticket.get("attempts"))
        extra = _apply_check(beam_path, data, ticket, result, name, str(event.get("error") or ""), now)
        after = (ticket.get("status"), _pr(ticket).get("check"), _pr(ticket).get("bugbot"), ticket.get("attempts"))
        if extra or after != before:
            _remote(ticket)["state"] = state
            _herald(lines, heralded, tid, state)
            lines.extend(extra)
            return True
        return changed
    if state in REVIEW_STATES:
        import bugbot

        return bugbot.apply_review(beam_path, ticket, event, stamp, lines, heralded) or changed
    if state == "lock-escape":
        if _apply_lock_escape(beam_path, ticket, event.get("escaped"), now):
            _remote(ticket)["state"] = "lock-escape"
            _herald(lines, heralded, tid, "lock-escape")
            return True
        return changed
    if state in {"alarm", "failed"}:
        reason = str(event.get("alarm") or "").strip()
        if not reason:
            reason = "failed" if state == "failed" else (str(event.get("error") or "").strip() or "alarm")
        prev = ticket.get("status")
        same = prev == "alarm" and ticket.get("alarm") == reason
        if same:
            return changed
        ticket["status"] = "alarm"
        ticket["alarm"] = reason
        ticket["updatedAt"] = stamp
        if event.get("error"):
            _remote(ticket)["error"] = event.get("error")
        _note_status(beam_path, ticket, prev, stamp)
        beam.append_ticket_event(beam_path, ticket, {"at": stamp, "type": "alarm", "value": reason})
        _remote(ticket)["state"] = state
        _herald(lines, heralded, tid, state)
        return True
    if state == "done":
        remote = _remote(ticket)
        if remote.get("state") == "done":
            return changed
        remote["state"] = "done"
        pr = _pr(ticket)
        pr["agentFinished"] = True
        prev = ticket.get("status")
        if prev in beam.SHUTTLE_WORK and prev != "fix":
            ticket["status"] = "review"
            _note_status(beam_path, ticket, prev, stamp)
        ticket["updatedAt"] = stamp
        _herald(lines, heralded, tid, "done")
        return True
    return changed


# States that keep the alarm fields already in state.json. Every other state clears them.
STICKY_STATES = {"heartbeat", "lock-escape", "alarm", "failed"}


def _event_from_state(state: dict) -> Optional[dict]:
    """One snapshot event when the directory has current state and the log was short."""
    name = str(state.get("state") or "").strip()
    if name not in STATES or name == "heartbeat":
        return None
    check = state.get("check") if isinstance(state.get("check"), dict) else {}
    event = {
        "at": state.get("updatedAt") or state.get("startedAt") or "",
        "state": name,
        "error": state.get("error") or check.get("log") or "",
        "id": state.get("id"),
        "escaped": state.get("escaped"),
        "alarm": state.get("alarm"),
        "name": check.get("name") or "",
        "pr": state.get("pr"),
        "agent": state.get("agent") or "",
    }
    if name == "check-red" or (check.get("result") == "red" and name not in {"check-red", "check-green"}):
        event["state"] = "check-red"
    elif name == "check-green" or check.get("result") == "green":
        event["state"] = "check-green"
    if isinstance(state.get("acResults"), dict) and state.get("acResults"):
        event["acs"] = dict(state["acResults"])
    if name in REVIEW_STATES:
        review = state.get("review") if isinstance(state.get("review"), dict) else {}
        event["sha"] = review.get("sha") or ""
        event["findings"] = review.get("findings") or []
        event["source"] = review.get("source") or "shuttle"
    if name == "lock-escape" or (name in STICKY_STATES and state.get("alarm") == "lock-escape"):
        # A later state (review, pr-opened, check-green) with a leftover alarm
        # field is that later state, not a new lock-escape.
        event["state"] = "lock-escape"
        event["escaped"] = state.get("escaped")
    return event


def _directory_has_check(lines: list, state: dict, tid: str) -> bool:
    for row in lines:
        if not isinstance(row, dict):
            continue
        owner = str(row.get("id") or tid)
        if owner != tid:
            continue
        if row.get("state") in {"check-red", "check-green"}:
            return True
    check = state.get("check") if isinstance(state.get("check"), dict) else {}
    return bool(str(check.get("result") or "").strip())


def _provider_key(checks: list) -> str:
    blob = json.dumps(checks, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def _apply_provider(beam_path: Path, data: dict, ticket: dict, checks: list, now: str, heralded: set, lines: list) -> bool:
    if not checks:
        return False
    remote = _remote(ticket)
    key = _provider_key(checks)
    if remote.get("providerKey") == key:
        return False
    pr = _pr(ticket)
    rollup = ""
    try:
        import provider

        rollup = provider.interpret_rollup(checks)
    except Exception:
        rollup = ""
    changed = False
    red_name = ""
    red_log = ""
    saw_make = False
    for item in checks:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("context") or "").strip()
        kind = orchestrator.conclusion_kind(item.get("conclusion") or item.get("state"))
        log = str(item.get("log") or item.get("output") or "").strip()
        if not name or kind not in {"red", "green"}:
            continue
        _store_check_row(pr, name, kind, log or ("%s %s" % (name, kind)))
        if "bugbot" in name.casefold():
            pr["bugbot"] = "fail" if kind == "red" else "pass"
            changed = True
        if orchestrator.is_make_ci(name):
            saw_make = True
            if kind == "red":
                red_name = name
                red_log = log or "make ci red"
            changed = True
    if rollup:
        pr["rollup"] = rollup
        changed = True
    if rollup == "red" and not red_name:
        command = orchestrator.resolve_check_command(data.get("config") or {}, _yaml(beam_path))
        if orchestrator.is_make_ci(command) or orchestrator.is_make_ci(pr.get("checkName")):
            red_name = "make ci"
            red_log = red_log or "make ci red"
    if red_name:
        extra = _apply_check(beam_path, data, ticket, "red", red_name, red_log or "make ci red", now)
        _herald(lines, heralded, ticket.get("id"), "check-red")
        lines.extend(extra)
        changed = True
    elif rollup == "red":
        _herald(lines, heralded, ticket.get("id"), "check-red")
        changed = True
    elif rollup == "green" or (saw_make and not red_name):
        if saw_make:
            pr["check"] = "green"
            pr["checkName"] = pr.get("checkName") or "make ci"
            pr.pop("checkLog", None)
        _herald(lines, heralded, ticket.get("id"), "check-green")
        changed = True
    remote["providerKey"] = key
    if changed:
        ticket["updatedAt"] = now
    return changed


def _owns(row: dict, tid: str) -> bool:
    owner = row.get("id")
    if owner in (None, ""):
        return True
    return str(owner) == str(tid)


def apply_directory(
    beam_path: Path,
    data: dict,
    ticket: dict,
    state: Optional[dict],
    lines_in: Optional[list],
    now: str,
    heralded: set,
    out: list,
    checks: Optional[list] = None,
) -> bool:
    """Patch one ticket from its directory. Ignore every other ticket's lines.

    Does not read a beam file. The caller passes only this ticket's state and log.
    """
    tid = str(ticket.get("id") or "")
    state = state if isinstance(state, dict) else {}
    if state.get("id") not in (None, "", tid):
        state = {}
    remote = _remote(ticket)
    seen = set(str(item) for item in remote.get("seen") or [])
    changed = False
    for row in lines_in or []:
        if not isinstance(row, dict):
            continue
        key = _line_key(row)
        if key in seen:
            continue
        seen.add(key)
        if not _owns(row, tid):
            continue
        if _apply_event(beam_path, data, ticket, row, now, heralded, out):
            changed = True
    remote["seen"] = sorted(seen)
    if state.get("cloudAgent"):
        try:
            import agents

            owner = str(state.get("agent") or ticket.get("agent") or "")
            agents.bind_cloud(data, owner, tid, beam_path, cloud=str(state.get("cloudAgent")))
        except Exception:
            pass
    snap_key = _state_key(state) if state else ""
    if state and snap_key and snap_key != remote.get("stateKey"):
        if _apply_heartbeat(ticket, str(state.get("heartbeatAt") or "")):
            changed = True
        event = _event_from_state(state)
        if event and _owns(event, tid):
            if _apply_event(beam_path, data, ticket, event, now, heralded, out):
                changed = True
        elif state.get("alarm") == "lock-escape":
            if _apply_lock_escape(beam_path, ticket, state.get("escaped"), now):
                _herald(out, heralded, tid, "lock-escape")
                changed = True
        remote["stateKey"] = snap_key
    if checks is not None and not _directory_has_check(lines_in or [], state, tid):
        if _apply_provider(beam_path, data, ticket, checks, now, heralded, out):
            changed = True
    return changed


def _default_fetch(root: Path, branch: str) -> bool:
    """Fetch the ticket branch. Do not rebase it and do not check it out."""
    if not _has_origin(root):
        return True
    proc = _git(root, "fetch", "origin", branch)
    return proc.returncode == 0


def _show(root: Path, ref: str, rel: str) -> Optional[str]:
    proc = _git(root, "show", "%s:%s" % (ref, rel))
    if proc.returncode != 0:
        return None
    return proc.stdout


def _default_read(root: Path, branch: str, tid: str):
    ref = "origin/%s" % branch if _has_origin(root) else branch
    import runs

    run = runs.current(root)
    rel = ".warp/tickets/%s" % tid
    state_text = log_text = None
    if run:
        state_text = _show(root, ref, runs.git_rel(run, rel + "/state.json"))
        log_text = _show(root, ref, runs.git_rel(run, rel + "/log.jsonl"))
    if state_text is None and log_text is None:
        state_text = _show(root, ref, rel + "/state.json")
        log_text = _show(root, ref, rel + "/log.jsonl")
    state = None
    if state_text:
        try:
            parsed = json.loads(state_text)
        except json.JSONDecodeError:
            parsed = None
        state = parsed if isinstance(parsed, dict) else None
    rows = []
    if log_text:
        for line in log_text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return state, rows


def _default_checks(root: Path, ticket: dict):
    pr = _pr(ticket).get("url")
    if not pr:
        return None
    text = str(pr)
    if "github.com" not in text and not text.isdigit():
        return None
    try:
        import provider
    except Exception:
        return None
    return provider.fetch_check_rows(root, text)


def _read_local_dir(root: Path, tid: str):
    """state.json and log.jsonl already on the parent checkout."""
    folder = ticket_dir(root, tid)
    state_path = folder / "state.json"
    log_path = folder / "log.jsonl"
    if not state_path.is_file() and not log_path.is_file():
        return None, []
    state = None
    if state_path.is_file():
        try:
            parsed = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            parsed = None
        state = parsed if isinstance(parsed, dict) else None
    rows = _read_log(log_path) if log_path.is_file() else []
    return state, rows


_ACTIVITY_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target", ".warp"}
_ACTIVITY_CAP = 5000


def _latest_mtime(folder: Path, cap: int = _ACTIVITY_CAP) -> float:
    """The newest file change under folder, skipping build and dependency trees. 0 when there is none."""
    import os

    newest = 0.0
    seen = 0
    stack = [str(folder)]
    while stack and seen < cap:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in _ACTIVITY_SKIP:
                        stack.append(entry.path)
                    continue
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
            except OSError:
                continue
            if seen >= cap:
                break
    return newest


def _git_dir_activity(worktree: Path) -> float:
    """A commit, a stage, or a checkout in the worktree: its git dir's HEAD, index, and HEAD log."""
    marker = worktree / ".git"
    gitdir = marker
    try:
        if marker.is_file():
            text = marker.read_text().strip()
            if text.startswith("gitdir:"):
                gitdir = Path(text.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = (worktree / gitdir).resolve()
    except OSError:
        return 0.0
    newest = 0.0
    for name in ("HEAD", "index", "logs/HEAD", "ORIG_HEAD", "FETCH_HEAD"):
        try:
            newest = max(newest, (gitdir / name).stat().st_mtime)
        except OSError:
            continue
    return newest


def worktree_activity(root: Path, ticket: dict) -> str:
    """When the Shuttle in this ticket's worktree last changed anything, as a UTC stamp. Empty when unknown.

    A Shuttle that edits, builds, or commits for longer than staleMinutes without writing a heartbeat
    line is still working. Its worktree shows it. Only a worktree on this disk is read.
    """
    from datetime import datetime, timezone

    raw = str(ticket.get("worktree") or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_absolute():
        path = Path(root) / path
    if not path.is_dir():
        return ""
    newest = max(_latest_mtime(path), _git_dir_activity(path))
    if newest <= 0:
        return ""
    return datetime.fromtimestamp(newest, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply_activity(root: Path, ticket: dict, now_s: str) -> bool:
    """Count worktree activity since this Shuttle started as its heartbeat. Never a time in the future."""
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    if not shuttle.get("pending") or shuttle.get("returned"):
        return False
    stamp = worktree_activity(root, ticket)
    if not stamp:
        return False
    if _newer(stamp, now_s):
        stamp = now_s
    began = shuttle.get("startedAt") or ticket.get("workerStartedAt")
    if began and not _newer(stamp, began):
        # Changes from before this Shuttle started are the previous one's.
        return False
    return _apply_heartbeat(ticket, stamp)


def _local_checkout(ticket: dict) -> bool:
    """Worktree tickets write state on the parent disk. A VM ticket does not."""
    if str(ticket.get("checkout") or "") == "subagent-vm" and ticket.get("isolation") != "fallback":
        return False
    if str(ticket.get("checkout") or "") == "worktree":
        return True
    if ticket.get("isolation") == "fallback":
        return True
    return bool(ticket.get("worktree"))


def _in_flight(ticket: dict) -> bool:
    if not isinstance(ticket, dict):
        return False
    if ticket.get("status") in _QUIET:
        return False
    return bool(ticket.get("branch"))


def observe_locked(
    beam_path: Path,
    now: Optional[str] = None,
    fetch: Optional[Callable] = None,
    reader: Optional[Callable] = None,
    provider_checks: Optional[Callable] = None,
    require_live: bool = True,
) -> list:
    """Fetch in-flight branches and patch the live beam from each ticket directory.

    Paused and stopped runs return before any fetch when `require_live` is true.
    `/warp-update-state` passes false so a pause still picks up ticket folders.
    A missing git repo returns nothing, so a beam that is not in a checkout is
    left to the heartbeat already stored on it.
    """
    beam_path = Path(beam_path)
    data = beam.load_json(beam_path)
    _now_dt, now_s = beam.coerce_now(now)
    if require_live:
        live, why = beam._run_live(data)
        if not live:
            return ["observe: skipped (%s)" % why]
    root = _repo_root(beam_path)
    if root is None and fetch is None and reader is None:
        return []
    if root is None:
        root = beam_path.parent.parent if beam_path.parent.name == ".warp" else beam_path.parent
    fetch_fn = fetch or _default_fetch
    read_fn = reader or _default_read
    checks_fn = provider_checks if provider_checks is not None else _default_checks
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    lines: list = []
    heralded: set = set()
    changed = False
    for tid in sorted(tickets):
        ticket = tickets[tid]
        if not _in_flight(ticket):
            continue
        branch = str(ticket.get("branch") or "")
        state, log_rows = None, []
        local = _local_checkout(ticket)
        if local:
            try:
                state, log_rows = _read_local_dir(root, tid)
            except Exception:
                lines.append("observe: fetch failed %s" % tid)
                continue
        remote = (not local) or (ticket.get("isolation") == "fallback" and state is None and not log_rows)
        if remote:
            if fetch_fn is not None and not fetch_fn(root, branch):
                lines.append("observe: fetch failed %s" % tid)
                continue
            try:
                state, log_rows = read_fn(root, branch, tid)
            except Exception:
                lines.append("observe: fetch failed %s" % tid)
                continue
        try:
            checks = checks_fn(root, ticket) if checks_fn else None
        except Exception:
            checks = None
        if apply_directory(beam_path, data, ticket, state, log_rows, now_s, heralded, lines, checks):
            changed = True
        if local and _apply_activity(root, ticket, now_s):
            changed = True
    if changed:
        data["metrics"] = beam.metrics(data)
        beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
        for line in lines:
            if line.startswith("herald: "):
                beam.journal(beam_path, {"type": "remote", "line": line[len("herald: ") :]})
        herald = [line[len("herald: ") :] for line in lines if line.startswith("herald: ")]
        if herald:
            beam.atomic_write(
                beam_path.parent / "remote.json",
                json.dumps({"at": now_s, "lines": herald}, indent=2) + "\n",
            )
    return lines


def observe(beam_path: Path, **kwargs) -> list:
    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return ["observe: no beam at %s" % beam_path]
    lock_path = beam_path.parent / ".watchdog.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    with lock_path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return observe_locked(beam_path, **kwargs)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def append_status(
    root: Path,
    tid: str,
    state: str,
    when: Optional[str] = None,
    error: str = "",
    escaped: Optional[list] = None,
    pr: str = "",
    name: str = "",
    alarm: str = "",
    agent: str = "",
    push: bool = False,
    findings: Optional[list] = None,
    sha: str = "",
    source: str = "",
    acs: Optional[dict] = None,
) -> int:
    """Write this ticket's directory only. Does not touch `.warp/beam.json`."""
    tid = _check_id(tid)
    if state not in STATES:
        print("refuse: unknown state %s" % state)
        return 2
    root = Path(root).resolve()
    folder = ticket_dir(root, tid)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = when or beam.utcnow()
    current = _load_state(folder / "state.json")
    body = _blank_state(tid)
    body.update(current if isinstance(current, dict) else {})
    body["id"] = tid
    if not body.get("startedAt"):
        body["startedAt"] = stamp
    body["updatedAt"] = stamp
    if agent:
        body["agent"] = agent
    cloud = _cloud_agent() if push else ""
    if cloud:
        # A Shuttle that pushes its folder is on its own VM. The parent binds
        # this id to the Shuttle's registry row, so pause and stop can cancel
        # it by id. A worktree Shuttle shares the parent's VM and reports none.
        body["cloudAgent"] = cloud
    if state == "heartbeat":
        body["heartbeatAt"] = stamp
        if not body.get("state"):
            body["state"] = "started"
    else:
        body["state"] = state
    if state not in STICKY_STATES:
        # A newer progress state: the alarm, the escaped paths and the error
        # belonged to the event that set them. Left here they raise it again.
        body["alarm"] = ""
        body["escaped"] = []
        body["error"] = ""
    if error:
        body["error"] = error
    if state == "pr-opened" and pr:
        body["pr"] = {"url": pr}
    if state in {"check-red", "check-green"}:
        body["check"] = {
            "result": "red" if state == "check-red" else "green",
            "name": name or "make ci",
            "log": error,
        }
    if acs:
        merged = dict(body.get("acResults") or {}) if isinstance(body.get("acResults"), dict) else {}
        merged.update(acs)
        body["acResults"] = merged
    if state in REVIEW_STATES:
        body["review"] = {
            "result": "pass" if state == "review-pass" else "fail",
            "sha": str(sha or "").strip(),
            "findings": [str(item).strip() for item in (findings or []) if str(item).strip()],
            "source": source or "shuttle",
            "at": stamp,
        }
    if state == "lock-escape":
        body["alarm"] = "lock-escape"
        body["escaped"] = _escaped(escaped)
    elif alarm:
        body["alarm"] = alarm
    if state == "alarm" and not body.get("alarm"):
        body["alarm"] = alarm or error or "alarm"
    if state == "failed" and not alarm:
        body["alarm"] = alarm or "failed"
    log_path = folder / "log.jsonl"
    existing = _read_log(log_path)
    seq = 1
    for row in existing:
        try:
            seq = max(seq, int(row.get("seq") or 0) + 1)
        except (TypeError, ValueError):
            continue
    line = {
        "seq": seq,
        "at": stamp,
        "id": tid,
        "state": state,
        "error": error or "",
    }
    if agent:
        line["agent"] = agent
    if pr:
        line["pr"] = pr
    if name:
        line["name"] = name
    if state == "lock-escape":
        line["escaped"] = body.get("escaped") or []
        line["alarm"] = "lock-escape"
    if alarm and state != "lock-escape":
        line["alarm"] = alarm
    if acs:
        line["acs"] = dict(acs)
    if state in REVIEW_STATES:
        line["sha"] = body["review"]["sha"]
        line["findings"] = body["review"]["findings"]
        line["source"] = body["review"]["source"]
    beam.atomic_write(folder / "state.json", json.dumps(body, indent=2) + "\n")
    with log_path.open("a") as handle:
        handle.write(json.dumps(line, separators=(",", ":")) + "\n")
    print("ticket: %s" % tid)
    print("state: %s" % state)
    print("dir: .warp/tickets/%s" % tid)
    code = 0 if not push else _push_dir(root, tid, state)
    for line in _reap_lines(root, tid, agent or _current_agent(root, tid) or str(body.get("agent") or ""), remote=bool(push)):
        print(line)
    return code


def _cloud_agent() -> str:
    """This VM's Cursor cloud agent id. Empty on a laptop."""
    try:
        import agents

        return agents.current_agent_id()
    except Exception:
        return ""


def _current_agent(root: Path, tid: str) -> str:
    """The agent the beam has on this ticket now. A line written without --agent is checked as that one,
    not as whichever Shuttle last wrote the folder (that was printing a stop line meant for an older Shuttle)."""
    try:
        data = json.loads((Path(root) / ".warp" / "beam.json").read_text())
        return str(((data.get("tickets") or {}).get(tid) or {}).get("agent") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _parse_acs(values) -> dict:
    import acceptance

    return acceptance.parse_args(values)


def _reap_lines(root: Path, tid: str, agent: str, remote: bool = False) -> list:
    """The reap answer for the Shuttle that just wrote its state.

    A worktree Shuttle reads the parent's beam. A Shuttle that pushes its
    folder is on its own VM and reads origin's base branch. No beam prints
    nothing. A failed check never fails the state write.
    """
    try:
        import agents

        lines = agents.reap_from(Path(root), agent, tid, "shuttle", remote=remote)
    except Exception:
        return []
    if lines and "(no beam" in lines[0]:
        return []
    return lines


def _push_dir(root: Path, tid: str, summary: str) -> int:
    """Commit and push only `.warp/tickets/<id>/`. Leave the beam snapshot unstaged.

    A named run commits the folder under `.warp/runs/<run>/tickets/<id>/`. The
    files are copied there first, so the merge of this branch carries the
    ticket's state and log into the run's folder on the base branch.
    """
    import runs

    local_rel = ".warp/tickets/%s" % tid
    run = runs.current(root)
    rel = runs.git_rel(run, local_rel)
    if run and rel != local_rel and (root / local_rel).is_dir():
        import shutil

        shutil.copytree(str(root / local_rel), str(root / rel), dirs_exist_ok=True)
    head = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    current = head.stdout.strip()
    base = _base_branch(root)
    if current and current == base:
        print("refuse: commit %s on the ticket branch, not %s" % (rel, base))
        return 2
    added = _git(root, "add", "--", rel)
    if added.returncode != 0:
        print("refuse: could not stage %s" % rel)
        print((added.stderr or added.stdout).strip())
        return 2
    message = "Warp ticket %s: %s" % (tid, summary)
    committed = _git(root, "commit", "--only", "-m", message, "--", rel)
    text = (committed.stdout or "") + (committed.stderr or "")
    if committed.returncode != 0:
        if "nothing to commit" in text or "no changes added" in text:
            print("committed: unchanged")
            return 0
        print("refuse: could not commit %s" % rel)
        print(text.strip())
        return 2
    # The commit must not contain the beam. Drop it if a pathspec ever swallowed it.
    shown = _git(root, "show", "--name-only", "--format=", "HEAD")
    names = [line.strip() for line in shown.stdout.splitlines() if line.strip()]
    beam_rel = runs.git_rel(run, ".warp/beam.json")
    if beam_rel in names or any(name.startswith(beam_rel) for name in names):
        print("refuse: commit included %s" % beam_rel)
        return 2
    outside = [name for name in names if not name.startswith(rel + "/") and name != rel]
    if outside:
        print("refuse: commit included %s" % ", ".join(outside))
        return 2
    print("committed: %s" % rel)
    if not _has_origin(root):
        return 0
    pushed = _git(root, "push", "origin", "HEAD")
    if pushed.returncode != 0:
        print("refuse: push of %s failed" % rel)
        print((pushed.stderr or pushed.stdout).strip())
        return 2
    print("pushed: origin %s" % (current or "HEAD"))
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-ticket status directory",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("append", help="append one event under .warp/tickets/<id>/")
    add.add_argument("--id", required=True)
    add.add_argument("--state", required=True, help="started, heartbeat, planning, coding, review, bugbot, fix, pr-opened, check-red, check-green, lock-escape, alarm, failed, done, review-pass, or review-fail")
    add.add_argument("--root", default=".")
    add.add_argument("--push", action="store_true", help="commit and push only .warp/tickets/<id>/")
    add.add_argument("--escaped", action="append", default=[], help="path outside the lock; repeat for each path")
    add.add_argument("--pr", default="", help="pull request url for pr-opened")
    add.add_argument("--name", default="", help="check name, for example make ci")
    add.add_argument("--error", default="", help="error text stored on the log line")
    add.add_argument("--alarm", default="", help="alarm reason")
    add.add_argument("--agent", default="")
    add.add_argument("--at", default="", help="timestamp; default is now")
    add.add_argument("--sha", default="", help="with review-pass or review-fail: the head commit that was reviewed")
    add.add_argument("--finding", action="append", default=[], help="with review-fail: one finding; repeat for each")
    add.add_argument("--source", default="", help="with review-pass or review-fail: shuttle (default) or hand")
    add.add_argument("--ac", action="append", default=[], help="an acceptance result, ID=pass|fail; repeat for each criterion")
    watch = sub.add_parser("observe", help="fetch in-flight branches and patch the live beam")
    watch.add_argument("--beam", default=".warp/beam.json")
    watch.add_argument("--root", default=".")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    if args.cmd == "append":
        return append_status(
            Path(args.root),
            args.id,
            args.state,
            when=args.at or None,
            error=args.error,
            escaped=args.escaped,
            pr=args.pr,
            name=args.name,
            alarm=args.alarm,
            agent=args.agent,
            push=bool(args.push),
            findings=args.finding,
            sha=args.sha,
            source=args.source,
            acs=_parse_acs(args.ac),
        )
    root = Path(args.root).resolve()
    path = Path(args.beam)
    if not path.is_file():
        path = root / args.beam
    if not path.is_file():
        print("observe: no beam at %s" % args.beam)
        return 2
    for line in observe(path):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
