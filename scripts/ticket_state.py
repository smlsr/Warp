#!/usr/bin/env python3
"""Per-ticket status the parent can read after the Agent is gone.

The live beam stays on the orchestrator. A ticket Agent does not commit
updates to it. The launch snapshot of `.warp/beam.json` on the ticket branch
is claim-in: the Agent reads it and leaves it alone.

Status comes out in that ticket's own directory. Only that Agent writes it:

  .warp/tickets/<id>/state.json   current state
  .warp/tickets/<id>/log.jsonl    one line per event

The Agent commits and pushes only that directory when the state changes, and
on a heartbeat at least every 5 minutes. Those directories do not overlap, so
they may merge to main. The beam file may not.

Every tick fetches each in-flight branch and reads only that ticket's
directory. It patches the live beam. It does not copy the branch beam. A
second tick applies nothing again. Paused and stopped runs do not fetch.

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
)
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


def _apply_lock_escape(beam_path: Path, ticket: dict, paths: list, now: str) -> bool:
    paths = _escaped(paths)
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
    dead Agent's red check is not dropped. fix stays fix. The third red parks.
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


def _apply_event(beam_path: Path, data: dict, ticket: dict, event: dict, now: str, heralded: set, lines: list) -> bool:
    """Patch one log line onto this ticket. Returns True when the beam changed."""
    state = str(event.get("state") or "").strip()
    if state not in STATES:
        return False
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
    if name == "lock-escape" or state.get("alarm") == "lock-escape":
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
    state_text = _show(root, ref, ".warp/tickets/%s/state.json" % tid)
    log_text = _show(root, ref, ".warp/tickets/%s/log.jsonl" % tid)
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
) -> list:
    """Fetch in-flight branches and patch the live beam from each ticket directory.

    Paused and stopped runs return before any fetch. A missing git repo returns
    nothing, so a beam that is not in a checkout is left to the heartbeat already
    stored on it.
    """
    beam_path = Path(beam_path)
    data = beam.load_json(beam_path)
    _now_dt, now_s = beam.coerce_now(now)
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
    if state == "heartbeat":
        body["heartbeatAt"] = stamp
        if not body.get("state"):
            body["state"] = "started"
    else:
        body["state"] = state
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
    beam.atomic_write(folder / "state.json", json.dumps(body, indent=2) + "\n")
    with log_path.open("a") as handle:
        handle.write(json.dumps(line, separators=(",", ":")) + "\n")
    print("ticket: %s" % tid)
    print("state: %s" % state)
    print("dir: .warp/tickets/%s" % tid)
    if not push:
        return 0
    return _push_dir(root, tid, state)


def _push_dir(root: Path, tid: str, summary: str) -> int:
    """Commit and push only `.warp/tickets/<id>/`. Leave the beam snapshot unstaged."""
    rel = ".warp/tickets/%s" % tid
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
    if ".warp/beam.json" in names or any(name.startswith(".warp/beam.json") for name in names):
        print("refuse: commit included .warp/beam.json")
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
    add.add_argument("--state", required=True, help="started, heartbeat, planning, coding, review, bugbot, fix, pr-opened, check-red, check-green, lock-escape, alarm, failed, or done")
    add.add_argument("--root", default=".")
    add.add_argument("--push", action="store_true", help="commit and push only .warp/tickets/<id>/")
    add.add_argument("--escaped", action="append", default=[], help="path outside the lock; repeat for each path")
    add.add_argument("--pr", default="", help="pull request url for pr-opened")
    add.add_argument("--name", default="", help="check name, for example make ci")
    add.add_argument("--error", default="", help="error text stored on the log line")
    add.add_argument("--alarm", default="", help="alarm reason")
    add.add_argument("--agent", default="")
    add.add_argument("--at", default="", help="timestamp; default is now")
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
