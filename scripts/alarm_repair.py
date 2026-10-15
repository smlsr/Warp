#!/usr/bin/env python3
"""Start lock-escape repairs. On the pass that sees the alarm, in parallel, never two on one file. Idempotent.

The parent tick calls this while the run is running. The listener Subagent
does not. Plugin hooks do not run on cloud runners. Pause and stop do not
repair, because the run is not running.

Only alarm reason lock-escape is repaired. Other reasons stay alarmed.

  python3 scripts/alarm_repair.py next --beam .warp/beam.json
  python3 scripts/alarm_repair.py next --beam .warp/beam.json --returned WV-01
  python3 scripts/alarm_repair.py next --beam .warp/beam.json --returned WV-01 --error "boom"

Every lock-escape ticket that can start prints `alarm-repair: start <id>` and
`alarm-repair: launch <id>`: run checkout.py launch for each and start one
Shuttle per ticket with its prompt. `--returned <id>` before that ticket's
repair Shuttle was launched prints `alarm-repair: not launched <id>` and does
not use an attempt.

Decided one ticket at a time, in plan order, under the beam lock; run in
parallel. A started repair is `claimed` with its widened locks before the next
ticket is looked at, so two escapes into the same file never both start: the
second prints `alarm-repair: locked <id> by <first>` and starts when the first
finishes. A ticket that waits does not hold back the ones after it. Each start
takes a Shuttle slot (`alarm-repair: queued <id>` when maxAgents is full).

The repair widens that ticket's locks to the paths that escaped. Those paths
are stored on the alarm (`escaped`). A path an in-flight ticket holds is not
taken: the repair waits, then widens and starts. A queued ticket's
overlapping lock can be widened. A failure leaves the alarm and records the
attempt; the ticket tries again alarmRepairMinutes after the failure.
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

Subcommand: next. Every lock-escape ticket that can start, in parallel, never two on one file.
alarmRepairMinutes (default 15) is the wait before a failed repair tries again,
and how often the send-back and gate housekeeping below runs.
maxAlarmRepairs (default 5) is the cap per ticket. A second tick is idempotent.
Paused and stopped runs print alarm-repair: skipped and change nothing.
When that housekeeping runs and the ready set is empty, pending gates are
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
`alarm-repair: start <id>` is followed by `alarm-repair: launch <id>`: run the
checkout.py launch it names and start one Shuttle with the prompt. Pass
--returned <id> only after that Shuttle returns. A return with no Shuttle
launched prints `alarm-repair: not launched <id>` and uses no attempt.
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


# A package manager rewrites the lockfile beside a manifest whenever the manifest's dependencies change.
LOCKFILE_OF = {
    "package.json": ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb"),
    "go.mod": ("go.sum",),
    "Cargo.toml": ("Cargo.lock",),
    "pyproject.toml": ("poetry.lock", "uv.lock", "pdm.lock"),
    "Pipfile": ("Pipfile.lock",),
    "Gemfile": ("Gemfile.lock",),
    "composer.json": ("composer.lock",),
}


def implied_locks(locks: list) -> list:
    """Lockfiles covered by a locked manifest: `app/package.json` covers `app/package-lock.json`.

    Before 1.6.18 WE-01 locked package.json, npm install wrote package-lock.json, and the lockfile counted as
    outside the ticket's locks.
    """
    out = []
    for lock in locks or []:
        if not isinstance(lock, str) or lock.endswith("/"):
            continue
        folder, _, name = lock.strip().rpartition("/")
        for companion in LOCKFILE_OF.get(name, ()):
            path = "%s/%s" % (folder, companion) if folder else companion
            if path not in locks and path not in out:
                out.append(path)
    return out


def _already_locked(locks: list, path: str) -> bool:
    """True when path is already that lock or sits inside it, or is the lockfile of a locked manifest."""
    path = path.rstrip("/")
    for lock in list(locks) + implied_locks(list(locks)):
        if not isinstance(lock, str) or not lock.strip():
            continue
        have = lock.strip().rstrip("/")
        if path == have or path.startswith(have + "/"):
            return True
    return False


def _holder(data: dict, ticket: dict, paths: list, now=None) -> Optional[str]:
    """In-flight ticket whose locks overlap a path this repair would take.

    Queued, alarmed, and finished tickets do not hold a lock. ACTIVE is the
    same set beam.held_locks uses.
    """
    import multirepo

    wanted = list(paths)
    if not wanted:
        return None
    mine = multirepo.scoped_locks(data, ticket, wanted)
    for other in (data.get("tickets") or {}).values():
        if not isinstance(other, dict) or other.get("id") == ticket.get("id"):
            continue
        if not beam.holds_now(other, data, now=now):
            continue
        if beam.lock_overlap(mine, multirepo.scoped_locks(data, other, list(other.get("locks") or []))):
            return str(other.get("id"))
    if not multirepo.is_sibling(data, ticket):
        import runs

        for label, locks in runs.external_locks(data):
            if beam.lock_overlap(mine, locks):
                return str(label)
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
    unless the parent passed --returned.
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
    # The launch marks it pending. Until then no repair Shuttle is out.
    ticket["shuttle"] = {"step": "repair", "pending": False, "hadPr": bool((ticket.get("pr") or {}).get("url"))}
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
    info.pop("launchedAt", None)
    ticket["updatedAt"] = now_s
    repair = _repair_state(data)
    active = _active(repair)
    if ticket.get("id") not in active:
        active.append(ticket.get("id"))
    _drop(_naming(repair), ticket.get("id"))
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
    lines.append(launch_line(ticket.get("id"), beam_path))
    herald = "%s lock-escape repair started." % ticket.get("id")
    if added:
        herald += " Added %s." % ", ".join(added)
    lines.append("herald: " + herald)


def launch_line(tid: str, beam_path: Path) -> str:
    """What the parent does with a started repair: launch one Shuttle, then wait for it."""
    root = Path(beam_path).resolve().parent.parent
    return (
        "alarm-repair: launch %s. Run python3 %s/checkout.py launch --root %s --id %s and start one Shuttle "
        "subagent with the prompt it prints, in that ticket's worktree. Pass --returned %s only after that "
        "Shuttle returns. A return with no Shuttle launched is refused and does not use an attempt."
        % (tid, orchestrator.scripts_dir(), root, tid, tid)
    )


def worker_seen(ticket: dict) -> bool:
    """True once a repair Shuttle was launched or has touched the ticket since the repair started."""
    info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
    if info.get("launchedAt") or ticket.get("lastSeenAt"):
        return True
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    if shuttle.get("pending") or shuttle.get("returned"):
        return True
    # The Shuttle moved the ticket off the state the repair left it in.
    return ticket.get("status") != "claimed" or ticket.get("alarm") != REASON


def _active(repair: dict) -> list:
    """Tickets with a repair Shuttle out. Before 1.6.27 this was one id; a string is read as a list of one."""
    raw = repair.get("active")
    ids = [raw] if isinstance(raw, str) and raw else [x for x in (raw or []) if isinstance(x, str) and x]
    repair["active"] = ids
    return ids


def _naming(repair: dict) -> list:
    raw = repair.get("naming")
    ids = [raw] if isinstance(raw, str) and raw else [x for x in (raw or []) if isinstance(x, str) and x]
    repair["naming"] = ids
    return ids


def _drop(ids: list, tid: str) -> None:
    while tid in ids:
        ids.remove(tid)


def _backoff(ticket: dict, now_dt, minutes: int) -> Optional[str]:
    """When a failed repair may try again: alarmRepairMinutes after the failure. None when it may now."""
    info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
    if info.get("state") != "failed" or not info.get("failedAt"):
        return None
    failed = beam.parse_ts(info["failedAt"])
    if failed is None:
        return None
    left = minutes - (now_dt - failed).total_seconds() / 60.0
    return None if left <= 0 else info["failedAt"]


def _room(data: dict) -> int:
    try:
        return int(beam.shuttle_room(data))
    except Exception:
        return 1


def start_ready(data: dict, beam_path: Path, now_dt, now_s: str, cap: int, minutes: int, lines: list) -> list:
    """Start every lock-escape repair that can start now. Returns the ids started.

    Decided one ticket at a time, in plan order, inside the beam lock; run in parallel. A started repair
    is `claimed` with its widened locks before the next ticket is looked at, so two escapes that need
    the same file never both start: the second sees the first as the holder and waits. A ticket that
    waits does not hold back the ones after it. Each start takes a Shuttle slot (maxAgents).
    """
    repair = _repair_state(data)
    active = _active(repair)
    naming = _naming(repair)
    budget = _room(data)
    started = []
    for ticket in _plan_order(data):
        tid = ticket.get("id")
        if tid in active or tid in naming:
            continue
        if orchestrator.shuttle_still_out(ticket, data, now=now_s):
            # The escape was seen while its own Shuttle still works. Relaunching now would put two Shuttles on it.
            lines.append("alarm-repair: shuttle-out %s (repaired when its Shuttle returns)" % tid)
            continue
        if _at_cap(ticket, cap):
            _herald_give_up(ticket, lines)
            continue
        since = _backoff(ticket, now_dt, minutes)
        if since:
            lines.append("alarm-repair: retry %s after %d minutes (failed %s)" % (tid, minutes, since))
            continue
        paths = _escaped(ticket)
        if not paths:
            naming.append(tid)
            lines.append("alarm-repair: name %s" % tid)
            continue
        # Existing locks plus the paths we would add. An in-flight holder (including a repair started a
        # moment ago in this loop) blocks the widen. A queued or alarmed ticket does not.
        wanted = normalize_paths(list(ticket.get("locks") or []) + paths)
        busy = _holder(data, ticket, wanted, now=now_s)
        if busy:
            lines.append("alarm-repair: locked %s by %s" % (tid, busy))
            continue
        if min(budget, _room(data) + len(started)) - len(started) <= 0:
            lines.append("alarm-repair: queued %s (no free Shuttle slot; maxAgents)" % tid)
            continue
        _start(data, ticket, now_s, beam_path, lines)
        started.append(tid)
    return started


def _settle(data: dict, beam_path: Path, tid: str, now_s: str, cap: int, returned: Optional[str], error: Optional[str], lines: list) -> bool:
    """Settle one active repair. True when the beam changed; a still-working Shuttle changes nothing."""
    repair = _repair_state(data)
    active = _active(repair)
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    if tid not in tickets:
        _drop(active, tid)
        return True
    ticket = tickets[tid]
    mine = returned == tid
    if mine and not worker_seen(ticket):
        # Nothing ran. Counting this would spend an attempt on a Shuttle that never started.
        lines.append(
            "alarm-repair: not launched %s. No repair Shuttle ran for attempt %d, so it is not counted."
            % (tid, _attempts(ticket))
        )
        if error:
            lines.append("alarm-repair: error noted, not counted: %s" % error)
        lines.append(launch_line(tid, beam_path))
        return False
    outcome = _observe(ticket, mine, error if mine else None)
    if outcome == "working":
        lines.append("alarm-repair: working %s" % tid)
        return False
    if outcome == "waiting":
        lines.append("alarm-repair: waiting %s" % tid)
        return False
    prev = ticket.get("status")
    _drop(active, tid)
    if outcome == "failed":
        _record_failure(ticket, now_s, error if mine else None, cap, lines)
        _append_status(beam_path, ticket, now_s, prev)
        info = _info(ticket)
        beam.append_ticket_event(beam_path, ticket, {"at": now_s, "type": "alarm-repair", "value": "failed",
                                                     "attempts": _attempts(ticket), "error": info.get("error")})
        beam.journal(beam_path, {"type": "alarm-repair", "action": "failed", "id": tid, "attempts": _attempts(ticket),
                                 "error": info.get("error")})
        if _attempts(ticket) >= cap:
            _herald_give_up(ticket, lines)
        else:
            lines.append("herald: %s lock-escape repair failed (attempt %d of %d). It tries again in alarmRepairMinutes."
                         % (tid, _attempts(ticket), cap))
        return True
    info = _info(ticket)
    info["state"] = "passed"
    info["passedAt"] = now_s
    ticket["updatedAt"] = now_s
    beam.append_ticket_event(beam_path, ticket, {"at": now_s, "type": "alarm-repair", "value": "passed", "attempts": _attempts(ticket)})
    beam.journal(beam_path, {"type": "alarm-repair", "action": "passed", "id": tid, "attempts": _attempts(ticket)})
    lines.append("alarm-repair: passed %s" % tid)
    return True


def _resolve_naming(data: dict, tid: str, lines: list) -> bool:
    """The naming Shuttle returned. With paths, the ticket starts this call (start_ready); without, it waits."""
    repair = _repair_state(data)
    _drop(_naming(repair), tid)
    ticket = (data.get("tickets") or {}).get(tid)
    if isinstance(ticket, dict) and not _escaped(ticket):
        lines.append("alarm-repair: unnamed %s" % tid)
    return True


def _interval_elapsed(repair: dict, now_dt, minutes: int) -> bool:
    stamp = repair.get("checkedAt")
    if not stamp:
        return True
    seen = beam.parse_ts(stamp)
    if seen is None:
        return True
    return (now_dt - seen).total_seconds() / 60.0 >= minutes


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
    repair.pop("tried", None)
    repair.pop("passOpen", None)
    repair.pop("passStartedAt", None)
    lines: list = []
    flipped_gates: list = []
    sent: list = []
    changed = False
    # 1. Settle every repair Shuttle that is out.
    for tid in list(_active(repair)):
        changed = _settle(data, beam_path, tid, now_s, cap, returned, error, lines) or changed
    # 2. A naming Shuttle that returned.
    for tid in list(_naming(repair)):
        if returned == tid:
            changed = _resolve_naming(data, tid, lines) or changed
        else:
            lines.append("alarm-repair: naming %s" % tid)
    # The housekeeping that rides on this tick (send-back, stale gates) keeps its interval.
    housekeeping = _interval_elapsed(repair, now_dt, minutes)
    ready_empty = housekeeping and not beam.ready(data)
    # 3. Start every lock-escape repair that can start now: no interval, no one-at-a-time.
    before = len(lines)
    started = start_ready(data, beam_path, now_dt, now_s, cap, minutes, lines)
    if started or any(line.startswith(("alarm-repair: name ", "alarm-repair: gave-up ", "herald: ")) for line in lines[before:]):
        changed = True
    if not _active(repair) and not _naming(repair) and not any(
            line.startswith(("alarm-repair: locked ", "alarm-repair: retry ", "alarm-repair: queued ")) for line in lines):
        # Nothing to repair. `wait` means the send-back and gate housekeeping is not due yet.
        lines.append("alarm-repair: idle" if housekeeping else "alarm-repair: wait")
    if housekeeping:
        repair["checkedAt"] = now_s
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
            beam.journal(beam_path, {"type": "send-back", "id": action.get("id"), "outcome": action.get("outcome"), "check": "make ci"})
        if flipped_gates:
            for gate in flipped_gates:
                beam.journal(beam_path, {"type": "gate", "key": gate.get("key"), "status": "green", "evidence": gate.get("evidence")})
            try:
                beam.write_board_files(beam_path, data)
            except Exception as exc:
                print("board: not rewritten (%s)" % exc)
        herald = [line[len("herald: "):] for line in lines if line.startswith("herald: ")]
        if herald:
            beam.atomic_write(beam_path.parent / RECORD_NAME, json.dumps({"at": now_s, "lines": herald}, indent=2) + "\n")
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
    """Settle repairs that are out and start every one that can start. Safe to call twice."""
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
    cmd = sub.add_parser("next", help="settle and start lock-escape repairs")
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
