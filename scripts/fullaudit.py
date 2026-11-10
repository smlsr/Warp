#!/usr/bin/env python3
"""The hourly full audit (1.7.21): every ticket Warp is not finished with, checked against what is really happening,
fixed where it can be, and listed with its issues.

`cadence.fullAudit` (default 3600 s) runs it from `wake.py run`; `wake.py full-audit` runs it now.

For every ticket that is QUEUED, BLOCKED, PARKED, EXTERNAL, in ALARM, waiting on BUGBOT, IN PROGRESS (claimed,
planning, coding, fix, recovering, review, ready, merging), it compares the beam with GitHub (REST, the pull request,
its CI, Bugbot and mergeable state), the agents (a live Shuttle or owner), ownership, and Jira, and acts:

  merged on GitHub, not on the beam       recorded merged (its dependents start)
  closed unmerged while being worked      queued again
  working, no live Shuttle, no activity   restarted: a fix round from the remote head when its pull request is open,
                                          else queued for a fresh Shuttle; an owned pull request is told to its owner
  Bugbot never ran on the head            `/review-bugbot <pr>` is called for it (bugbotManualCommand), once per head
  red CI with no fix in flight            one fix round with every red check and finding
  dirty against its base                  a rebase (or the owner told)
  green, Bugbot passed, mergeable, idle   queued for the merge
  no owner                                Warp's own: recorded; else given to the least-loaded coordinator
  parked                                  looked at again (merged, closed, blockers merged, outside owner)
  lock-escape                             widened when no in-flight ticket holds the paths (wake.unstick)
  Jira                                    one search of every key, compared with what the beam says
It writes `.warp/fullaudit/<ts>.md` (every ticket, its real state, its issues and what was done) and posts a short
summary. Pull requests a coordinator owns are never worked by Warp here; their owner gets the instruction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

WORKING = {"claimed", "planning", "coding", "fix", "recovering"}
REVIEWING = {"review", "bugbot_running", "ready", "merging"}
WAITING = {"queued", "blocked", "parked", "external", "alarm", "skipped"}
JIRA_FOR = {"queued": "To Do", "blocked": "To Do", "claimed": "In Progress", "planning": "In Progress",
            "coding": "In Progress", "fix": "In Progress", "recovering": "In Progress", "review": "In Review",
            "bugbot_running": "In Review", "ready": "In Review", "merging": "In Review", "merged": "Done"}


def _pr_number(ticket: dict) -> str:
    match = re.search(r"/pull/(\d+)", str(((ticket or {}).get("pr") or {}).get("url") or ""))
    return match.group(1) if match else ""


def _minutes_since(stamp: str, now_dt) -> Optional[float]:
    import beam as beam_mod

    ts = beam_mod.parse_ts(str(stamp or ""))
    return None if ts is None else (now_dt - ts).total_seconds() / 60


def _last_activity(ticket: dict) -> str:
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    stamps = [str(ticket.get(k) or "") for k in ("lastSeenAt", "heartbeatAt", "updatedAt", "workerStartedAt")]
    stamps += [str(shuttle.get(k) or "") for k in ("heartbeatAt", "startedAt")]
    stamps += [str(((ticket.get("pr") or {}).get(k)) or "") for k in ("pushedAt", "headAt")]
    return max([s for s in stamps if s] or [""])


def check(data: dict, beam_path: Path, snaps: dict, live: Callable, now=None, stale_minutes: int = 45) -> list:
    """[(row, lines)] for every ticket Warp is not finished with. Mutates the beam in memory (the caller saves it).
    snaps: {id: provider snapshot}; live(ticket) -> True when a Shuttle or its owner is working on it."""
    import beam as beam_mod
    import bugbot
    import orchestrator
    import ownership
    import pipeline
    import terminal
    import unpark

    now_dt, now_s = beam_mod.coerce_now(now)
    cfg = data.get("config") or {}
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    out = []
    for tid in sorted(tickets):
        ticket = tickets[tid]
        if not isinstance(ticket, dict) or terminal.is_terminal(ticket):
            continue
        status = str(ticket.get("status") or "queued")
        pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
        snap = snaps.get(tid) or {}
        number = _pr_number(ticket)
        owner = ownership.hands_off(data, ticket, beam_path)
        is_live = bool(live(ticket))
        idle = _minutes_since(_last_activity(ticket), now_dt)
        row = {"id": tid, "status": status, "phase": ticket.get("phase") or "", "pr": number,
               "github": str(snap.get("prState") or ("" if not number else "?")), "ci": str(snap.get("ci") or pr.get("ci") or ""),
               "bugbot": str(snap.get("bugbot") or pr.get("bugbot") or ""),
               "mergeable": str(snap.get("mergeStateStatus") or pr.get("mergeStateStatus") or ""),
               "owner": owner or ("warp" if number else ""), "live": is_live,
               "idle": "" if idle is None else "%dm" % idle, "jira": ticket.get("jiraKey") or "",
               "issues": [], "actions": []}
        lines: list = []
        state = row["github"].upper()
        # Merged or closed on GitHub.
        if state == "MERGED":
            row["issues"].append("merged on GitHub, %s on the beam" % status)
            pipeline.finish_merge(data, ticket, lines, sha=str(snap.get("mergeSha") or ""), how="found merged by the hourly audit")
            row["actions"].append("recorded merged")
            out.append((row, lines))
            continue
        if status == "parked" or status == "external":
            moved = unpark.reconsider(data, ticket, snap, lines, now_s)
            if moved:
                row["actions"].append("reconsidered: %s" % moved)
            else:
                row["issues"].append("parked: %s" % (ticket.get("parkReason") or ticket.get("lastParkReason") or "no reason recorded")
                                     if status == "parked" else "external: owned by %s" % (ticket.get("externalOwner") or "?"))
            out.append((row, lines))
            continue
        if state == "CLOSED" and status in WORKING | REVIEWING:
            row["issues"].append("its pull request #%s was closed unmerged" % number)
            if owner:
                lines.extend(_tell(data, beam_path, owner, "#%s was closed without a merge; reopen it or tell Warp the "
                                   "ticket is done." % number))
                row["actions"].append("told %s" % owner)
            else:
                unpark._reopen(ticket, "pull request #%s closed unmerged (hourly audit)" % number, now_s)
                row["actions"].append("queued again")
            out.append((row, lines))
            continue
        if status == "alarm":
            row["issues"].append("alarm: %s" % (ticket.get("alarm") or "?"))
        # Work with nobody working on it.
        if status in WORKING and not is_live and (idle is None or idle >= stale_minutes):
            row["issues"].append("stalled: no live Shuttle and no activity for %s" % (row["idle"] or "a long time"))
            if owner:
                lines.extend(_tell(data, beam_path, owner, "%s (#%s) has had no push or report for %s. Push what you have "
                                   "(one push for every open check and finding) or say what blocks it." % (tid, number or "-", row["idle"])))
                row["actions"].append("nudged %s" % owner)
            elif number and state in {"OPEN", "?", ""}:
                pipeline.begin_fix(data, ticket, ["stalled: no live Shuttle and no activity for %s. Fetch the remote head "
                                                  "first and continue from it; push one product commit" % row["idle"]], lines)
                row["actions"].append("restarted as a fix round from the remote head")
            else:
                _requeue(ticket)
                row["actions"].append("queued for a new Shuttle")
            out.append((row, lines))
            continue
        if number and state in {"OPEN", ""} and status in REVIEWING | {"alarm"}:
            head = str(snap.get("headSha") or pr.get("headSha") or "")
            reasons = pipeline.red_reasons(ticket, cfg)
            ran = str(pr.get("bugbotSha") or "") == head or row["bugbot"] in {"pass", "fail"}
            if orchestrator.bugbot_applies(ticket, cfg) and not ran and not is_live:
                row["issues"].append("Bugbot has not run on %s" % head[:12])
                if str(pr.get("auditBugbotAsked") or "") != head:
                    pr["auditBugbotAsked"] = head
                    lines.append("bugbot: MUST DO %s: Bugbot has not run on #%s (head %s). Run `%s` now." % (
                        tid, number, head[:12], bugbot.manual_command(data, pr.get("url") or "")))
                    row["actions"].append("called %s" % bugbot.manual_command(data, pr.get("url") or ""))
            if reasons and not is_live and not pipeline.pending(ticket):
                row["issues"].append("red: %s" % "; ".join(str(r) for r in reasons[:4]))
                pipeline.begin_fix(data, ticket, list(reasons), lines)
                row["actions"].append("%s for a fix round" % ("told the owner" if owner else "sent back"))
            elif str(row["mergeable"]).upper() in {"DIRTY", "CONFLICTING"} and not is_live:
                row["issues"].append("dirty against its base")
                pipeline.start_rebase(data, ticket, lines)
                row["actions"].append("rebase %s" % ("asked of its owner" if owner else "started"))
            elif not reasons and pipeline.bugbot_finished(ticket, cfg) and orchestrator.checks_green(ticket, cfg) \
                    and ticket.get("phase") not in {"ready", "merging"} and not owner:
                row["issues"].append("green and reviewed, but not queued for the merge")
                pipeline._phase(ticket, "ready", lines)
                pipeline._hold_or_queue(ticket, lines)
                row["actions"].append("queued for the merge")
        if number and not owner and not (data.get("prOwners") or {}).get(number):
            row["issues"].append("no recorded owner")
            import relay

            relay.set_owner(data, number, tid, agent=str(ticket.get("agent") or ""), coordinator="warp", now_s=now_s)
            row["actions"].append("recorded Warp as its owner")
        if status in {"queued", "blocked"}:
            blockers = [d for d in orchestrator.blockers(ticket)
                        if not (isinstance(tickets.get(d), dict) and terminal.is_terminal(tickets[d]))]
            if blockers:
                row["issues"].append("waits on %s" % ", ".join(blockers[:6]))
        out.append((row, lines))
    return out


def _requeue(ticket: dict) -> None:
    ticket["status"] = "queued"
    ticket.pop("phase", None)
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    for key in ("pending", "step", "startedAt", "launching", "launchLines"):
        shuttle.pop(key, None)


def _tell(data: dict, beam_path: Path, owner: str, text: str) -> list:
    try:
        import relay

        return relay.send(Path(beam_path), data, owner, text)
    except Exception as exc:
        return ["relay: not sent to %s (%s)" % (owner, exc)]


def jira_lines(beam_path: Path, data: dict, rows: list) -> list:
    """One search of every key, with what the beam expects, written to .warp/fullaudit/jira-expected.json."""
    expected = {}
    for row in rows:
        ticket = (data.get("tickets") or {}).get(row["id"]) or {}
        key = str(ticket.get("jiraKey") or "")
        want = JIRA_FOR.get(str(ticket.get("status") or ""))
        if key and want:
            expected[key] = {"ticket": row["id"], "expected": want}
    if not expected:
        return []
    folder = Path(beam_path).parent / "fullaudit"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "jira-expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    keys = sorted(expected)
    import jira_sync

    return ["jira: MUST DO audit %d issues: search `key in (%s)` with fields [\"status\"], compare each status with "
            ".warp/fullaudit/jira-expected.json, and for every one that differs run python3 %s/jira_sync.py catchup "
            "--beam %s --id <ticket>." % (len(keys), ", ".join(keys[:100]) + (" …" if len(keys) > 100 else ""),
                                          Path(jira_sync.__file__).resolve().parent, Path(beam_path).resolve())]


def report(beam_path: Path, rows: list, now=None) -> tuple:
    import beam as beam_mod
    import selfdrive

    now_s = beam_mod.coerce_now(now)[1]
    path = selfdrive.report_path(beam_path, "fullaudit", now_s)
    with_issues = [r for r in rows if r["issues"]]
    acted = [r for r in rows if r["actions"]]
    by_status: dict = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    body = ["# Full audit %s" % now_s, "",
            "%d tickets not finished (%s); %d with issues; %d acted on." % (
                len(rows), ", ".join("%s %d" % kv for kv in sorted(by_status.items())), len(with_issues), len(acted)), "",
            "| ticket | beam | PR | GitHub | CI | Bugbot | mergeable | owner | live | idle | Jira | issues | actions |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        body.append("| %s | %s%s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["id"], r["status"], ("/" + r["phase"]) if r["phase"] else "", ("#" + r["pr"]) if r["pr"] else "-",
            r["github"] or "-", r["ci"] or "-", r["bugbot"] or "-", r["mergeable"] or "-", r["owner"] or "-",
            "yes" if r["live"] else "no", r["idle"] or "-", r["jira"] or "-",
            "; ".join(r["issues"]).replace("|", "/") or "-", "; ".join(r["actions"]).replace("|", "/") or "-"))
    path.write_text("\n".join(body) + "\n")
    summary = "Full audit: %d tickets not finished (%s); %d with issues, %d fixed or restarted. Report %s" % (
        len(rows), ", ".join("%s %d" % kv for kv in sorted(by_status.items())), len(with_issues), len(acted),
        path.relative_to(Path(beam_path).parent.parent))
    return path, summary


def run(beam_path: Path, env=None, now=None, snaps: Optional[dict] = None, live: Optional[Callable] = None) -> list:
    """The cadence: unstick (widen lock-escapes, auto-scan), check every ticket, act, save, report."""
    import agents
    import beam as beam_mod
    import beamlock
    import pipeline
    import wake

    path = Path(beam_path)
    out = []
    try:
        out.extend(wake.unstick(path, now=now))
    except Exception as exc:
        out.append("fullaudit: unstick skipped (%s)" % exc)
    data = beam_mod.load_json(path)
    before = {tid: json.dumps(t, sort_keys=True) for tid, t in (data.get("tickets") or {}).items()}
    data["_repoRoot"] = str(path.resolve().parent.parent)
    data["_live"] = True
    if snaps is None:
        try:
            snaps = pipeline.live_provider(data, path)
        except Exception as exc:
            snaps = {}
            out.append("fullaudit: GitHub not read (%s)" % exc)
    live = live or (lambda t: agents.shuttle_busy(data, t, now=now, beam_path=path))
    stale = int((data.get("config") or {}).get("staleMinutes") or 45)
    results = check(data, path, snaps, live, now=now, stale_minutes=max(stale, 30))
    rows = [r for r, _l in results]
    for _r, lines in results:
        out.extend(lines)
    out.extend(jira_lines(path, data, rows))
    data.pop("_live", None)
    data.pop("_repoRoot", None)
    with beamlock.run_lock(path):
        disk = beam_mod.load_json(path)
        for tid, ticket in (data.get("tickets") or {}).items():
            if json.dumps(ticket, sort_keys=True) != before.get(tid) and tid in (disk.get("tickets") or {}):
                disk["tickets"][tid] = ticket
        for key in ("prOwners", "said", "mergedPrs"):
            if key in data:
                disk[key] = data[key]
        beam_mod.atomic_write(path, json.dumps(disk, indent=2) + "\n", data=disk)
    report_path, summary = report(path, rows, now=now)
    out.append("fullaudit: %s" % summary)
    out.append("herald: %s" % summary)
    return out
