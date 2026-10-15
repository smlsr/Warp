#!/usr/bin/env python3
"""Slack and Teams news the run posts that no single step owns: alarms, Shuttle starts, run start and stop.

- **Alarms.** At the end of every pass (and on start and resume) each ticket's alarm is compared with the
  last one announced (`alarmAnnounced` on the ticket). A new alarm posts why
  (`WE-07 is in alarm: lock-escape. It edited files outside its locks: app/x.ts.`); a ticket out of alarm
  posts what it is doing now and why, when Warp knows (`WE-07 is out of alarm (lock-escape): its locks were
  widened to app/x.ts and a repair Shuttle is on it.`). However the alarm was raised or cleared, it is
  announced once.
- **Shuttle starts.** Every Shuttle a pass starts, not only a ticket's first, posts one line naming the step
  (coding, fix and why, review, restart, replacement, resume after a halt) and the model.
- **The run.** `/warp-start` and `/warp-resume` post where the run came from, where it stands, and what is
  starting now; a refused start posts why. `/warp-pause` and `/warp-stop` post what they ended.
"""

from __future__ import annotations

from typing import Optional

ALARM_WHY = {
    "lock-escape": "it edited files outside its locks",
    "worker-died": "its Shuttle died and the recovery cap is reached",
    "stuck": "its Shuttle stopped reporting",
    "stalled": "it made no progress for too long",
    "stale": "its Shuttle's heartbeat went stale",
    "ci-red": "CI is red",
    "gate-red": "its gate is red",
    "bugbot-failed": "Bugbot findings were not fixed within maxFixAttempts",
    "spawn-cap": "the Shuttle spawn cap is reached",
    "conflict": "its pull request has a merge conflict",
    "parked": "it was parked",
}


def _short(text, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def alarm_text(ticket: dict) -> str:
    """Why this ticket is in alarm, in one sentence."""
    code = str(ticket.get("alarm") or "").strip() or "alarm"
    why = ALARM_WHY.get(code, "")
    bits = []
    if code == "lock-escape":
        paths = [p for p in ticket.get("escaped") or [] if isinstance(p, str)]
        if paths:
            bits.append(", ".join(paths[:4]) + (" and %d more" % (len(paths) - 4) if len(paths) > 4 else ""))
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    for key in ("alarmDetail", "error", "lastError"):
        if ticket.get(key):
            bits.append(_short(ticket[key]))
            break
    else:
        if code in {"ci-red", "conflict"} and pr.get("url"):
            bits.append(str(pr["url"]))
        if code == "bugbot-failed" and pr.get("fixAttempts"):
            bits.append("%s fix attempts" % pr["fixAttempts"])
    sentence = "%s is in alarm: %s." % (ticket.get("id"), code) if code != "alarm" else "%s is in alarm." % ticket.get("id")
    if why:
        sentence += " %s%s." % (why[0].upper() + why[1:], (": " + "; ".join(bits)) if bits else "")
    elif bits:
        sentence += " " + "; ".join(bits) + "."
    return sentence


def _exit_why(data: dict, ticket: dict, was: str) -> str:
    status = str(ticket.get("status") or "")
    info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    if status in {"merged", "done"}:
        return "it is %s" % status
    if status == "skipped":
        return "it was skipped"
    if was == "lock-escape" and info.get("state") in {"working", "passed"}:
        added = [p for p in info.get("added") or [] if isinstance(p, str)]
        return "its locks were widened%s and a repair Shuttle %s" % (
            (" to " + ", ".join(added[:4])) if added else "", "is on it" if info.get("state") == "working" else "finished")
    if info.get("state") == "cleared":
        return "heal cleared it"
    if shuttle.get("step") == "restart" or status == "recovering":
        return "a new Shuttle is restarting it"
    if shuttle.get("step") == "fix":
        return "a fix Shuttle took it"
    if shuttle.get("step") == "review" or status == "review":
        return "it is back in review"
    return "it is now %s" % (status or "queued")


def alarm_news(data: dict) -> list:
    """`herald:` lines for alarms raised or cleared since the last announcement. Marks them announced."""
    lines = []
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for tid, ticket in tickets.items():
        if not isinstance(ticket, dict):
            continue
        now = str(ticket.get("alarm") or "") if ticket.get("status") == "alarm" else ""
        if ticket.get("status") == "alarm" and not now:
            now = "alarm"
        before = str(ticket.get("alarmAnnounced") or "")
        if now == before:
            continue
        if now:
            lines.append("herald: " + alarm_text(ticket))
            ticket["alarmAnnounced"] = now
        else:
            lines.append("herald: %s is out of alarm (%s): %s." % (ticket.get("id") or tid, before, _exit_why(data, ticket, before)))
            ticket.pop("alarmAnnounced", None)
    return lines


STEP_WORDS = {
    "implement": "a Shuttle is starting the work",
    "fix": "a fix Shuttle is starting",
    "review": "a review Shuttle is starting",
    "restart": "its Shuttle is restarting",
    "repair": "a lock-escape repair Shuttle is starting",
}


def start_text(data: dict, ticket: dict, step: str, detail: str = "", replacing: bool = False, resumed: bool = False) -> str:
    """One line for a Shuttle a pass starts."""
    import multirepo
    import orchestrator

    tid = ticket.get("id")
    cfg = data.get("config") or {}
    if ticket.get("haltResume") or "resume after halt" in str(detail or ""):
        what = "a new Shuttle picks it up after the pause (%s)" % step
        detail = ""
    elif resumed:
        what = "its Shuttle is resuming (%s)" % step
    elif replacing:
        what = "a new Shuttle replaces the one that died (%s)" % step
    else:
        what = STEP_WORDS.get(step, "a Shuttle is starting (%s)" % step)
    try:
        model = orchestrator.model_for(ticket, cfg, step=step)[0]
    except Exception:
        model = ""
    where = []
    if ticket.get("branch"):
        where.append("on %s" % ticket["branch"])
    repo = multirepo.ticket_repo(data, ticket)
    if repo:
        where.append("in %s" % repo)
    if model:
        where.append("model %s" % model)
    text = "%s: %s%s" % (tid, what, (" (" + ", ".join(where) + ")") if where else "")
    if detail and step in {"fix", "restart"}:
        text += ": " + _short(detail, 140)
    return text + "."


def run_line(data: dict, verb: str, prior: dict, lines: list) -> str:
    """Where the run came from, where it stands, and what starts now."""
    import orchestrator

    tickets = [t for t in (data.get("tickets") or {}).values() if isinstance(t, dict)]
    merged = sum(1 for t in tickets if t.get("status") in {"merged", "done"})
    alarms = sorted(str(t.get("id")) for t in tickets if t.get("status") == "alarm")
    out = sum(1 for t in tickets if orchestrator.shuttle_is_live(t, data))
    open_ = sum(1 for t in tickets if t.get("status") not in {"merged", "done", "skipped", "queued", "blocked"})
    state = prior.get("runState") or ("paused" if prior.get("paused") else "stopped")
    reason = str(prior.get("pauseReason") or "").strip()
    if state == "running" and not prior.get("paused"):
        head = "Run picked up by a new parent session (%s)" % verb
    elif state == "paused" or prior.get("paused"):
        head = "Run resumed (was paused%s)" % (": " + reason if reason else "")
    else:
        head = "Run started (was stopped%s)" % (": " + reason if reason and reason != "stop" else "")
    starting = []
    for line in lines:
        parts = str(line).split()
        if len(parts) >= 2 and parts[0] in {"start", "resume"}:
            starting.append(parts[1])
        elif str(line).startswith("alarm-repair: start "):
            starting.append(parts[2] + " (lock-escape repair)")
    seen = []
    for item in starting:
        if item not in seen:
            seen.append(item)
    text = "%s. %d of %d merged, %d open, %d Shuttle%s out%s." % (
        head, merged, len(tickets), open_, out, "" if out == 1 else "s",
        (", in alarm: " + ", ".join(alarms[:8]) + (" +%d" % (len(alarms) - 8) if len(alarms) > 8 else "")) if alarms else "")
    text += (" Starting now: %s." % ", ".join(seen[:12]) + (" +%d" % (len(seen) - 12) if len(seen) > 12 else "")) if seen else " Nothing new to start yet."
    return text


def refused_line(verb: str, lines: list) -> Optional[str]:
    refusals = [str(line)[len("refuse: "):] for line in lines if str(line).startswith("refuse: ")]
    if not refusals:
        return None
    return "Run not %s: %s" % ("resumed" if verb == "resume" else "started", _short(refusals[-1], 300))


def halt_line(state: str, reason: str, lines: list) -> str:
    agents = ""
    for line in lines:
        text = str(line)
        if text.startswith("halt: ") and "agents=" in text:
            agents = text.split("agents=", 1)[1].split()[0]
    head = "Run %s%s." % (state, (" (%s)" % reason) if reason and reason not in {"stop", state} else "")
    tail = " %s agent%s ended." % (agents, "" if agents == "1" else "s") if agents else ""
    return head + tail + " Branches, worktrees, and pull requests stay. /warp-resume or warp:resume continues."


# ---- review cycle ------------------------------------------------------------------------------------------


def _pr_label(pr: dict) -> str:
    import re

    match = re.search(r"/pull/(\d+)", str(pr.get("url") or ""))
    return ("PR #%s" % match.group(1)) if match else "its pull request"


def stage(data: dict, ticket: dict) -> tuple:
    """(key, text) for where a ticket is in its review cycle; text None when another line already says it.

    The key carries the head commit, so a new push starts the cycle again.
    """
    import orchestrator

    status = str(ticket.get("status") or "")
    phase = str(ticket.get("phase") or "")
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    head = str(pr.get("headSha") or "")[:12]
    tid = ticket.get("id")
    label = _pr_label(pr)
    cfg = data.get("config") or {}
    if status in {"merged", "done", "skipped", "parked", "alarm", "queued", "blocked", "awaiting_approval"}:
        return status, None  # merged, parked, approval, and alarm lines say these
    if status == "merging" or phase == "merging":
        return "merging@" + head, "%s: merging %s (%s)." % (tid, label, pr.get("mergeMethod") or cfg.get("mergeMethod") or "merge")
    if phase == "ready":
        return "ready@" + head, "%s: all checks are green on %s; it is next in the merge queue." % (tid, label)
    if status == "fix" or phase == "fixing":
        import pipeline

        shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
        why = str(shuttle.get("detail") or "").strip() or "; ".join(pipeline.red_reasons(ticket, cfg)[:3])
        return "fix@" + head, "%s: back to fix%s." % (tid, (": " + _short(why, 160)) if why else "")
    if status in {"review", "bugbot_running"} or phase in {"pr-open", "reviewing"}:
        if not pr.get("url"):
            return "pr-wait", "%s: code is done; waiting for its pull request." % tid
        run = pr.get("bugbotRun") if isinstance(pr.get("bugbotRun"), dict) else {}
        on_head = not head or str(run.get("sha") or "")[:12] == head
        if orchestrator.bugbot_applies(ticket, cfg):
            if on_head and run.get("startedAt") and not run.get("doneAt"):
                return "bugbot-running@" + head, None  # "Cursor Bugbot is reviewing …"
            if on_head and run.get("doneAt"):
                if str(run.get("result") or "") != "pass":
                    return "bugbot-findings@" + head, None  # "… finding(s). Back to fix."
                ci = str(pr.get("ci") or "").casefold()
                if ci in {"green", "success", "pass"} or pr.get("ciNone"):
                    return "checks@" + head, "%s: Bugbot passed and CI is green on %s; waiting on its acceptance checks." % (tid, label)
                return "ci-wait@" + head, "%s: Bugbot passed on %s; waiting on CI." % (tid, label)
            asked = " (asked %sZ)" % str(pr.get("bugbotRequestedAt"))[11:16] if pr.get("bugbotRequestedAt") else ""
            return "bugbot-wait@" + head, "%s: in review on %s; waiting for Cursor Bugbot to start%s." % (tid, label, asked)
        return "ci-wait@" + head, "%s: in review on %s; waiting on CI." % (tid, label)
    if status in {"claimed", "planning", "coding", "recovering"}:
        return "coding", None  # the claim and Shuttle start lines say it
    return status, None


def stage_news(data: dict, lines: list) -> list:
    """One `herald:` line per review-cycle step a ticket reached since the last one announced (`stageAnnounced`).

    A ticket that already has its own line in this pass (`herald: WE-01 …`) gets no second one.
    """
    out = []
    said = set()
    for line in lines or []:
        text = str(line)
        if text.startswith("herald: "):
            word = text[len("herald: "):].split(" ", 1)[0].rstrip(":")
            said.add(word)
    for tid, ticket in (data.get("tickets") or {}).items():
        if not isinstance(ticket, dict):
            continue
        try:
            key, text = stage(data, ticket)
        except Exception:
            continue
        if key == ticket.get("stageAnnounced"):
            continue
        ticket["stageAnnounced"] = key
        if text and str(ticket.get("id") or tid) not in said:
            out.append("herald: " + text)
    return out
