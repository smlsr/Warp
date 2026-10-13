#!/usr/bin/env python3
"""build-schedule/2: the schedule.json the HOS, HOS-Admin, FORGEQA and WARPDEMO packs write.

One file per program: `schema: "build-schedule/2"`, a `model` (slots, merge rule, clock, priority),
`numbers`, `critical_chain`, `lanes`, `repos`, `gates`, `tickets` and `steps`. Warp reads it like this:

  ticket                      Warp
  h                           hours
  after                       blockers (run:id names a ticket in another run)
  gate ("" = none)            gate
  rank, critical, wave        rank, critical (critical chain first), wave
  priority_gate               dispatched after the critical chain, ahead of rank
  checks [{name, check}]      checks to run when the ticket merges (shown, kept on the beam)
  slot, start, merge, ready   the planned picture; the dashboard draws it as written
  repo, lane, module, layer   kept; locks written `<repo>/<path>` lose the repo prefix
  locks                       lock paths, relative to the ticket's repo

  gate                        Warp
  if_red, on_green            ifRed, onGreen (printed when the gate turns green)
  blocking, scope             a red blocking gate stops everyone; a red non-blocking gate stops
                              the tickets that need its members. A pending gate holds nothing:
                              build-schedule/2 gates are milestones ("nothing waits for a gate").
  priority, starts, siblings  kept and shown; starts names the programs that may start at this gate
  at                          gate order (blocks and ranges follow it, so G-RBAC sits between G0 and G1)

`model.gate_order: "strict"` keeps Warp's own rule instead: gate blocks run in order and a pending
blocking gate holds the tickets that need its members. Config `gateOrder` overrides both.

Several repos: a run works in one repo. When `repos` lists more than one and this checkout is one
of them (config `repo`, else the origin's name), scan keeps that repo's tickets. A blocker in
another repo becomes `<run>:<id>`, where the run is `repoRuns[<repo>]` (config), else the repo name
in lower case. Warp reads that run's published beam from the other repo when `externalRepos`
names it (config: run → git URL or local path).

  python3 scripts/buildsched.py ?
  python3 scripts/buildsched.py show <schedule.json>       summary: tickets per repo, gates, cross-repo blockers
  python3 scripts/buildsched.py show <schedule.json> --repo HumanifyOS

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

HELP = __doc__
SCHEMA = "build-schedule/2"
SIZE_HOURS = {"S": 4, "M": 7, "L": 11, "XL": 16}


def is_v2(data) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("tickets"), list):
        return False
    if data.get("schema") == SCHEMA:
        return True
    first = data["tickets"][0] if data["tickets"] else {}
    return isinstance(first, dict) and "h" in first and "after" in first and "slot" in first and "critical_chain" in data


def parse_map(raw) -> dict:
    """`{a: b, c: d}`, `a=b, c=d`, JSON, or a dict -> {"a": "b", "c": "d"}."""
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip()}
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        got = json.loads(text)
        if isinstance(got, dict):
            return {str(k).strip(): str(v).strip() for k, v in got.items()}
    except (ValueError, TypeError):
        pass
    text = text.strip("{}")
    out = {}
    for piece in re.split(r"[,\n]+", text):
        if not piece.strip():
            continue
        m = re.match(r"\s*['\"]?([^:='\"]+?)['\"]?\s*[:=]\s*(.+?)\s*$", piece)
        if m:
            out[m.group(1).strip()] = m.group(2).strip().strip("'\"")
    return out


def _checks(raw) -> list:
    out = []
    for c in raw or []:
        if isinstance(c, dict):
            out.append({"name": str(c.get("name") or "").strip(), "check": str(c.get("check") or c.get("command") or "").strip()})
        elif str(c).strip():
            out.append({"name": "", "check": str(c).strip()})
    return out


def strip_repo(lock: str, repo: Optional[str], root: Optional[Path]) -> str:
    """`HumanifyOS/apps/x` -> `apps/x` when the ticket's repo is HumanifyOS and this checkout has no
    top-level folder of that name (a workspace that holds both repos keeps the prefix)."""
    text = str(lock or "").strip()
    if not repo or not text.startswith(repo + "/"):
        return text
    if root is not None and (Path(root) / repo).is_dir():
        return text
    return text[len(repo) + 1:]


def gate_order(data: dict) -> str:
    raw = str(((data.get("model") or {}).get("gate_order")) or "").strip().lower()
    return "strict" if raw == "strict" else "deps"


def parse(data: dict, path: Path, root: Optional[Path] = None) -> dict:
    """A Warp graph from a build-schedule/2 document."""
    mode = gate_order(data)
    hold = "green" if mode == "strict" else "red"
    tickets = []
    for t in data.get("tickets") or []:
        tid = t.get("id") or t.get("tempId")
        if not tid:
            continue
        repo = t.get("repo") or None
        size = str(t.get("size") or "M").strip().upper()
        size = size if size in SIZE_HOURS else "M"
        raw_key = t.get("jiraKey") or t.get("jira")
        tickets.append({
            "id": str(tid),
            "jiraKey": raw_key or None,
            "jiraKeySource": "plan" if raw_key else None,
            "jiraKeyConfidence": None,
            "jiraKeyForced": False,
            "summary": t.get("summary") or t.get("title") or "",
            "deps": list(t.get("deps") or []),
            "after": [str(a) for a in t.get("after") or []],
            "locks": [strip_repo(l, repo, root) for l in t.get("locks") or []],
            "starred": bool(t.get("starred") or t.get("star")),
            "rank": t.get("rank"),
            "size": size,
            "hours": t.get("h") if t.get("h") not in (None, "") else t.get("hours"),
            "critical": bool(t.get("critical")),
            "rankDays": 0,
            "gate": t.get("gate") or None,
            "module": t.get("module"),
            "layer": t.get("layer"),
            "lane": t.get("lane"),
            "repo": repo,
            "wave": t.get("wave"),
            "priority": None,
            "priorityGate": t.get("priority_gate") or None,
            "checks": _checks(t.get("checks")),
            "screens": list(t.get("screens") or []),
            "mock": t.get("mock"),
            "planned": {k: t.get(k) for k in ("slot", "start", "merge", "ready", "latest_start") if t.get(k) is not None} or None,
            "acs": t.get("acs"),
        })
    gates = []
    for g in data.get("gates") or []:
        if not isinstance(g, dict) or not g.get("key"):
            continue
        gates.append({
            "key": str(g["key"]),
            "name": g.get("name"),
            "blocking": bool(g.get("blocking", True)),
            "scope": g.get("scope"),
            "members": [str(m) for m in g.get("members") or []],
            "checks": [c if isinstance(c, str) else (c.get("check") or c.get("name") or "") for c in g.get("checks") or []],
            "ifRed": g.get("ifRed") or g.get("if_red"),
            "onGreen": g.get("onGreen") or g.get("on_green"),
            "priority": bool(g.get("priority")),
            "starts": list(g.get("starts") or []),
            "siblings": [s.get("program") if isinstance(s, dict) else str(s) for s in g.get("siblings") or []],
            "at": g.get("at"),
            "hold": hold,
        })
    model = data.get("model") or {}
    numbers = data.get("numbers") or {}
    return {
        "tickets": tickets,
        "gates": gates,
        "criticalPath": [str(c) for c in data.get("critical_chain") or data.get("criticalPath") or []],
        "source": str(path),
        "format": SCHEMA,
        "schedule": {
            "schema": SCHEMA,
            "program": data.get("program"),
            "rebuilt": data.get("rebuilt"),
            "repos": list(data.get("repos") or []),
            "lanes": list(data.get("lanes") or []),
            "gateOrder": mode,
            "slots": model.get("slots"),
            "priority": model.get("priority"),
            "finishHours": numbers.get("finish_h"),
            "agentHours": numbers.get("agent_hours"),
            "criticalHours": numbers.get("critical_path_h"),
            "peak": numbers.get("peak"),
            "waves": numbers.get("waves"),
        },
    }


def pick_repo(graph: dict, root: Path, cfg: dict) -> tuple:
    """(repo or None, lines). Keeps one repo's tickets when the schedule names several."""
    sched = graph.get("schedule") or {}
    repos = [r for r in sched.get("repos") or [] if r]
    ticket_repos = sorted({t.get("repo") for t in graph["tickets"] if t.get("repo")})
    if len(set(repos) | set(ticket_repos)) < 2:
        return (ticket_repos[0] if ticket_repos else (repos[0] if repos else None)), []
    every = sorted(set(repos) | set(ticket_repos))
    wanted = str(cfg.get("repo") or "").strip()
    if not wanted:
        try:
            import herald_fmt

            wanted = herald_fmt.repo_name(root)
        except Exception:
            wanted = root.name
    match = next((r for r in every if r.casefold() == wanted.casefold()), None)
    if match is None:
        if all((root / r).is_dir() for r in every):
            return None, [f"repos: {', '.join(every)} are folders of this checkout; all tickets kept, locks keep their repo prefix"]
        return None, [
            f"repos: the schedule spans {', '.join(every)} and this checkout is {wanted!r}. All tickets kept.",
            "repos: set `repo: <name>` in .warp/config.yaml to run one repo's tickets here.",
        ]
    run_of = {k.casefold(): v for k, v in parse_map(cfg.get("repoRuns")).items()}
    other_run = lambda r: run_of.get(r.casefold()) or re.sub(r"[^a-z0-9-]+", "-", r.casefold()).strip("-")
    keep = [t for t in graph["tickets"] if (t.get("repo") or match) == match]
    gone = {t["id"]: t.get("repo") for t in graph["tickets"] if (t.get("repo") or match) != match}
    rewired = []
    for t in keep:
        for key in ("deps", "after"):
            new = []
            for d in t.get(key) or []:
                if d in gone:
                    new.append("%s:%s" % (other_run(gone[d]), d))
                    rewired.append((t["id"], new[-1]))
                else:
                    new.append(d)
            t[key] = new
    graph["tickets"] = keep
    for g in graph.get("gates") or []:
        others = [m for m in g["members"] if m in gone]
        if others:
            g["otherMembers"] = others
        g["members"] = [m for m in g["members"] if m not in gone]
    graph["criticalPath"] = [c for c in graph.get("criticalPath") or [] if c not in gone]
    graph.setdefault("schedule", {})["repo"] = match
    lines = [f"repo: {match} — {len(keep)} of {len(keep) + len(gone)} tickets; the other {len(gone)} belong to " +
             ", ".join(f"{r} (run {other_run(r)})" for r in every if r != match)]
    if rewired:
        lines.append("deps: blockers in another repo, read from that run's published beam: " +
                     ", ".join(f"{a} ← {b}" for a, b in rewired[:12]) + (f" (+{len(rewired) - 12})" if len(rewired) > 12 else ""))
        ext = parse_map(cfg.get("externalRepos"))
        missing = sorted({b.split(":", 1)[0] for _, b in rewired} - set(ext))
        if missing:
            lines.append("deps: set externalRepos in .warp/config.yaml so Warp can read " + ", ".join(missing) +
                         " (for example externalRepos: {" + ", ".join(f"{m}: <git url>" for m in missing) + "}); until then those tickets wait")
    emptied = [g["key"] for g in graph.get("gates") or [] if not g["members"]]
    if emptied:
        lines.append("gates: no members in this repo: " + ", ".join(emptied) + " (reached when every ticket here is merged)")
    return match, lines


def summary(path: Path, repo: str = "") -> list:
    data = json.loads(Path(path).read_text())
    if not is_v2(data):
        return [f"{path}: not build-schedule/2"]
    g = parse(data, Path(path))
    lines = [f"schedule: {data.get('program')} {SCHEMA} tickets={len(g['tickets'])} gates={len(g['gates'])} gateOrder={g['schedule']['gateOrder']} finish=T+{g['schedule']['finishHours']}h"]
    by = {}
    for t in g["tickets"]:
        by.setdefault(t.get("repo") or "-", []).append(t["id"])
    for r, ids in sorted(by.items()):
        lines.append(f"repo: {r} {len(ids)} tickets")
    ids = {t["id"]: t for t in g["tickets"]}
    cross = [(t["id"], d) for t in g["tickets"] for d in t["after"] if d in ids and ids[d].get("repo") != t.get("repo")]
    if cross:
        lines.append("cross-repo: " + ", ".join(f"{a} ← {b}" for a, b in cross))
    for gt in sorted(g["gates"], key=lambda x: (x["at"] is None, x["at"] or 0)):
        lines.append(f"gate: {gt['key']} at T+{gt['at']}h {'blocking' if gt['blocking'] else 'scope: ' + str(gt['scope'])}"
                     + (" priority" if gt["priority"] else "") + (f" starts {', '.join(gt['starts'])}" if gt["starts"] else "")
                     + (f" on_green: {gt['onGreen']}" if gt["onGreen"] else ""))
    if repo:
        graph = parse(data, Path(path))
        cfg = {}
        local = Path(".warp/config.yaml")
        if local.is_file():
            try:
                import prompt_gate

                cfg = {k: v for k, v in prompt_gate.yaml_scalars(local.read_text()).items() if k in ("repoRuns", "externalRepos")}
            except Exception:
                cfg = {}
        cfg["repo"] = repo
        _, more = pick_repo(graph, Path("."), cfg)
        lines += more
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="build-schedule/2", epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("path")
    s.add_argument("--repo", default="")
    import usage

    args = ap.parse_args(usage.normalize_argv(argv))
    for line in summary(Path(args.path), args.repo):
        print(line)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
