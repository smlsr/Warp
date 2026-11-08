#!/usr/bin/env python3
"""The four cadences with their real inputs (1.7.15): `wake.py tick|sweep|audit|deep` and `wake.py run` call these.

Every outside call goes through an `Env`, so tests replace GitHub (REST), the Cloud Agents API and git with functions:
  env.rest(cmd, root)        gh api …            (provider._run)
  env.git(cmd, root)         git …               (provider._run)
  env.agents(method, url, key, body)             (agents.default_transport)
  env.env                    os.environ          (API keys)

State a cadence keeps lives on the beam (prOwners, waivers, said, refresh, ladder, …) and in `.warp/*.json`. The
beam is re-read under the run lock at the end and only those keys and the tickets a cadence touched are written,
so a supervise pass or a gap created meanwhile is never written over.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

KEYS = ("prOwners", "waivers", "said", "refresh", "ladder", "ciCancelled", "bugbotRunAsked", "bugbotSentBack",
        "sweepBook", "mergedPrs", "capHits")


class Env:
    def __init__(self, rest: Optional[Callable] = None, git: Optional[Callable] = None, agents: Optional[Callable] = None,
                 env: Optional[dict] = None, clock: Optional[Callable] = None):
        import provider

        self.rest = rest or provider._run
        self.git = git or provider._run
        self.agents = agents
        self.env = env if env is not None else dict(os.environ)
        self.clock = clock or time.monotonic


def _ctx(beam_path: Path):
    import beam as beam_mod
    import ghrest
    import selfdrive

    path = Path(beam_path)
    data = beam_mod.load_json(path)
    root = path.resolve().parent.parent
    cfg = selfdrive.config(data, path)
    slug = ghrest.slug_of(root)
    return path, data, root, cfg, slug


def _fingerprint(data: dict) -> dict:
    return {tid: json.dumps([t.get("status"), t.get("holds"), (t.get("pr") or {}).get("mergedBy")], sort_keys=True)
            for tid, t in (data.get("tickets") or {}).items() if isinstance(t, dict)}


def commit(path: Path, data: dict, before: dict) -> None:
    """Write back only the cadence keys and the tickets whose status, holds or lane merge changed."""
    import beam as beam_mod
    import beamlock

    after = _fingerprint(data)
    touched = [tid for tid, fp in after.items() if before.get(tid) != fp]
    with beamlock.run_lock(path):
        disk = beam_mod.load_json(path)
        for key in KEYS:
            if key in data:
                disk[key] = data[key]
        for tid in touched:
            if tid in (disk.get("tickets") or {}) and isinstance(data["tickets"].get(tid), dict):
                for field in ("status", "holds", "pr", "phase", "restaffRequested", "coordinator", "externalOwner"):
                    if field in data["tickets"][tid]:
                        disk["tickets"][tid][field] = data["tickets"][tid][field]
        beam_mod.atomic_write(path, json.dumps(disk, indent=2) + "\n", data=disk)


def sender(path: Path, data: dict, env: Env) -> Callable:
    import relay

    def send(owner: str, text: str, agent: str = "") -> list:
        return relay.send(path, data, owner, text, agent_id=agent, transport=env.agents, env=env.env)
    return send


def _owner_row(data: dict, number: str) -> dict:
    import relay

    return relay.owner_of(data, str(number))


# ---- tick ---------------------------------------------------------------------------------------------------------


def _main_history(root: Path, env: Env, base: str, slug: str, n: int = 12) -> list:
    import ghrest
    import provider

    done = env.git(["git", "log", "--first-parent", "--format=%H", "-n", str(n), "origin/%s" % base], root)
    shas = [s for s in (getattr(done, "stdout", "") or "").split() if s][::-1]
    out = []
    for sha in shas:
        ok, doc, _ = ghrest.call(root, "GET", "repos/%s/commits/%s/check-runs?per_page=100" % (slug, sha), runner=env.rest)
        runs = (doc or {}).get("check_runs") or [] if ok and isinstance(doc, dict) else []
        rows = [{"name": r.get("name"), "conclusion": str(r.get("conclusion") or "").upper(),
                 "status": str(r.get("status") or "").upper()} for r in runs if isinstance(r, dict)]
        state = provider.interpret_rollup(rows) if rows else ""
        out.append((sha, "green" if state in {"green", "pass"} else "red" if state in {"red", "fail"} else ""))
    return out


def tick(beam_path: Path, env: Optional[Env] = None, now=None, base_checks: Optional[Callable] = None) -> list:
    """The lane, blockers, holds, main red, hung CI, Bugbot. Must finish in under 90 s."""
    import beam as beam_mod
    import blockgraph
    import ghrest
    import intake
    import lane
    import provider
    import selfdrive
    import waivers

    env = env or Env()
    began = env.clock()
    path, data, root, cfg, slug = _ctx(beam_path)
    before = _fingerprint(data)
    lines: list = []
    send = sender(path, data, env)
    base = str((data.get("config") or {}).get("baseBranch") or "main")
    top_n = int(selfdrive.num(cfg, "blockers", "topN"))
    # Main red: waiver, one gap per break (new failures join it), strict gate; retire on the fix merge.
    try:
        checks = (base_checks or (lambda: provider.base_checks(root, base, runner=env.rest)))()
        lines.extend(waivers.main_red(path, data, checks, lambda: _main_history(root, env, base, slug),
                                      lambda sha: _author(root, env, sha), now=now, top_n=top_n))
        lines.extend(waivers.retire(path, data, now=now))
    except Exception as exc:
        lines.append("tick: main red not checked (%s)" % exc)
    contains = waivers.git_contains(root, env.git)
    # Intake: READY and GAPPR, verified over REST.
    try:
        lines.extend(intake.intake(path, data, lambda pr: ghrest.pr_doc(root, _url(slug, pr), runner=env.rest),
                                   contains=contains, now=now))
    except Exception as exc:
        lines.append("tick: intake skipped (%s)" % exc)
    # The lane.
    try:
        method = provider.merge_method(data.get("config") or {})
        cap = beam_mod.config_int(data, path, "maxMergesPerPass", 3)
        lines.extend(lane.run_lane(
            path, data,
            merge=lambda e: ghrest.merge(root, e.get("url") or _url(slug, e["pr"]), sha=e["sha"],
                                         method=method if method in {"squash", "rebase"} else "squash", runner=env.rest),
            list_children=lambda branch: _children(root, slug, branch, data, env),
            retarget=lambda url, to: ghrest.retarget(root, url, to, runner=env.rest),
            rebuild=lambda child, head: lane.rebuild_child(root, child, head, base=base, run=env.git),
            send=send, max_merges=cap, now=now))
    except Exception as exc:
        lines.append("tick: lane skipped (%s)" % exc)
    # Stale pull requests: one REFRESH per head; union-only conflicts are merged by the lane.
    try:
        lines.extend(lane.refresh_stale(path, data, _dirty(root, data, env, base), lambda b, onto: lane.merge_into(
            root, b, onto, run=env.git), send, cfg, now=now))
    except Exception as exc:
        lines.append("tick: refresh skipped (%s)" % exc)
    # Hung CI: cancel once, never rerun.
    try:
        heads = [(str((t.get("pr") or {}).get("headSha") or ""), tid) for tid, t in (data.get("tickets") or {}).items()
                 if isinstance(t, dict) and str((t.get("pr") or {}).get("ci") or "") in {"pending", "running"}
                 and (t.get("pr") or {}).get("headSha")]
        if heads and slug:
            lines.extend(waivers.hung(path, data, root, slug, heads, runner=env.rest, now=now,
                                      minutes=selfdrive.num(cfg, "ci", "hungMinutes")))
    except Exception as exc:
        lines.append("tick: hung CI not checked (%s)" % exc)
    # Bugbot: too large -> `bugbot run`; findings after GAPPR -> back to the owner, all at once.
    try:
        lines.extend(waivers.bugbot_actions(path, data, root, _bugbot_rows(path, data),
                                            lambda url, body: ghrest.comment(root, url, body, runner=env.rest), send))
    except Exception as exc:
        lines.append("tick: Bugbot actions skipped (%s)" % exc)
    lines.extend(selfdrive.say_once(data, "blockers", blockgraph.lines(data, top_n, now=now)[0]))
    commit(path, data, before)
    took = env.clock() - began
    lines.append("tick: done in %.1fs" % took)
    return lines


def _url(slug: str, pr) -> str:
    text = str(pr)
    return text if text.startswith("http") else "https://github.com/%s/pull/%s" % (slug, text.lstrip("#"))


def _author(root: Path, env: Env, sha: str) -> str:
    done = env.git(["git", "log", "-1", "--format=%an", sha], root)
    return (getattr(done, "stdout", "") or "").strip()


def _children(root: Path, slug: str, branch: str, data: dict, env: Env) -> list:
    import ghrest

    if not branch or not slug:
        return []
    ok, rows, _ = ghrest.call(root, "GET", "repos/%s/pulls?state=open&base=%s&per_page=100" % (slug, branch), runner=env.rest)
    out = []
    for pull in rows if ok and isinstance(rows, list) else []:
        number = str(pull.get("number"))
        owner = _owner_row(data, number)
        out.append({"number": number, "url": pull.get("html_url") or _url(slug, number),
                    "branch": (pull.get("head") or {}).get("ref") or "", "mergeable": pull.get("mergeable_state") or "",
                    "owner": owner.get("owner") or ((pull.get("user") or {}).get("login") or ""), "agent": owner.get("agent") or "",
                    "stacked": True})
    return out


def _dirty(root: Path, data: dict, env: Env, base: str) -> list:
    import lane
    import terminal

    out = []
    for tid, t in sorted((data.get("tickets") or {}).items()):
        if not isinstance(t, dict) or terminal.is_terminal(t):
            continue
        pr = t.get("pr") if isinstance(t.get("pr"), dict) else {}
        if not pr.get("url") or str(pr.get("mergeStateStatus") or "").upper() not in {"DIRTY", "CONFLICTING"}:
            continue
        number = re.search(r"/pull/(\d+)", pr["url"]).group(1)
        branch = str(pr.get("headRef") or t.get("branch") or "")
        onto = str(pr.get("base") or base)
        done = env.git(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", "origin/%s" % onto,
                        "origin/%s" % branch], root)
        rows = [r for r in (getattr(done, "stdout", "") or "").splitlines() if r.strip()][1:]
        owner = _owner_row(data, number)
        out.append({"number": number, "branch": branch, "base": onto, "dirty": True, "conflicts": rows,
                    "owner": owner.get("owner") or t.get("externalOwner") or tid, "agent": owner.get("agent") or t.get("agent") or "",
                    "head": pr.get("headSha")})
    return out


def _bugbot_rows(path: Path, data: dict) -> list:
    import intake

    gappr = {e.get("pr") for e in (intake.load_lane(path).get("queue") or {}).values() if e.get("kind") == "GAPPR"}
    gappr |= {k.split("@")[0] for k, v in (intake.load_lane(path).get("seen") or {}).items() if v}
    out = []
    for tid, t in (data.get("tickets") or {}).items():
        if not isinstance(t, dict):
            continue
        pr = t.get("pr") if isinstance(t.get("pr"), dict) else {}
        match = re.search(r"/pull/(\d+)", str(pr.get("url") or ""))
        if not match:
            continue
        owner = _owner_row(data, match.group(1))
        out.append({"number": match.group(1), "url": pr["url"], "head": pr.get("headSha") or "",
                    "checks": pr.get("checks") or [], "findings": pr.get("findings") or pr.get("bugbotFindings") or [],
                    "gappr": match.group(1) in gappr, "owner": owner.get("owner") or tid, "agent": owner.get("agent") or t.get("agent") or ""})
    return out


# ---- sweep --------------------------------------------------------------------------------------------------------


def sweep(beam_path: Path, env: Optional[Env] = None, now=None) -> list:
    import agents
    import fleet
    import ghrest
    import relay

    env = env or Env()
    path, data, root, cfg, slug = _ctx(beam_path)
    before = _fingerprint(data)
    send = sender(path, data, env)
    key = str(env.env.get(agents.ENV_KEY) or "")
    items: list = []
    if key:
        ok, items, error = agents.list_cloud(key, transport=env.agents)
        if not ok:
            return ["sweep: Cloud Agents API not read (%s); nothing classified" % error]
    registry = agents._rows(agents.registry(data, path))
    rows = fleet.inventory(items, registry, data)
    pulls, _newest = ghrest.changed_pulls(root, slug, "", runner=env.rest, pages=3) if slug else ({}, "")
    prs = {n: {"state": "MERGED" if p.get("merged_at") else str(p.get("state") or "").upper(), "mergedAt": p.get("merged_at"),
               "updatedAt": p.get("updated_at"), "head": (p.get("head") or {}).get("sha")} for n, p in (pulls or {}).items()}
    fleet.classify(rows, prs, data, cfg, now=now)
    owned_open = {n: v for n, v in prs.items() if v["state"] == "OPEN" and str(n) in (data.get("prOwners") or {})}
    orphan = [n for n in fleet.orphans(rows, owned_open)
              if (relay.owner_of(data, n).get("agent") and relay.owner_of(data, n)["agent"] not in {r["id"] for r in rows if r.get("class") != "ended"})]

    def stop(row):
        api_key = relay.key_for({"keyEnv": row.get("keyEnv")}, env.env)
        if api_key:
            ok, detail = agents.cancel_run(row["id"], api_key, transport=env.agents)
            if ok:
                agents.archive_id(row["id"], api_key, transport=env.agents)
                return True, "api", []
        parent = row.get("parent") or ""
        if parent and parent not in {"warp", row["id"]}:
            return True, "parent %s" % parent, send(parent, "Stop your agent %s now (%s); it must not push again." % (
                row["id"], row.get("why")), parent)
        return False, "", []

    def restaff(row):
        tid = row.get("ticket")
        ticket = (data.get("tickets") or {}).get(tid) if tid else None
        if isinstance(ticket, dict):
            ticket["restaffRequested"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            ticket["status"] = "fix" if (ticket.get("pr") or {}).get("url") else "queued"
            ticket.pop("phase", None)
            return ["restaff: %s goes back to %s; the next pass starts a new Shuttle that checks the remote head first" % (
                tid, ticket["status"])]
        return send(row.get("parent") or "warp", "Restaff #%s: start a new agent that fetches the remote head first and "
                    "builds on it; stop %s." % (row.get("pr") or "?", row["id"]), "")

    def assign(number):
        import audit

        table = audit.load_table(data, audit.load_coordinators(path), rows)
        target = sorted(table, key=lambda r: len(r["prs"]))[0]["name"] if table else "warp"
        relay.set_owner(data, number, target, coordinator=target)
        return ["sweep: orphan #%s assigned to %s (least loaded)" % (number, target)] + send(
            target, "You own #%s now (it had no live owner). Fetch the remote head first, then carry it to READY." % number, "")

    actions = fleet.act(path, data, rows, orphan, cfg, stop, send, restaff, assign, now=now)
    report = fleet.report(path, rows, actions, now=now)
    (path.parent / "sweep" / "last.json").write_text(json.dumps(rows, indent=2) + "\n")
    commit(path, data, before)
    return actions + ["sweep: %d agents (%s); report %s" % (len(rows), ", ".join(
        "%s %d" % (c, sum(1 for r in rows if r.get("class") == c)) for c in sorted({r.get("class") for r in rows})),
        report.relative_to(path.parent.parent))]


# ---- audit --------------------------------------------------------------------------------------------------------


def audit(beam_path: Path, env: Optional[Env] = None, now=None) -> list:
    import audit as audit_mod
    import blockgraph
    import gaps
    import ghrest
    import intake
    import beam as beam_mod
    import selfdrive

    env = env or Env()
    path, data, root, cfg, slug = _ctx(beam_path)
    before = _fingerprint(data)
    send = sender(path, data, env)
    pulls, _ = ghrest.changed_pulls(root, slug, "", runner=env.rest, pages=3) if slug else ({}, "")
    by_number = {}
    for tid, t in (data.get("tickets") or {}).items():
        match = re.search(r"/pull/(\d+)", str(((t or {}).get("pr") or {}).get("url") or "")) if isinstance(t, dict) else None
        if match:
            by_number[match.group(1)] = t
    lane_doc = intake.load_lane(path)
    queued = {e.get("pr") for e in (lane_doc.get("queue") or {}).values()}
    red_by_check: dict = {}
    for number, pull in (pulls or {}).items():
        t = by_number.get(number) or {}
        pr = t.get("pr") or {}
        state = ("lane" if number in queued else "held" if t.get("holds") else
                 "dirty" if str(pr.get("mergeStateStatus") or "").upper() in {"DIRTY", "CONFLICTING"} else
                 "red" if pr.get("ci") in {"red", "fail"} else "ci-pending" if pr.get("ci") in {"pending", "running"} else
                 "bugbot-pending" if pr.get("bugbot") in {"pending", "running"} else "other")
        pull["_state"] = state
        pull["_dirtyDuringCi"] = bool(pr.get("dirtyDuringCi"))
        for name in pr.get("failedChecks") or []:
            red_by_check.setdefault(name, []).append(number)
    through = audit_mod.throughput(list((pulls or {}).values()), lane_doc.get("merged") or [], now=now)
    coords = audit_mod.load_coordinators(path)
    try:
        agents_rows = json.loads((path.parent / "sweep" / "last.json").read_text())
    except (OSError, ValueError):
        agents_rows = []
    table = audit_mod.load_table(data, coords, agents_rows)
    actions: list = []
    for chain, old, new in audit_mod.rebalance(data, table, cfg):
        actions.append("rebalance: chain %s moves from %s to %s" % (", ".join("#%s" % n for n in chain), old, new))
        actions.extend(send(old, "Hand off %s to %s: stop your workers on them, then write `HANDED-OFF %s <head>` for "
                            "each in your outbox." % (", ".join("#%s" % n for n in chain), new, new), ""))
        actions.extend(send(new, "You take over %s from %s. Push only after you see `HANDED-OFF %s <head>` for each "
                            "one, and build on that head." % (", ".join("#%s" % n for n in chain), old, new), ""))
    unowned: dict = {}
    for tid, t in (data.get("tickets") or {}).items():
        if isinstance(t, dict) and t.get("status") in {"queued", "parked", "external"} and not t.get("coordinator") and t.get("externalOwner") in (None, "", "outside Warp"):
            area = str(t.get("module") or "general")
            unowned[area] = unowned.get(area, 0) + 1
    actions.extend(audit_mod.coordinators_step(path, data, coords, table, unowned, cfg, now=now))
    audit_mod.save_coordinators(path, coords)
    found = audit_mod.discover(root, data, red_by_check)
    made = 0
    ranks = {n: i for i, (n, _s, _d) in enumerate(blockgraph.ranked(data, now=now))}
    for gap in found[:5]:
        if not selfdrive.config(data, path)["gaps"].get("autoCreate", True):
            break
        gid, lines = gaps.create(path, lambda: beam_mod.load_json(path), gap["area"], gap["short"], gap["summary"],
                                 source=gap["source"], row=gap.get("row") or "", score_rank=None,
                                 top_n=int(selfdrive.num(cfg, "gaps", "autoStaffTopN")), now=now)
        actions.extend(lines)
        made += 1
    data = _reload_keep(path, data)
    top = blockgraph.top(data, 10, now=now)
    import asks

    decisions = ["%s: %s" % (aid, row.get("question")) for aid, row in (asks._load(path).get("open") or {}).items()]
    headline = "%d merges in 3h, %d open pull requests, %d coordinators, %d actions" % (
        through["merges"][3], through["open"], len(table), len(actions))
    report, slack = audit_mod.report(path, headline, through, table, top, actions, decisions, now=now)
    commit(path, data, before)
    return actions + ["audit: report %s" % report.relative_to(path.parent.parent)] + ["herald: " + " | ".join(slack)]


def _reload_keep(path: Path, data: dict) -> dict:
    import beam as beam_mod

    disk = beam_mod.load_json(path)
    for key in KEYS:
        if key in data:
            disk[key] = data[key]
    return disk


# ---- deep ---------------------------------------------------------------------------------------------------------


def deep(beam_path: Path, env: Optional[Env] = None, now=None) -> list:
    """Waiver expiry, stale tickets, history and cost. Six-hourly."""
    import asks
    import beam as beam_mod
    import selfdrive
    import waivers

    env = env or Env()
    path, data, root, cfg, slug = _ctx(beam_path)
    now_dt, now_s = beam_mod.coerce_now(now)
    out = []
    before = _fingerprint(data)
    for wid, row in waivers.load(path).items():
        if not isinstance(row, dict) or row.get("retiredAt"):
            continue
        made = beam_mod.parse_ts(str(row.get("createdAt") or ""))
        gap = (data.get("tickets") or {}).get(str(row.get("gap") or ""))
        if made is not None and (now_dt - made).days >= 2 and isinstance(gap, dict) and gap.get("rank") != -1:
            # The ladder, not an alarm: the fix goes to the very front and its owner is told.
            gap["rank"] = -1
            gap["priority"] = "highest"
            out.append("deep: waiver %s is %d days old and %s has not merged; %s is now first in the queue" % (
                wid, (now_dt - made).days, row.get("gap"), row.get("gap")))
    if out:
        with __import__("beamlock").run_lock(path):
            disk = beam_mod.load_json(path)
            for tid, t in (data.get("tickets") or {}).items():
                if isinstance(t, dict) and t.get("rank") == -1 and tid in disk.get("tickets", {}):
                    disk["tickets"][tid].update(rank=-1, priority="highest")
            beam_mod.atomic_write(path, json.dumps(disk, indent=2) + "\n", data=disk)
    stale = []
    for tid, t in (data.get("tickets") or {}).items():
        if isinstance(t, dict) and t.get("status") == "queued":
            seen = beam_mod.parse_ts(str(t.get("updatedAt") or t.get("addedAt") or ""))
            if seen is not None and (now_dt - seen).days >= 7:
                stale.append(tid)
    if stale:
        out.append("deep: %d queued tickets untouched for 7 days: %s" % (len(stale), ", ".join(sorted(stale)[:20])))
    try:
        reg = json.loads((path.parent / "agents.json").read_text())
        spawns = len([r for r in reg.get("agents") or [] if isinstance(r, dict)])
    except (OSError, ValueError):
        spawns = 0
    out.append("deep: %d agents recorded on this run; %d agent-cap waits" % (spawns, len(data.get("capHits") or [])))
    body = ["# Deep audit %s" % now_s, ""] + ["- " + line for line in out]
    report = selfdrive.report_path(path, "deep", now_s)
    report.write_text("\n".join(body) + "\n")
    return out + ["deep: report %s" % report.relative_to(path.parent.parent)]


RUNNERS = {"tick": tick, "sweep": sweep, "audit": audit, "deep": deep}


def run_one(beam_path: Path, name: str, env: Optional[Env] = None, now=None, holder: str = "inline") -> list:
    """One cadence under its lease; the result is recorded in cadence.json."""
    import selfdrive

    path = Path(beam_path)
    cfg = selfdrive.config(None, path)
    started = time.time()
    if not selfdrive.take_lease(path, name, cfg, started, holder):
        return ["%s: another copy holds the lease; skipped" % name]
    try:
        lines = RUNNERS[name](path, env=env, now=now)
    except Exception as exc:
        lines = ["%s: failed (%s)" % (name, exc)]
    selfdrive.finish(path, name, time.time(), lines if holder == "background" else [], time.time() - started)
    return lines


def due_lines(beam_path: Path, env: Optional[Env] = None, now=None, inline: Optional[bool] = None) -> list:
    """What `wake.py run` adds after its supervise pass: the tick every wake, and each due sweep, audit, deep job
    (in the background under a lease unless cadence.background is off). Background results from earlier jobs print
    here, once."""
    import selfdrive

    path = Path(beam_path)
    cfg = selfdrive.config(None, path)
    out = selfdrive.take_pending(path)
    stamp = time.time()
    names = selfdrive.due(path, cfg, stamp)
    if "tick" not in names:
        names = ["tick"] + names
    background = cfg["cadence"].get("background", True) and not selfdrive.in_test() and inline is not True
    for name in names:
        if name == "tick" or not background:
            out.extend(run_one(path, name, env=env, now=now))
        elif selfdrive.background(path, name):
            out.append("%s: started in the background under a lease; the next tick prints what it found" % name)
        else:
            out.extend(run_one(path, name, env=env, now=now))
    return out
