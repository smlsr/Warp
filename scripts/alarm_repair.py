#!/usr/bin/env python3
"""Queue the next lock-escape repair. One at a time. Idempotent.

The parent tick calls this while the run is running. The listener Subagent
does not. Plugin hooks do not run on cloud runners. Pause and stop do not
repair, because the run is not running.

Only alarm reason lock-escape is repaired. Other reasons stay alarmed.

  python3 scripts/alarm_repair.py next --beam .warp/beam.json
  python3 scripts/alarm_repair.py next --beam .warp/beam.json --returned WV-01
  python3 scripts/alarm_repair.py next --beam .warp/beam.json --returned WV-01 --error "boom"

A second call while a repair Shuttle is still working prints
alarm-repair: working and does not start another. A failure records the
attempt, leaves the alarm, and starts the next lock-escape ticket in that
same call. The same ticket is not started again until the next pass.

The repair widens that ticket's locks to the paths that escaped. Those
paths are stored on the alarm (`escaped`). A path an in-flight ticket
holds is not taken: the repair waits, then widens and starts. A queued
ticket's overlapping lock can be widened.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beam  # noqa: E402
import orchestrator  # noqa: E402

REASON = "lock-escape"
DEFAULT_ALARM_REPAIR_MINUTES = 15
DEFAULT_MAX_ALARM_REPAIRS = 5
# Statuses that mean the repair Shuttle is still the worker on this ticket.
# review and later belong to Reed or a person. merged and done are finished.
FORWARD = {
    "claimed",
    "recovering",
    "planning",
    "coding",
    "review",
    "bugbot_running",
    "fix",
    "awaiting_approval",
    "merging",
    "merged",
    "done",
}
RECORD_NAME = "alarm-repair.json"

HELP = """
examples:
  python3 scripts/alarm_repair.py ?
  python3 scripts/alarm_repair.py next --beam .warp/beam.json
  python3 scripts/alarm_repair.py next --beam .warp/beam.json --returned WV-01
  python3 scripts/alarm_repair.py next --beam .warp/beam.json --returned WV-01 --error "boom"
  python3 scripts/alarm_repair.py next --beam .warp/beam.json --now 2026-10-06T12:00:00Z

Subcommand: next. One repair Shuttle at a time, for lock-escape only.
alarmRepairMinutes (default 15) is how often a running listener opens a pass.
maxAlarmRepairs (default 5) is the cap per ticket. A second tick is idempotent.
Paused and stopped runs print alarm-repair: skipped and change nothing.
When that pass opens and the ready set is empty, pending gates are
recomputed, including a stale red gate the beam can disprove. A gate turns
green when every member is merged or done, or when every ticket is. Herald
prints "<gate> pending cleared. Members merged. Tick ran." and the dispatch
tick prints start lines in that same output. A second pass does not start
them again. A member that is not merged, a ticket that is still alarmed or
parked, or a check that is actually red leaves the gate as it is.
The same pass sends a red `make ci` back to that ticket even when the
ready set is not empty: `send-back <id> fix` and a start line. Launch
that Shuttle with the log. A Shuttle already in fix is not started
again. Parked does not launch. A green `make ci` is recorded and is
not a failure.
The repair widens the ticket's locks to the paths on `escaped` and does not
clear the alarm. It waits when an in-flight ticket holds one of those paths.
A queued ticket does not block the widen. Herald lines, including the paths
added, are also written to .warp/alarm-repair.json.

  python3 scripts/alarm_repair.py paths --beam .warp/beam.json --id WV-01 --path src/extra

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""


def _repair_state(data: dict) -> dict:
    raw = data.get("alarmRepair")
    if not isinstance(raw, dict):
        raw = {}
        data["alarmRepair"] = raw
    tried = raw.get("tried")
    if not isinstance(tried, list):
        raw["tried"] = []
    return raw


def _info(ticket: dict) -> dict:
    raw = ticket.get("alarmRepair")
    if not isinstance(raw, dict):
        raw = {}
        ticket["alarmRepair"] = raw
    return raw


def _attempts(ticket: dict) -> int:
    try:
        return int(ticket.get("alarmRepairs") or 0)
    except (TypeError, ValueError):
        return 0


def _cleared(alarm) -> bool:
    return alarm in (None, "")


def _is_lock_escape(ticket: dict) -> bool:
    return ticket.get("status") == "alarm" and ticket.get("alarm") == REASON


def _plan_order(data: dict) -> list:
    """Lock-escape alarms in beam order, then id. Beam order is plan order."""
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    index = {tid: i for i, tid in enumerate(tickets)}
    rows = []
    for tid, ticket in tickets.items():
        if isinstance(ticket, dict) and _is_lock_escape(ticket):
            rows.append(ticket)
    rows.sort(key=lambda ticket: (index.get(ticket.get("id"), 10**9), str(ticket.get("id"))))
    return rows


def normalize_paths(values) -> list:
    """Drop blanks and duplicates. Keep the order the Shuttle named."""
    out = []
    seen = set()
    for raw in values or []:
        if not isinstance(raw, str):
            continue
        path = raw.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _escaped(ticket: dict) -> list:
    return normalize_paths(ticket.get("escaped"))


def _already_locked(locks: list, path: str) -> bool:
    """True when path is already that lock or sits inside it."""
    path = path.rstrip("/")
    for lock in locks:
        if not isinstance(lock, str) or not lock.strip():
            continue
        have = lock.strip().rstrip("/")
        if path == have or path.startswith(have + "/"):
            return True
    return False


def _holder(data: dict, ticket: dict, paths: list) -> Optional[str]:
    """In-flight ticket whose locks overlap a path this repair would take.

    Queued, alarmed, and finished tickets do not hold a lock. ACTIVE is the
    same set beam.held_locks uses.
    """
    wanted = list(paths)
    if not wanted:
        return None
    for other in (data.get("tickets") or {}).values():
        if not isinstance(other, dict) or other.get("id") == ticket.get("id"):
            continue
        if other.get("status") not in beam.ACTIVE:
            continue
        if beam.lock_overlap(wanted, list(other.get("locks") or [])):
            return str(other.get("id"))
    return None


def _widen(ticket: dict, paths: list) -> list:
    """Add escaped paths that the ticket does not already lock. Returns the added list."""
    locks = [lock for lock in (ticket.get("locks") or []) if isinstance(lock, str) and lock.strip()]
    added = []
    for path in paths:
        if _already_locked(locks, path):
            continue
        locks.append(path)
        added.append(path)
    ticket["locks"] = locks
    ticket["addedLocks"] = list(added)
    return added


def _at_cap(ticket: dict, cap: int) -> bool:
    info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
    return bool(info.get("gaveUp")) or _attempts(ticket) >= cap


def _success_complete(ticket: dict, returned: bool) -> bool:
    """Merged, or back on the normal path with the alarm cleared and the shuttle done.

    While status is still shuttle work, the repair Shuttle is still working
    unless the listener passed --returned.
    """
    status = ticket.get("status")
    if status in {"merged", "done"}:
        return True
    if not _cleared(ticket.get("alarm")) or status not in FORWARD:
        return False
    if status not in beam.SHUTTLE_WORK:
        return True
    return returned


def _observe(ticket: dict, returned: bool, error: Optional[str]) -> str:
    """working, waiting, passed, or failed. Does not mutate."""
    info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
    if info.get("state") != "working":
        if info.get("state") == "passed":
            return "passed"
        if info.get("state") in {"failed", "gave-up"}:
            return "failed"
        return "working"
    if error:
        return "failed"
    status = ticket.get("status")
    alarm = ticket.get("alarm")
    if status == "alarm" and alarm == REASON:
        return "failed"
    if status == "alarm":
        return "failed"
    if _success_complete(ticket, returned):
        return "passed"
    if returned:
        return "failed"
    if _cleared(alarm) and status in beam.SHUTTLE_WORK:
        return "waiting"
    return "working"


def _herald_give_up(ticket: dict, lines: list) -> None:
    info = _info(ticket)
    if info.get("gaveUpPosted"):
        return
    info["gaveUp"] = True
    info["gaveUpPosted"] = True
    if info.get("state") == "working":
        info["state"] = "gave-up"
    tid = ticket.get("id")
    lines.append("alarm-repair: gave-up %s" % tid)
    lines.append("herald: %s lock-escape repair given up." % tid)


def _record_failure(ticket: dict, now_s: str, error: Optional[str], cap: int, lines: list) -> None:
    """Leave the ticket alarmed. Do not clear lock-escape to look finished."""
    prev = ticket.get("status")
    alarm = ticket.get("alarm")
    if alarm in (None, "", REASON):
        ticket["status"] = "alarm"
        ticket["alarm"] = REASON
    elif prev != "alarm":
        ticket["status"] = "alarm"
    info = _info(ticket)
    info["state"] = "failed"
    info["failedAt"] = now_s
    info["attempts"] = _attempts(ticket)
    if error:
        info["error"] = error
    ticket["updatedAt"] = now_s
    lines.append("alarm-repair: failed %s" % ticket.get("id"))


def _append_status(beam_path: Path, ticket: dict, now_s: str, prev: str) -> None:
    if prev == ticket.get("status"):
        return
    beam.append_ticket_event(
        beam_path,
        ticket,
        {"at": now_s, "type": "status", "from": prev, "to": ticket.get("status")},
    )


def _start(data: dict, ticket: dict, now_s: str, beam_path: Path, lines: list) -> None:
    """Widen to the escaped paths, then claim one repair. The alarm stays lock-escape."""
    prev = ticket.get("status")
    added = _widen(ticket, _escaped(ticket))
    locks = list(ticket.get("locks") or [])
    n = _attempts(ticket) + 1
    agent = "repair-%s-%d" % (ticket.get("id"), n)
    ticket["alarmRepairs"] = n
    ticket["status"] = "claimed"
    ticket["alarm"] = REASON
    ticket["agent"] = agent
    ticket["workerStartedAt"] = now_s
    if not ticket.get("claimedAt"):
        ticket["claimedAt"] = now_s
    ticket.pop("lastSeenAt", None)
    info = _info(ticket)
    info.update(
        {
            "state": "working",
            "attempts": n,
            "startedAt": now_s,
            "agent": agent,
            "gaveUp": False,
            "added": list(added),
        }
    )
    info.pop("error", None)
    info.pop("failedAt", None)
    ticket["updatedAt"] = now_s
    repair = _repair_state(data)
    repair["active"] = ticket.get("id")
    repair["naming"] = None
    tried = repair["tried"]
    if ticket.get("id") not in tried:
        tried.append(ticket.get("id"))
    _append_status(beam_path, ticket, now_s, prev)
    beam.append_ticket_event(
        beam_path,
        ticket,
        {
            "at": now_s,
            "type": "alarm-repair",
            "value": "started",
            "attempts": n,
            "agent": agent,
            "added": list(added),
        },
    )
    beam.journal(
        beam_path,
        {
            "type": "alarm-repair",
            "action": "start",
            "id": ticket.get("id"),
            "attempts": n,
            "agent": agent,
            "added": list(added),
        },
    )
    lock_text = ",".join(locks)
    added_text = ",".join(added)
    lines.append(
        "alarm-repair: start %s agent=%s locks=%s added=%s" % (ticket.get("id"), agent, lock_text, added_text)
    )
    herald = "%s lock-escape repair started." % ticket.get("id")
    if added:
        herald += " Added %s." % ", ".join(added)
    lines.append("herald: " + herald)


def _consider(data: dict, cap: int, lines: list) -> Optional[dict]:
    """First lock-escape ticket this pass can start, or None.

    Capped tickets are skipped. The first eligible ticket that still needs
    path names, or whose escaped paths are held in flight, waits. A later
    ticket is not started ahead of it. Queued holders do not block.
    """
    repair = _repair_state(data)
    tried = set(repair.get("tried") or [])
    for ticket in _plan_order(data):
        tid = ticket.get("id")
        if tid in tried:
            continue
        if _at_cap(ticket, cap):
            _herald_give_up(ticket, lines)
            continue
        paths = _escaped(ticket)
        # Existing locks plus the paths we would add. An in-flight holder blocks
        # the widen. A queued ticket does not.
        wanted = normalize_paths(list(ticket.get("locks") or []) + paths)
        busy = _holder(data, ticket, wanted)
        if busy:
            lines.append("alarm-repair: locked %s by %s" % (tid, busy))
            return None
        if not paths:
            repair["naming"] = tid
            lines.append("alarm-repair: name %s" % tid)
            return None
        return ticket
    return None


def _close_pass(repair: dict, now_s: str) -> None:
    repair["active"] = None
    repair["passOpen"] = False
    repair["checkedAt"] = now_s


def _open_pass(repair: dict, now_s: str) -> None:
    repair["tried"] = []
    repair["passOpen"] = True
    repair["passStartedAt"] = now_s
    repair["checkedAt"] = now_s
    repair["active"] = None
    repair["naming"] = None


def _interval_elapsed(repair: dict, now_dt, minutes: int) -> bool:
    if repair.get("passOpen"):
        return True
    stamp = repair.get("checkedAt")
    if not stamp:
        return True
    seen = beam.parse_ts(stamp)
    if seen is None:
        return True
    return (now_dt - seen).total_seconds() / 60.0 >= minutes


def _finish_pass_or_start(data: dict, beam_path: Path, now_s: str, cap: int, lines: list) -> None:
    repair = _repair_state(data)
    nxt = _consider(data, cap, lines)
    if nxt is None:
        waiting = any(
            line.startswith("alarm-repair: locked ") or line.startswith("alarm-repair: name ") for line in lines
        )
        if not waiting:
            _close_pass(repair, now_s)
            lines.append("alarm-repair: idle")
        return
    _start(data, nxt, now_s, beam_path, lines)


def _settle_active(data: dict, beam_path: Path, now_s: str, cap: int, returned: Optional[str], error: Optional[str], lines: list) -> bool:
    """Settle the active repair.

    Returns True when the beam changed. A still-working shuttle returns False
    so a second tick does not rewrite the beam or start another repair.
    """
    repair = _repair_state(data)
    tid = repair.get("active")
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    if not tid or tid not in tickets:
        repair["active"] = None
        return True
    ticket = tickets[tid]
    mine = returned == tid
    outcome = _observe(ticket, mine, error if mine else None)
    if outcome == "working":
        lines.append("alarm-repair: working %s" % tid)
        return False
    if outcome == "waiting":
        lines.append("alarm-repair: waiting %s" % tid)
        return False
    prev = ticket.get("status")
    if outcome == "failed":
        _record_failure(ticket, now_s, error if mine else None, cap, lines)
        _append_status(beam_path, ticket, now_s, prev)
        info = _info(ticket)
        beam.append_ticket_event(
            beam_path,
            ticket,
            {
                "at": now_s,
                "type": "alarm-repair",
                "value": "failed",
                "attempts": _attempts(ticket),
                "error": info.get("error"),
            },
        )
        beam.journal(
            beam_path,
            {
                "type": "alarm-repair",
                "action": "failed",
                "id": tid,
                "attempts": _attempts(ticket),
                "error": info.get("error"),
            },
        )
        repair["active"] = None
        if tid not in repair["tried"]:
            repair["tried"].append(tid)
        # Start the next in this same call. Do not wait for the interval
        # and do not retry this ticket in this pass.
        before = len(lines)
        nxt = _consider(data, cap, lines)
        if nxt is not None:
            lines.append("herald: %s lock-escape repair failed. Taking %s." % (tid, nxt.get("id")))
        if _attempts(ticket) >= cap:
            _herald_give_up(ticket, lines)
        if nxt is not None:
            _start(data, nxt, now_s, beam_path, lines)
        elif not any(
            line.startswith("alarm-repair: locked ") or line.startswith("alarm-repair: name ") for line in lines[before:]
        ):
            _close_pass(repair, now_s)
            lines.append("alarm-repair: idle")
        return True
    info = _info(ticket)
    info["state"] = "passed"
    info["passedAt"] = now_s
    ticket["updatedAt"] = now_s
    beam.append_ticket_event(
        beam_path,
        ticket,
        {"at": now_s, "type": "alarm-repair", "value": "passed", "attempts": _attempts(ticket)},
    )
    beam.journal(beam_path, {"type": "alarm-repair", "action": "passed", "id": tid, "attempts": _attempts(ticket)})
    lines.append("alarm-repair: passed %s" % tid)
    repair["active"] = None
    if tid not in repair["tried"]:
        repair["tried"].append(tid)
    _finish_pass_or_start(data, beam_path, now_s, cap, lines)
    return True


def _resolve_naming(data: dict, beam_path: Path, now_s: str, cap: int, lines: list) -> bool:
    """The naming Shuttle returned. Widen and start, wait, or leave this ticket for the pass."""
    repair = _repair_state(data)
    tid = repair.get("naming")
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    repair["naming"] = None
    ticket = tickets.get(tid) if tid else None
    if not isinstance(ticket, dict):
        return True
    paths = _escaped(ticket)
    wanted = normalize_paths(list(ticket.get("locks") or []) + paths)
    busy = _holder(data, ticket, wanted) if paths else None
    if not paths:
        if tid not in repair["tried"]:
            repair["tried"].append(tid)
        lines.append("alarm-repair: unnamed %s" % tid)
        _finish_pass_or_start(data, beam_path, now_s, cap, lines)
        return True
    if busy:
        repair["passOpen"] = True
        lines.append("alarm-repair: locked %s by %s" % (tid, busy))
        return True
    _start(data, ticket, now_s, beam_path, lines)
    return True


def _apply(beam_path: Path, now=None, returned: Optional[str] = None, error: Optional[str] = None) -> list:
    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return ["alarm-repair: no beam at %s" % beam_path]
    data = beam.load_json(beam_path)
    now_dt, now_s = beam.coerce_now(now)
    live, why = beam._run_live(data)
    if not live:
        return ["alarm-repair: skipped (%s)" % why]
    minutes = beam.config_int(data, beam_path, "alarmRepairMinutes", DEFAULT_ALARM_REPAIR_MINUTES)
    cap = beam.config_int(data, beam_path, "maxAlarmRepairs", DEFAULT_MAX_ALARM_REPAIRS)
    if minutes < 0:
        minutes = DEFAULT_ALARM_REPAIR_MINUTES
    if cap < 0:
        cap = DEFAULT_MAX_ALARM_REPAIRS
    repair = _repair_state(data)
    lines: list = []
    flipped_gates: list = []
    sent: list = []
    if repair.get("active"):
        changed = _settle_active(data, beam_path, now_s, cap, returned, error, lines)
    elif repair.get("naming"):
        if returned == repair.get("naming"):
            changed = _resolve_naming(data, beam_path, now_s, cap, lines)
        else:
            lines.append("alarm-repair: naming %s" % repair.get("naming"))
            changed = False
    elif repair.get("passOpen"):
        # The pass is already open (the next ticket was lock-busy).
        # Do not clear `tried` and do not wait out the interval.
        _finish_pass_or_start(data, beam_path, now_s, cap, lines)
        changed = any(
            line.startswith("alarm-repair: start ")
            or line.startswith("alarm-repair: idle")
            or line.startswith("alarm-repair: gave-up ")
            or line.startswith("alarm-repair: name ")
            or line.startswith("herald: ")
            for line in lines
        )
    elif not _interval_elapsed(repair, now_dt, minutes):
        lines.append("alarm-repair: wait")
        changed = False
    else:
        _open_pass(repair, now_s)
        # Snapshot before repair mutates the beam. The idle case Shawn hit
        # is an empty ready set: nothing in flight, nothing waiting, no
        # alarms, and pending gates whose members are already merged.
        ready_empty = not beam.ready(data)
        _finish_pass_or_start(data, beam_path, now_s, cap, lines)
        yaml_text = ""
        cfg_path = beam.config_yaml_path(beam_path)
        if cfg_path.is_file():
            try:
                yaml_text = cfg_path.read_text()
            except OSError:
                yaml_text = ""
        sent = orchestrator.send_back_red_checks(data, yaml_text)
        if sent:
            lines.extend(orchestrator.format_send_back(sent, data))
        if ready_empty:
            flipped_gates = beam.advance_pending_gates(data, now_s)
            if flipped_gates:
                lines.extend(beam.gate_flip_lines(flipped_gates, data, True))
        changed = True
    if changed:
        data["metrics"] = beam.metrics(data)
        beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
        for action in sent:
            beam.journal(
                beam_path,
                {
                    "type": "send-back",
                    "id": action.get("id"),
                    "outcome": action.get("outcome"),
                    "check": "make ci",
                },
            )
        if flipped_gates:
            for gate in flipped_gates:
                beam.journal(
                    beam_path,
                    {
                        "type": "gate",
                        "key": gate.get("key"),
                        "status": "green",
                        "evidence": gate.get("evidence"),
                    },
                )
            try:
                beam.write_board_files(beam_path, data)
            except Exception as exc:
                print("board: not rewritten (%s)" % exc)
        herald = [line[len("herald: ") :] for line in lines if line.startswith("herald: ")]
        if herald:
            beam.atomic_write(
                beam_path.parent / RECORD_NAME,
                json.dumps({"at": now_s, "lines": herald}, indent=2) + "\n",
            )
    return lines


def store_paths(beam_path: Path, tid: str, paths: list, now=None) -> list:
    """Record escaped paths on a lock-escape alarm. Does not widen and does not start."""
    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return ["alarm-repair: no beam at %s" % beam_path]
    lock_path = beam_path.parent / ".alarm-repair.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = beam.load_json(beam_path)
            tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
            ticket = tickets.get(tid)
            if not isinstance(ticket, dict):
                return ["alarm-repair: unknown %s" % tid]
            if ticket.get("status") != "alarm" or ticket.get("alarm") != REASON:
                return ["alarm-repair: paths refused %s" % tid]
            cleaned = normalize_paths(paths)
            if not cleaned:
                return ["alarm-repair: paths refused %s" % tid]
            _now_dt, now_s = beam.coerce_now(now)
            ticket["escaped"] = cleaned
            ticket["updatedAt"] = now_s
            beam.journal(beam_path, {"type": "alarm-repair", "action": "paths", "id": tid, "escaped": cleaned})
            beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
            return ["alarm-repair: paths %s %s" % (tid, ",".join(cleaned))]
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def next_repair(beam_path: Path, now=None, returned: Optional[str] = None, error: Optional[str] = None) -> list:
    """Claim or advance one lock-escape repair. Safe to call twice."""
    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return ["alarm-repair: no beam at %s" % beam_path]
    lock_path = beam_path.parent / ".alarm-repair.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _apply(beam_path, now=now, returned=returned, error=error)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Queue the next lock-escape repair",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    cmd = sub.add_parser("next", help="claim or advance the one lock-escape repair")
    cmd.add_argument("--beam", default=".warp/beam.json")
    cmd.add_argument("--now", help="timestamp for tests, YYYY-MM-DDTHH:MM:SSZ")
    cmd.add_argument("--returned", help="repair Shuttle for this ticket id has returned")
    cmd.add_argument("--error", help="with --returned, the repair errored; the alarm stays")
    paths = sub.add_parser("paths", help="store escaped paths on a lock-escape alarm; does not widen")
    paths.add_argument("--beam", default=".warp/beam.json")
    paths.add_argument("--id", required=True)
    paths.add_argument("--path", action="append", required=True, help="file or directory outside the lock")
    paths.add_argument("--now", help="timestamp for tests, YYYY-MM-DDTHH:MM:SSZ")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    if args.cmd == "paths":
        lines = store_paths(Path(args.beam), args.id, args.path, now=args.now)
        for line in lines:
            print(line)
        if lines and (lines[0].startswith("alarm-repair: no beam") or lines[0].startswith("alarm-repair: unknown") or "refused" in lines[0]):
            return 1
        return 0
    lines = next_repair(Path(args.beam), now=args.now, returned=args.returned, error=args.error)
    for line in lines:
        print(line)
    if lines and lines[0].startswith("alarm-repair: no beam"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
