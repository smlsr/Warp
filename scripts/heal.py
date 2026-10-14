#!/usr/bin/env python3
"""What /warp-start and /warp-resume do with tickets in alarm, so no one has to post warp:retry.

A start or resume is the person saying "carry on". Each alarmed ticket is put back on a
path that can finish, with a fresh budget, and one line says what was done:

  lock-escape  Every escaped path already inside the ticket's locks (a repair widened
               them, or the alarm was left over from an earlier event): the alarm is
               cleared. With an open pull request the ticket goes back to review; the
               next pass asks Bugbot and checks again. Without one it is queued.
               Paths still outside the locks: they are added now when no ticket in
               flight holds them, and the ticket goes the same way. When one is held,
               the repair count starts again and the parent's repair widens it once
               the holder is done.
  gate-red     Left alone. The gate's own check is red; fix the base or the check.
  any other    worker-died, stuck, spawn-cap, stalled, ci-red, bugbot-failed, failed:
               the same as warp:retry. An open pull request goes to `fix` on its
               branch, otherwise `queued`; the counts start again (old ones on retryLog).

Parked tickets stay parked: parking is a cap someone may want to look at. warp:retry still
works on them.

  python3 scripts/heal.py --beam .warp/beam.json            heal now (start and resume run this)
  python3 scripts/heal.py --beam .warp/beam.json --dry-run  print what it would do
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beam as beam_mod  # noqa: E402

LEAVE = {"gate-red"}


def _paths(ticket: dict) -> tuple:
    import ticket_state

    escaped = ticket_state._escaped(ticket.get("escaped"))
    return escaped, ticket_state.uncovered(ticket, escaped)


def plan(data: dict) -> list:
    """(ticket id, action, detail) for each alarmed ticket. Does not change anything."""
    import alarm_repair

    import orchestrator

    rows = []
    for tid, ticket in (data.get("tickets") or {}).items():
        if not isinstance(ticket, dict) or ticket.get("status") in orchestrator.SETTLED:
            continue
        alarm = str(ticket.get("alarm") or "")
        # A lock-escape can sit on a ticket that is still moving (a repair or restart
        # Shuttle out). Every other alarm is healed only once the ticket stopped on it.
        if not alarm or (ticket.get("status") != "alarm" and alarm != "lock-escape"):
            continue
        if alarm in LEAVE:
            rows.append((tid, "leave", alarm))
            continue
        if alarm == "lock-escape":
            _escaped, missing = _paths(ticket)
            if not missing:
                rows.append((tid, "clear", "escaped paths already locked"))
                continue
            busy = alarm_repair._holder(data, ticket, missing)
            if busy:
                rows.append((tid, "repair", "%s held by %s" % (", ".join(missing), busy)))
            else:
                rows.append((tid, "widen", ", ".join(missing)))
            continue
        rows.append((tid, "retry", alarm or "alarm"))
    return rows


def _clear_lock_escape(data: dict, ticket: dict, now_s: str, widened: list, now=None) -> str:
    import alarm_repair
    import inbound
    import orchestrator

    tid = ticket.get("id")
    if widened:
        alarm_repair._widen(ticket, widened)
    ticket["alarm"] = None
    ticket["escaped"] = []
    ticket["alarmRepairs"] = 0
    info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
    info.update({"state": "cleared", "clearedAt": now_s, "gaveUp": False})
    ticket["alarmRepair"] = info
    repair = data.get("alarmRepair") if isinstance(data.get("alarmRepair"), dict) else {}
    if repair.get("active") == tid:
        repair["active"] = None
    if repair.get("naming") == tid:
        repair["naming"] = None
    added = " Added %s to its locks." % ", ".join(widened) if widened else ""
    if ticket.get("status") != "alarm" and orchestrator.shuttle_is_live(ticket, data, now=now):
        # A Shuttle is still out on it. Only the alarm goes; the Shuttle's return moves it on.
        ticket["updatedAt"] = now_s
        return "heal: %s lock-escape cleared; its %s Shuttle is still out.%s" % (
            tid, (ticket.get("shuttle") or {}).get("step") or "", added)
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else None
    if shuttle is not None:
        shuttle["pending"] = False
    watch = ticket.get("watch") if isinstance(ticket.get("watch"), dict) else {}
    for key in ("fixerAttempts", "seenAlarm", "stalled", "stallReason"):
        watch.pop(key, None)
    ticket["stalled"] = False
    ticket["needsReplacement"] = False
    ticket["agent"] = None
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    if inbound.retry_mode(ticket) == "fix":
        # The pull request is fine as far as the locks go. Back to review: the next
        # pass reads its checks and asks Bugbot for this head if it has not answered.
        ticket["status"] = "review"
        ticket["phase"] = "reviewing"
        where = "back to review on %s" % pr.get("url")
    else:
        ticket["status"] = "queued"
        ticket.pop("phase", None)
        where = "queued; the next claim reuses %s" % (ticket.get("branch") or "its branch")
    ticket["updatedAt"] = now_s
    return "heal: %s lock-escape cleared, %s.%s" % (tid, where, added)


def check_merged(beam_path: Path, fetch=None) -> list:
    """Tickets recorded merged that the provider shows are not: put them back to ready-to-merge.

    Before 1.6.12 a live pass with no merge result from the provider recorded the merge anyway, so a
    ticket could read merged (and clear its gate) while its pull request was still open. Each merged
    ticket with a pull request and no confirmed merge is checked once; a confirmed one is marked.
    """
    import provider as provider_mod

    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return []
    data = beam_mod.load_json(beam_path)
    root = beam_path.resolve().parent.parent
    fetch = fetch or (lambda url: provider_mod.fetch_pr_state(root, url))
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    lines, reopened, changed = [], [], False
    for tid, ticket in tickets.items():
        if not isinstance(ticket, dict) or ticket.get("status") != "merged":
            continue
        pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
        if not pr.get("url") or pr.get("mergeConfirmed"):
            continue
        snap = fetch(pr["url"])
        state = str((snap or {}).get("prState") or "").upper()
        if state == "MERGED":
            pr["mergeConfirmed"] = True
            if snap.get("mergeSha"):
                pr["sha"] = snap["mergeSha"]
            changed = True
            continue
        if state not in {"OPEN", "CLOSED"}:
            continue  # the provider could not be read; leave it
        ticket["status"] = "review"
        ticket["phase"] = "ready"
        for key in ("sha", "mergeRequestedSha", "mergeRequestedAt", "mergeAttempt", "mergeAnnounced"):
            pr.pop(key, None)
        reopened.append(tid)
        changed = True
        lines.append("heal: %s was recorded merged, but %s is %s on the provider. Back to ready to merge; the next pass merges it."
                     % (tid, pr["url"], state.lower()))
    if reopened:
        for gate in data.get("gates") or []:
            if isinstance(gate, dict) and gate.get("status") == "green" and set(gate.get("members") or []) & set(reopened):
                gate["status"] = "pending"
                gate["evidence"] = None
                gate["greenAt"] = None
                lines.append("heal: gate %s is pending again: %s is not merged." % (gate.get("key"), ", ".join(sorted(set(gate.get("members") or []) & set(reopened)))))
    if changed:
        data["metrics"] = beam_mod.metrics(data)
        beam_mod.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
        if reopened:
            beam_mod.journal(beam_path, {"type": "heal", "unmerged": reopened})
    return lines


def heal(beam_path: Path, dry_run: bool = False, now=None) -> list:
    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return []
    data = beam_mod.load_json(beam_path)
    rows = plan(data)
    if not rows:
        return []
    if dry_run:
        return ["heal: would %s %s (%s)" % (action, tid, detail) for tid, action, detail in rows]
    import inbound

    _now_dt, now_s = beam_mod.coerce_now(now)
    lines = []
    retry = []
    for tid, action, detail in rows:
        ticket = data["tickets"][tid]
        if action == "leave":
            lines.append("heal: %s left in alarm (%s). Fix the gate's check; the gate reruns it." % (tid, detail))
        elif action in {"clear", "widen"}:
            widened = _paths(ticket)[1] if action == "widen" else []
            lines.append(_clear_lock_escape(data, ticket, now_s, widened, now=now))
            beam_mod.append_ticket_event(beam_path, ticket, {"at": now_s, "type": "heal", "value": "lock-escape cleared"})
        elif action == "repair":
            ticket["alarmRepairs"] = 0
            info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
            info.update({"gaveUp": False, "gaveUpPosted": False})
            ticket["alarmRepair"] = info
            lines.append("heal: %s lock-escape kept: %s. Repair count reset; the parent's repair widens it when the holder is done." % (tid, detail))
        else:
            retry.append(tid)
    if any(action != "retry" for _tid, action, _d in rows):
        data["metrics"] = beam_mod.metrics(data)
        beam_mod.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
    for tid in retry:
        text = inbound._requeue(beam_path, tid, by="resume")
        lines.append("heal: " + text.splitlines()[0])
    beam_mod.journal(beam_path, {"type": "heal", "tickets": [tid for tid, _a, _d in rows]})
    return lines


HELP = __doc__.split("\n\n", 1)[1] + "\n?, help, -h, and --help print this text. Quote ? if the shell expands it.\n"


def main() -> None:
    p = argparse.ArgumentParser(description="Put alarmed tickets back on a path that can finish", epilog=HELP,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--beam", default=".warp/beam.json")
    p.add_argument("--dry-run", action="store_true")
    import usage

    args = p.parse_args(usage.normalize_argv(None))
    lines = heal(Path(args.beam), dry_run=bool(args.dry_run))
    for line in lines or ["heal: nothing in alarm"]:
        print(line)


if __name__ == "__main__":
    main()
