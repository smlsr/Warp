#!/usr/bin/env python3
"""What the next pass could do right now, so the parent never sits out a wait while work is ready.

`keys(data)` lists every action a pass would take at once, as `<kind>:<id>`:

  return:<id>    a Shuttle came back and its result is not handled yet
  escape:<id>    a lock-escape alarm a repair can start on
  replace:<id>   a dead Shuttle waiting for its replacement
  fix:<id>       a ticket in fix with no Shuttle out
  merge:<id>     a ticket ready to merge
  ready:<id>     a queued ticket that can start in a free slot

The end of every supervise pass stores its keys on the beam (`pass.work`). `parent-exit --wait` checks every few
seconds and returns `pass: work <key>` as soon as a key appears that the last pass did not have: a Shuttle that
exits without writing its folder, an alarm raised by a launch or a verify, a slot freed by a death, without
waiting out `pollSeconds`. A key the last pass already had (a ticket waiting on a lock) does not wake it again,
so a pass that cannot act yet does not spin.
"""

from __future__ import annotations

import re
from typing import Optional

LIMIT = 40


def keys(data: dict, now=None) -> list:
    import beam as beam_mod
    import orchestrator

    out = []
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    if not tickets or (data.get("runState") or "running") != "running" or data.get("paused"):
        return []
    repair = data.get("alarmRepair") if isinstance(data.get("alarmRepair"), dict) else {}
    active = repair.get("active") if isinstance(repair.get("active"), list) else ([repair["active"]] if repair.get("active") else [])
    for tid, t in tickets.items():
        if not isinstance(t, dict):
            continue
        status = t.get("status")
        shuttle = t.get("shuttle") if isinstance(t.get("shuttle"), dict) else {}
        live = orchestrator.shuttle_is_live(t, data, now=now)
        if status in beam_mod.SHUTTLE_WORK and orchestrator.shuttle_returned(t):
            out.append("return:%s" % tid)
        elif status == "alarm" and t.get("alarm") == "lock-escape" and tid not in active:
            info = t.get("alarmRepair") if isinstance(t.get("alarmRepair"), dict) else {}
            if not info.get("gaveUp") and info.get("state") != "failed":
                out.append("escape:%s" % tid)
        elif t.get("needsReplacement") and not live:
            out.append("replace:%s" % tid)
        elif status == "fix" and not live and not shuttle.get("pending"):
            out.append("fix:%s" % tid)
        elif t.get("phase") == "ready" and status not in {"merged", "done", "awaiting_approval"}:
            out.append("merge:%s" % tid)
        if len(out) >= LIMIT:
            return out
    try:
        for t in beam_mod.ready(dict(data))[: LIMIT - len(out)]:
            out.append("ready:%s" % t.get("id"))
    except Exception:
        pass
    return out


REVIEW_STATUSES = {"review", "bugbot_running", "merging", "awaiting_approval", "fix"}


def review_changed(data: dict, beam_path, lister=None) -> Optional[str]:
    """`review <id>` when an open pull request the run waits on changed since the last pass read it (a new head,
    CI or Bugbot finished, a conflict, merged or closed elsewhere). One `gh pr list` per repo. None otherwise,
    and None when nothing waits on review or gh cannot be asked."""
    from pathlib import Path

    import multirepo
    import provider as provider_mod

    root = Path(beam_path).resolve().parent.parent
    by_repo: dict = {}
    for tid, t in sorted((data.get("tickets") or {}).items()):
        if not isinstance(t, dict) or t.get("status") not in REVIEW_STATUSES:
            continue
        pr = t.get("pr") if isinstance(t.get("pr"), dict) else {}
        if not pr.get("url") or not pr.get("signal"):
            continue
        try:
            repo = multirepo.root_for(data, t, root)
        except Exception:
            repo = root
        by_repo.setdefault(str(repo), []).append((str(tid), str(pr["url"]).rstrip("/"), str(pr["signal"])))
    for repo, rows in by_repo.items():
        live = (lister or provider_mod.review_signals)(Path(repo))
        if live is None:
            continue
        for tid, url, seen in rows:
            now_sig = live.get(url, "gone")  # not open any more: merged or closed elsewhere
            if now_sig != seen:
                return "review %s" % tid
    return None


def new_since_pass(data: dict, now=None) -> Optional[str]:
    """The first key the last pass did not have, or None."""
    seen = set(((data.get("pass") or {}).get("work")) or [])
    for key in keys(data, now=now):
        if key not in seen:
            return key
    return None


RUSH_LIMIT = 5
_ACTED = re.compile(r"^(merge: \S+ merged|pr: opened |alarm-repair: (start|passed)|reconcile: \S+ Shuttle ended without writing done"
                    r"|reconcile: \S+ repair Shuttle returned|reconcile: \S+ lock-escape repair retries|lock: remove )")
_ACTIONABLE = ("return:", "escape:", "replace:", "fix:", "merge:")


def acted(lines: list) -> bool:
    """The pass moved something forward: a merge, a pull request opened, a return handled, a repair started."""
    return any(_ACTED.match(str(line)) for line in lines or [])


def note_pass(data: dict, lines: list) -> list:
    """Record what this pass did and could do next on the beam (`pass.work`, `pass.acted`, `rush`), and say so
    when nothing runs and nothing moves (`stall:`), once in Slack per stall. Called at the end of every pass."""
    work = keys(data)
    moved = acted(lines)
    mark = data.setdefault("pass", {})
    mark["work"] = work
    mark["acted"] = moved
    data["rush"] = int(data.get("rush") or 0) + 1 if moved else 0
    return stall_news(data, lines, work, moved)


def continue_now(data: dict, now=None) -> Optional[str]:
    """`continue <key>` when the last pass moved something and more of the same is ready (one merge per pass, a
    second return to handle): the next pass runs at once instead of after pollSeconds. At most RUSH_LIMIT passes
    in a row, so a pass that keeps acting on the same thing still waits."""
    mark = data.get("pass") if isinstance(data.get("pass"), dict) else {}
    if not mark.get("acted") or int(data.get("rush") or 0) > RUSH_LIMIT:
        return None
    for key in keys(data, now=now):
        if key.startswith(_ACTIONABLE):
            return "continue %s" % key
    return None


def _live_count(data: dict) -> int:
    import orchestrator

    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    # A Shuttle that is only marked (`launching`) is not out: two passes of that is a stall too.
    return sum(1 for t in tickets.values() if isinstance(t, dict) and orchestrator.shuttle_still_out(t, data))


def waiting_line(data: dict) -> str:
    """What every open ticket that has no Shuttle out waits on, by kind."""
    import orchestrator
    import pipeline

    cfg = data.get("config") or {}
    groups: dict = {}
    order = ("Bugbot", "CI", "approval", "merge", "an alarm", "a pull request", "a Shuttle launch")
    for tid, t in sorted((data.get("tickets") or {}).items()):
        if not isinstance(t, dict) or t.get("status") in orchestrator.SETTLED or t.get("status") in {"queued", "blocked"}:
            continue
        shuttle = t.get("shuttle") if isinstance(t.get("shuttle"), dict) else {}
        if orchestrator.shuttle_is_live(t, data) and not shuttle.get("launching"):
            continue
        pr = t.get("pr") if isinstance(t.get("pr"), dict) else {}
        if shuttle.get("pending") and shuttle.get("launching"):
            kind = "a Shuttle launch"
        elif t.get("status") == "alarm":
            kind = "an alarm"
        elif t.get("status") == "awaiting_approval":
            kind = "approval"
        elif t.get("phase") in {"ready", "merging"} or t.get("status") == "merging":
            kind = "merge"
        elif not pr.get("url"):
            kind = "a pull request"
        elif orchestrator.bugbot_applies(t, cfg) and not pipeline.bugbot_finished(t, cfg):
            kind = "Bugbot"
        else:
            kind = "CI"
        groups.setdefault(kind, []).append(str(tid))
    parts = []
    for kind in order:
        ids = groups.get(kind) or []
        if ids:
            shown = ", ".join(ids[:6]) + (" +%d" % (len(ids) - 6) if len(ids) > 6 else "")
            parts.append("%d on %s (%s)" % (len(ids), kind, shown))
    return "; ".join(parts)


def stall_news(data: dict, lines: list, work: list, moved: bool) -> list:
    """`stall:` when no Shuttle is out and two passes in a row moved nothing; one herald per stall, and one when
    work moves again."""
    import orchestrator

    stall = data.get("stall") if isinstance(data.get("stall"), dict) else {}
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    unfinished = any(isinstance(t, dict) and t.get("status") not in orchestrator.SETTLED for t in tickets.values())
    idle = unfinished and not moved and _live_count(data) == 0
    out = []
    if not idle:
        if stall.get("announced"):
            out.append("herald: Work is moving again.")
        data.pop("stall", None)
        return out
    stall["passes"] = int(stall.get("passes") or 0) + 1
    data["stall"] = stall
    if stall["passes"] < 2:
        return out
    try:
        import beam as beam_mod

        cap = beam_mod.capacity(data)["line"].split("; ", 1)
        why = cap[1] if len(cap) > 1 else cap[0]
    except Exception:
        why = ""
    nxt = ", ".join(work[:6]) if work else "none"
    waits = waiting_line(data)
    out.append("stall: no Shuttle is out and nothing moved for %d passes. Next steps known: %s. Waiting: %s. Queue: %s"
               % (stall["passes"], nxt, waits or "nothing", why or "unknown"))
    if " on a Shuttle launch (" in " " + waits or (work and any(k.startswith(_ACTIONABLE) or k.startswith("ready:") for k in work)):
        out.append("stall: MUST DO run the start, resume, merge, and alarm-repair lines in this pass's output now; "
                   "a line printed and not acted on is why nothing moves")
    if not stall.get("announced"):
        stall["announced"] = True
        out.append("herald: Nothing is running. %s%s" % (
            ("Waiting: %s. " % waits) if waits else "", ("Queue: %s." % why) if why else ""))
    return out


def capacity_news(data: dict) -> list:
    """The capacity line each pass, and one Slack line when maxInProgress starts or stops holding new tickets."""
    import beam as beam_mod

    try:
        info = beam_mod.capacity(data)
    except Exception as exc:
        return ["capacity: not computed (%s)" % exc]
    lines = [info["line"]]
    was = str(data.get("capacityAnnounced") or "")
    now = info["limiter"] if info["limiter"] == "maxInProgress" else ""
    if now != was:
        if now:
            lines.append("herald: New tickets are held: maxInProgress %d/%d is full while %d of %d Shuttle slots are free "
                         "(%d open tickets wait on review, CI, or approval). Raise maxInProgress in .warp/config.yaml, or set it to auto."
                         % (info["open"], info["progressCap"], info["free"], info["cap"], info["waiting"]))
        else:
            lines.append("herald: New tickets are starting again: maxInProgress is no longer full.")
        data["capacityAnnounced"] = now
    return lines
