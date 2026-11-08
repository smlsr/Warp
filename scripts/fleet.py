#!/usr/bin/env python3
"""The agent sweep, every 15 minutes (1.7.15): agent and subagent health across the whole repo.

Inventory: every cloud agent for this repo (Cloud Agents API: lifecycle, last activity, created-at), joined with
`.warp/agents.json`, the beam and `prOwners`, so each is agent -> parent coordinator -> ticket -> pull request.

Classify:
  active            pushed or reported within sweep.activeMinutes (45)
  stalled           running, no push or report, and its pull request has gone stale
  duplicate         two or more agents own one pull request or gap
  finished-running  its pull request merged or closed, or it reported done, but it still shows running
  stale-wait        waiting on something that already happened (a merged PR, a finished CI run, a sent GAPPR)
  orphan-pr         an open pull request with no live owner

Act:
  duplicate         keep the agent with the latest push, stop the rest
  finished-running  stop it
  stalled           nudge once, then restaff after sweep.restaffMinutes (30); the new agent checks the remote head
                    first and builds on it
  stale-wait        send the owner the fact ("#X merged at T")
  orphan-pr         assign it to the least-loaded coordinator in its area

Stopping uses the API key that created the agent, or goes through its parent coordinator. When neither can reach it:
one `needs-human-stop <agent url>` line, and its pull request stays protected by the sole-pusher guard (not alarmed).
The sweep writes `.warp/sweep/<ts>.md` and posts only the actions it took.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

_BUSY = {"RUNNING", "CREATING", "PENDING", "ACTIVE", "BUSY"}
_WAITS = re.compile(r"(waiting (on|for)|waits on|blocked on|until)\s+(#\d+|[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+|ci|gappr)", re.I)


def _pr(text: str) -> str:
    match = re.search(r"/pull/(\d+)|#(\d+)", str(text or ""))
    return (match.group(1) or match.group(2)) if match else ""


def inventory(items: list, registry: list, data: dict, repo_key: str = "", heads: Optional[dict] = None) -> list:
    """One row per agent: {id, status, lastActivity, createdAt, parent, ticket, pr, summary, lastPush}."""
    by_id = {}
    for row in registry:
        if not isinstance(row, dict):
            continue
        for key in ("cloudId", "agentId", "id"):
            if row.get(key):
                by_id[str(row[key])] = row
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    ticket_by_agent = {str(t.get("agent")): tid for tid, t in tickets.items() if isinstance(t, dict) and t.get("agent")}
    owners = data.get("prOwners") if isinstance(data.get("prOwners"), dict) else {}
    pr_by_agent = {str(v.get("agent")): k for k, v in owners.items() if isinstance(v, dict) and v.get("agent")}
    out = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        aid = str(item["id"])
        reg = by_id.get(aid, {})
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        pr = _pr(target.get("prUrl") or item.get("prUrl") or "") or pr_by_agent.get(aid, "")
        tid = ticket_by_agent.get(aid) or str(reg.get("ticket") or "")
        if not pr and tid and isinstance(tickets.get(tid), dict):
            pr = _pr(((tickets[tid].get("pr") or {}).get("url")) or "")
        branch = str(target.get("branchName") or reg.get("branch") or "")
        if not pr and branch and heads:
            # 1.7.17: a coordinator's worker started without a target PR: its branch names the open pull request.
            pr = str(heads.get(branch) or "")
        if not pr:
            named = re.search(r"#(\d+)\b", str(item.get("name") or ""))
            pr = named.group(1) if named else ""
        out.append({
            "id": aid, "status": str(item.get("status") or "").upper(), "name": item.get("name") or "",
            "lastActivity": str(item.get("updatedAt") or item.get("lastActivityAt") or item.get("createdAt") or ""),
            "createdAt": str(item.get("createdAt") or ""), "parent": str(reg.get("parent") or reg.get("session") or ""),
            "ticket": tid, "pr": pr, "summary": str(item.get("summary") or ""), "keyEnv": reg.get("keyEnv") or "",
            "lastPush": str(reg.get("lastPushAt") or ""), "branch": str(target.get("branchName") or reg.get("branch") or ""),
        })
    return out


def classify(rows: list, prs: dict, data: dict, cfg: dict, now=None) -> list:
    """Adds `class` and `why` to each row. prs: {number: {state, updatedAt, mergedAt, head, ciDone}}."""
    import beam as beam_mod
    import selfdrive

    now_dt = beam_mod.coerce_now(now)[0]
    active_m = selfdrive.num(cfg, "sweep", "activeMinutes")
    by_pr: dict = {}
    for row in rows:
        if row["pr"] and row["status"] in _BUSY:
            by_pr.setdefault(row["pr"], []).append(row)
    dup_losers = set()
    for number, group in by_pr.items():
        if len(group) > 1:
            group.sort(key=lambda r: (r.get("lastPush") or r.get("lastActivity") or ""), reverse=True)
            for loser in group[1:]:
                dup_losers.add(loser["id"])
    for row in rows:
        last = beam_mod.parse_ts(row.get("lastPush") or row.get("lastActivity") or "")
        idle = (now_dt - last).total_seconds() / 60 if last is not None else 1e9
        pr = prs.get(row["pr"]) if row["pr"] else None
        busy = row["status"] in _BUSY
        if not busy:
            row["class"], row["why"] = "ended", row["status"].lower() or "not running"
        elif row["id"] in dup_losers:
            row["class"], row["why"] = "duplicate", "another agent on #%s pushed later" % row["pr"]
        elif pr and pr.get("state") in {"MERGED", "CLOSED"}:
            row["class"], row["why"] = "finished-running", "#%s is %s" % (row["pr"], pr["state"].lower())
        elif re.search(r"\b(done|finished|complete[d]?)\b", row["summary"], re.I) and idle >= active_m:
            row["class"], row["why"] = "finished-running", "it reported done"
        elif (wait := _waits_on(row["summary"], prs, data)):
            row["class"], row["why"] = "stale-wait", wait
        elif idle < active_m:
            row["class"], row["why"] = "active", "activity %dm ago" % idle
        else:
            row["class"], row["why"] = "stalled", "no push or report for %dm" % idle
    return rows


def _waits_on(summary: str, prs: dict, data: dict) -> str:
    import terminal

    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for match in _WAITS.finditer(summary or ""):
        name = match.group(3)
        if name.startswith("#"):
            pr = prs.get(name[1:])
            if pr and pr.get("state") == "MERGED":
                return "%s merged at %s" % (name, pr.get("mergedAt") or "?")
        elif isinstance(tickets.get(name), dict) and terminal.is_terminal(tickets[name]):
            return "%s is %s" % (name, tickets[name].get("status"))
    return ""


def orphans(rows: list, open_prs: dict) -> list:
    live = {r["pr"] for r in rows if r.get("class") in {"active", "stalled", "stale-wait"} and r.get("pr")}
    return sorted(n for n, pr in open_prs.items() if pr.get("state") == "OPEN" and n not in live)


def act(beam_path: Path, data: dict, rows: list, orphan_prs: list, cfg: dict, stop: Callable, send: Callable,
        restaff: Callable, assign: Callable, now=None) -> list:
    """stop(row) -> (ok, how); send(owner, text, agent) -> lines; restaff(row) -> lines; assign(pr) -> lines."""
    import beam as beam_mod
    import selfdrive

    now_dt, now_s = beam_mod.coerce_now(now)
    book = data.setdefault("sweepBook", {})
    restaff_m = selfdrive.num(cfg, "sweep", "restaffMinutes")
    out = []
    for row in rows:
        cls = row.get("class")
        mark = book.setdefault(row["id"], {})
        if cls in {"duplicate", "finished-running"}:
            if mark.get("stopped"):
                continue
            ok, how, more = _stop(stop, row)
            out.extend(more)
            mark["stopped"] = now_s if ok else ""
            if ok:
                out.append("sweep: stopped %s (%s: %s)%s" % (row["id"], cls, row["why"], (" via %s" % how) if how else ""))
            elif not mark.get("humanStop"):
                mark["humanStop"] = now_s
                out.append("needs-human-stop https://cursor.com/agents?id=%s (%s: %s). Its pull request #%s stays "
                           "protected: only its recorded owner pushes." % (row["id"], cls, row["why"], row.get("pr") or "-"))
        elif cls == "stalled":
            if not mark.get("nudgedAt"):
                mark["nudgedAt"] = now_s
                out.extend(send(row.get("parent") or row["id"], "You have not pushed or reported for a while on %s. "
                                "Push what you have (one push covering every open check and finding) or report what "
                                "blocks you." % where(row), row["id"]))
                continue
            nudged = beam_mod.parse_ts(mark["nudgedAt"])
            if nudged is not None and (now_dt - nudged).total_seconds() >= restaff_m * 60 and not mark.get("restaffedAt"):
                mark["restaffedAt"] = now_s
                ok, how, more = _stop(stop, row)
                out.extend(more)
                out.extend(restaff(row))
                out.append("sweep: %s stalled after a nudge %dm ago; %s and restaffed #%s (the new agent starts from the "
                           "remote head)" % (row["id"], restaff_m, "stopped" if ok else "could not stop it", row.get("pr") or row.get("ticket")))
        elif cls == "stale-wait":
            if mark.get("toldWait") == row["why"]:
                continue
            mark["toldWait"] = row["why"]
            out.extend(send(row.get("parent") or row["id"], "Stop waiting on %s: %s." % (where(row), row["why"]), row["id"]))
    for number in orphan_prs:
        mark = book.setdefault("pr:%s" % number, {})
        if mark.get("assigned"):
            continue
        lines = assign(number)
        if lines:
            mark["assigned"] = now_s
            out.extend(lines)
    return out


def where(row: dict) -> str:
    """What the agent is on, for a message: `#<pr>`, else `your branch <branch>`, else its ticket by name (never
    `#<ticket id>` and never `#?`)."""
    if str(row.get("pr") or "").isdigit():
        return "#%s" % row["pr"]
    if row.get("branch"):
        return "your branch %s" % row["branch"]
    if row.get("ticket"):
        return "ticket %s" % row["ticket"]
    return "your current work"


def _stop(stop: Callable, row: dict) -> tuple:
    got = stop(row)
    if len(got) == 3:
        return got
    return got[0], got[1], []


def report(beam_path: Path, rows: list, actions: list, now=None) -> Path:
    import beam as beam_mod
    import selfdrive

    now_s = beam_mod.coerce_now(now)[1]
    path = selfdrive.report_path(beam_path, "sweep", now_s)
    counts: dict = {}
    for row in rows:
        counts[row.get("class", "?")] = counts.get(row.get("class", "?"), 0) + 1
    body = ["# Sweep %s" % now_s, "", "Agents: %d (%s)" % (len(rows), ", ".join("%s %d" % kv for kv in sorted(counts.items()))), "",
            "## Actions", ""] + (["- " + a for a in actions] or ["- none"]) + ["", "## Agents", "",
            "| agent | class | why | parent | ticket | PR |", "|---|---|---|---|---|---|"]
    body += ["| %s | %s | %s | %s | %s | %s |" % (r["id"], r.get("class"), r.get("why"), r.get("parent") or "-",
                                                 r.get("ticket") or "-", r.get("pr") or "-") for r in rows]
    path.write_text("\n".join(body) + "\n")
    return path
