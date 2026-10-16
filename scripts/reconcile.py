#!/usr/bin/env python3
"""Reconcile the beam with what the workers did, at the start of every pass, and act on what that shows.

The beam says which tickets have a Shuttle out. The workers say otherwise in four places: the journal
(`subagent-end`, written by the subagentStop hook and by `checkout.py result`), the agent registry
(`.warp/agents.json`), the ticket folders (read by `ticket_state.observe` just before this), and the ticket's
own status. Before 1.7.1 a Shuttle that ended without writing `done` to its folder (D-01: exited without a
pull request) was only noticed when its heartbeat went stale, `staleMinutes` (15) later. Each pass now:

  1. Ended without a return: the journal or the registry shows the ticket's Shuttle ended after it was
     launched, and the beam still has it out. It is marked returned, so this same pass handles the return:
     a missing pull request is opened (or sent back to fix), a review goes on, a failed end restarts the step.
  2. A repair Shuttle that returned: its lock-escape repair is settled now (`alarm_repair next --returned`),
     not when the parent remembers to.
  3. Settled with a Shuttle out: a merged, done, parked, or skipped ticket still marked as having a Shuttle
     out frees that slot.
  4. Orphan locks: a settled ticket that still lists locks drops them, so the tickets that wait on them start.
  5. Idle repair backoff: when no Shuttle is out at all and a lock-escape repair waits only on
     `alarmRepairMinutes`, the wait is waived and the repair starts now.

Every finding prints one `reconcile:` line; a pass with none prints `reconcile: ok (<n> Shuttles out)`.

  python3 scripts/reconcile.py --beam .warp/beam.json            what supervise runs (and saves)
  python3 scripts/reconcile.py --beam .warp/beam.json --dry-run  list without changing anything

?, help, -h, and --help print this text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

HELP = __doc__
JOURNAL_TAIL = 2000
FAILED_ENDS = {"error", "errored", "failed", "failure", "aborted", "cancelled", "canceled", "crashed", "died", "dead"}


def _journal_ends(beam_path: Optional[Path]) -> dict:
    """ticket id -> the newest `subagent-end` row for a Shuttle on it."""
    if beam_path is None:
        return {}
    path = Path(beam_path).parent / "journal.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 400 * JOURNAL_TAIL))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return {}
    out = {}
    for raw in text.splitlines()[-JOURNAL_TAIL:]:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "subagent-end":
            continue
        tid = str(row.get("ticket") or "")
        if tid and str(row.get("role") or "shuttle") in {"shuttle", ""}:
            out[tid] = row
    return out


def _started(ticket: dict):
    import beam as beam_mod

    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    return beam_mod.parse_ts(str(shuttle.get("startedAt") or ticket.get("workerStartedAt") or ""))


def _registry_ended(doc: dict, ticket: dict, started) -> Optional[dict]:
    """The ended registry row of this ticket's Shuttle, when no row for it is live."""
    import agents
    import beam as beam_mod

    tid = str(ticket.get("id") or "")
    rows = [row for row in agents._rows(doc) if isinstance(row, dict) and str(row.get("ticket") or "") == tid
            and row.get("role") in {"shuttle", None, ""}]
    if not rows or any(row.get("state") in agents._LIVE and not row.get("ended") for row in rows):
        return None
    current = str(ticket.get("agent") or "")
    for row in reversed(rows):
        if current and str(row.get("id") or "") != current:
            continue
        ended = beam_mod.parse_ts(str(row.get("ended") or ""))
        if ended is not None and started is not None and ended >= started and row.get("endReason") not in {"replaced", "dead"}:
            return row
    return None


def mark_returned(ticket: dict, when: str, failed: bool = False, why: str = "") -> bool:
    """Mark the Shuttle that is out as returned. False when there is none or it already returned."""
    import orchestrator

    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else None
    if not shuttle or not shuttle.get("pending") or orchestrator.shuttle_returned(ticket):
        return False
    shuttle["returned"] = True
    shuttle["returnedAt"] = when
    if failed:
        shuttle["failed"] = True
    if why:
        shuttle["endedBy"] = why
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else None
    if pr is None:
        pr = {}
        ticket["pr"] = pr
    pr["shuttleReturned"] = True
    return True


def _ended_without_return(data: dict, beam_path: Optional[Path], now_s: str, lines: list, dry: bool) -> list:
    import agents
    import beam as beam_mod
    import orchestrator

    ends = _journal_ends(beam_path)
    doc = agents.registry(data, beam_path) if beam_path is not None else {}
    marked = []
    for tid, ticket in sorted((data.get("tickets") or {}).items()):
        if not isinstance(ticket, dict) or ticket.get("status") in orchestrator.SETTLED:
            continue
        shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
        if not shuttle.get("pending") or shuttle.get("launching") or orchestrator.shuttle_returned(ticket):
            continue
        started = _started(ticket)
        if started is None:
            continue
        why, failed, at = "", False, now_s
        end = ends.get(str(tid))
        end_at = beam_mod.parse_ts(str((end or {}).get("ts") or "")) if end else None
        if end_at is not None and end_at >= started:
            status = str(end.get("status") or "").strip().casefold()
            failed = status in FAILED_ENDS
            why = "its Agent ended%s" % (" (%s)" % status if status else "")
            at = str(end.get("ts"))
        else:
            row = _registry_ended(doc, ticket, started)
            if row is not None:
                why = "the agent registry shows it ended (%s)" % (row.get("endReason") or "ended")
                at = str(row.get("ended"))
        if not why:
            continue
        step = shuttle.get("step") or "?"
        if dry:
            lines.append("reconcile: would mark %s returned (step=%s): %s" % (tid, step, why))
            continue
        if mark_returned(ticket, at, failed=failed, why=why):
            marked.append(str(tid))
            lines.append("reconcile: %s Shuttle ended without writing done (step=%s): %s. Handling its return now%s."
                         % (tid, step, why, ", as a failed step" if failed else ""))
    return marked


def _returned_repairs(data: dict) -> list:
    import alarm_repair
    import orchestrator

    repair = data.get("alarmRepair") if isinstance(data.get("alarmRepair"), dict) else {}
    out = []
    for tid in alarm_repair._active(dict(repair)):
        ticket = (data.get("tickets") or {}).get(tid)
        if not isinstance(ticket, dict):
            continue
        info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
        if info.get("state") == "working" and info.get("launchedAt") and orchestrator.shuttle_returned(ticket):
            out.append(tid)
    return out


def _settled_with_shuttle(data: dict, lines: list, dry: bool) -> None:
    import orchestrator
    import pipeline

    for tid, ticket in sorted((data.get("tickets") or {}).items()):
        if not isinstance(ticket, dict) or ticket.get("status") not in orchestrator.SETTLED:
            continue
        if not pipeline.pending(ticket):
            continue
        lines.append("reconcile: %s is %s but still had a Shuttle marked out; %s its slot"
                     % (tid, ticket.get("status"), "would free" if dry else "freed"))
        if not dry:
            pipeline.clear_shuttle(ticket)
            (ticket.get("shuttle") or {}).pop("launching", None)


def _orphan_locks(data: dict, lines: list, dry: bool) -> None:
    import orchestrator

    for tid, ticket in sorted((data.get("tickets") or {}).items()):
        if not isinstance(ticket, dict) or ticket.get("status") not in orchestrator.SETTLED:
            continue
        locks = [item for item in (ticket.get("locks") or []) if str(item).strip()]
        if not locks:
            continue
        lines.append("reconcile: %s is %s but still held %d lock%s; %s" % (
            tid, ticket.get("status"), len(locks), "" if len(locks) == 1 else "s", "would release" if dry else "released"))
        if not dry:
            import lockkeep

            lockkeep.release(ticket)  # kept on the ticket: a retry gives them back (1.7.3)
            lines.append("lock: remove %s" % tid)


def _restore_locks(data: dict, beam_path: Optional[Path], lines: list, dry: bool) -> None:
    """A ticket back at work (or queued to start) with no locks gets the ones it released, or the plan's."""
    import lockkeep
    import orchestrator

    for tid, ticket in sorted((data.get("tickets") or {}).items()):
        if not isinstance(ticket, dict) or ticket.get("status") in orchestrator.SETTLED:
            continue
        if [p for p in (ticket.get("locks") or []) if str(p).strip()]:
            continue
        if dry:
            probe = dict(ticket)
            said = lockkeep.restore(probe, beam_path)
            if said:
                lines.append("reconcile: would restore: " + said)
            continue
        said = lockkeep.restore(ticket, beam_path)
        if said:
            lines.append("reconcile: %s is %s with no locks (a park or settle released them); %s"
                         % (tid, ticket.get("status"), said[len("locks: "):]))


def _phantoms(data: dict, now_s: str, lines: list, dry: bool) -> None:
    """Slots claimed for a Shuttle nobody launched, past the launch grace: said once; they hold no slot."""
    import orchestrator

    for tid, ticket in sorted((data.get("tickets") or {}).items()):
        if not isinstance(ticket, dict) or not orchestrator.phantom(ticket, data, now_s):
            continue
        shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
        info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
        mark = "%s|%s" % (shuttle.get("markedAt") or "", info.get("startedAt") or "")
        if ticket.get("phantomNoted") == mark:
            continue
        what = "a lock-escape repair" if not shuttle.get("pending") else "a Shuttle (%s)" % (shuttle.get("step") or "work")
        lines.append("reconcile: %s was claimed for %s that was never launched; it no longer holds a Shuttle slot "
                     "(the start line is printed again until it is launched)" % (tid, what))
        if not dry:
            ticket["phantomNoted"] = mark
            if shuttle.get("pending"):
                ticket["shuttle"]["phantom"] = True
            else:
                info["phantom"] = True


def _waive_idle_backoff(data: dict, beam_path: Optional[Path], now_dt, now_s: str, lines: list, dry: bool) -> None:
    import alarm_repair
    import beam as beam_mod
    import orchestrator

    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    if any(isinstance(t, dict) and (orchestrator.shuttle_is_live(t, data, now=now_s) or orchestrator.repair_awaiting_launch(t))
           for t in tickets.values()):
        return
    try:
        minutes = beam_mod.config_int(data, beam_path, "alarmRepairMinutes", alarm_repair.DEFAULT_ALARM_REPAIR_MINUTES)
    except Exception:
        minutes = alarm_repair.DEFAULT_ALARM_REPAIR_MINUTES
    for tid, ticket in sorted(tickets.items()):
        if not isinstance(ticket, dict) or ticket.get("status") != "alarm" or ticket.get("alarm") != alarm_repair.REASON:
            continue
        info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
        if info.get("gaveUp") or not alarm_repair._backoff(ticket, now_dt, minutes):
            continue
        lines.append("reconcile: %s lock-escape repair %s now: no Shuttle is out, so it does not wait out alarmRepairMinutes"
                     % (tid, "would retry" if dry else "retries"))
        if not dry:
            info["backoffWaived"] = now_s


def run(data: dict, beam_path: Optional[Path] = None, now=None, dry: bool = False) -> dict:
    """Mutates data (unless dry). Returns {"lines": [...], "returned": [ids], "repairs": [ids]}."""
    import beam as beam_mod
    import orchestrator

    lines: list = []
    out = {"lines": lines, "returned": [], "repairs": []}
    if (data.get("runState") or "running") in {"paused", "stopped"} or data.get("paused"):
        return out
    now_dt, now_s = beam_mod.coerce_now(now)
    try:
        out["returned"] = _ended_without_return(data, beam_path, now_s, lines, dry)
        out["repairs"] = _returned_repairs(data)
        for tid in out["repairs"]:
            lines.append("reconcile: %s repair Shuttle returned; settling the repair now" % tid)
        _settled_with_shuttle(data, lines, dry)
        _orphan_locks(data, lines, dry)
        _restore_locks(data, beam_path, lines, dry)
        _phantoms(data, now_s, lines, dry)
        _waive_idle_backoff(data, beam_path, now_dt, now_s, lines, dry)
    except Exception as exc:  # reconcile never stops a pass
        lines.append("reconcile: skipped (%s)" % exc)
        return out
    if not lines:
        tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
        live = sum(1 for t in tickets.values() if isinstance(t, dict) and orchestrator.shuttle_is_live(t, data, now=now_s))
        lines.append("reconcile: ok (%d Shuttle%s out, matching the beam)" % (live, "" if live == 1 else "s"))
    return out


def run_on_disk(beam_path: Path, now=None) -> list:
    """What supervise runs, under the run lock it already holds: reconcile, save, then settle returned repairs."""
    import alarm_repair
    import beam as beam_mod

    path = Path(beam_path)
    try:
        data = beam_mod.load_json(path)
    except (OSError, ValueError) as exc:
        return ["reconcile: not read (%s)" % exc]
    before = json.dumps(data, sort_keys=True)
    result = run(data, path, now=now)
    if json.dumps(data, sort_keys=True) != before:
        beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
    lines = list(result["lines"])
    for tid in result["repairs"]:
        try:
            lines.extend(alarm_repair.next_repair(path, now=now, returned=tid))
        except Exception as exc:
            lines.append("reconcile: repair %s not settled (%s)" % (tid, exc))
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile the beam with the workers", epilog=HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--beam", default=".warp/beam.json")
    parser.add_argument("--dry-run", action="store_true")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    path = Path(args.beam)
    if not path.is_file():
        print("reconcile: no beam at %s" % path)
        return 2
    if args.dry_run:
        import beam as beam_mod

        for line in run(beam_mod.load_json(path), path, dry=True)["lines"]:
            print(line)
        return 0
    import beamlock

    with beamlock.run_lock(path):
        for line in run_on_disk(path):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
