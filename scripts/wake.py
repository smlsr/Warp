#!/usr/bin/env python3
"""The watch timer: one recurring timer (a subscription) per run that wakes the parent every `watchSeconds` (default
300; 0 is off) and keeps the whole lifecycle moving: open, corrected, and going (1.7.11).

`/warp-start` and `/warp-resume` print `timer: MUST DO` with its name (`warp-<run>-<instance>-watch`) and the command
that prints its prompt. Each wake runs `wake.py run`, which does in one call what a parent woken by a timer did by
hand, in this order:

  1. heartbeat         parent-alive --every <watchSeconds>, so the listener treats the parent as alive
  2. commands          inbound.py apply-pending
  3. unstick           capped and parked tickets get a fresh budget (the auto-scan, forced), a lock-escape whose paths
                       nobody holds is widened (beam and _ingested_schedule.json), a stale stop on a ticket with an
                       open pull request goes back to reviewing
  4. the pass          orchestrator.py supervise: starts, resumes, repairs, pull requests, reviews, merges, Jira, Slack
  5. new work          a blocker no ticket and no run owns is named, with the command that adds it (pack, beam, Jira)
  6. close             `wake: done …` and what to do with the lines above

A wake that fires while a pass runs, or within a few seconds of the last wake (stacked fires), does nothing. A pass
that sees no wake for three intervals says the timer is gone and how to create it again. A run that is paused,
stopped, or finished says to delete it.

  python3 scripts/wake.py prompt --beam .warp/beam.json      the timer's prompt for this run
  python3 scripts/wake.py armed --beam .warp/beam.json --name warp-ui-db7970-watch   record that it exists
  python3 scripts/wake.py run --beam .warp/beam.json         one wake (what the timer runs)
  python3 scripts/wake.py status --beam .warp/beam.json      the timer's name, interval, and last wake
  python3 scripts/wake.py setup --beam .warp/beam.json       the timer lines /warp-start prints
  python3 scripts/wake.py add-ticket --beam .warp/beam.json --id UI-90 --summary "…" --from UI-12 [--locks a,b]
          [--size S|M|L|XL] [--module UI] [--deps UI-03,UI-04] add a ticket for a blocker nobody owns

?, help, -h, and --help print this text.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

HELP = __doc__
DEFAULT_SECONDS = 300
GONE_INTERVALS = 3
REMIND_MINUTES = 30
FALSE_STOPS = {"stuck", "stalled", "stale", "worker-died"}


def interval(data: dict, beam_path: Optional[Path]) -> int:
    """Seconds between wakes; 0 is off. A value under 60 is read as 60."""
    import beam as beam_mod

    try:
        if beam_path is not None:
            seconds = int(beam_mod.config_int(data, Path(beam_path), "watchSeconds", DEFAULT_SECONDS))
        else:
            raw = (data.get("config") or {}).get("watchSeconds", DEFAULT_SECONDS)
            seconds = int(raw) if raw not in (None, "") else DEFAULT_SECONDS
    except (TypeError, ValueError):
        seconds = DEFAULT_SECONDS
    return max(60, seconds) if seconds > 0 else 0


def timer_name(data: dict) -> str:
    import agents

    run = str(data.get("run") or "").strip()
    inst = agents.instance_id(data) or "run"
    return "warp-%s%s-watch" % ("%s-" % run if run else "", inst)


def _record(data: dict) -> dict:
    raw = data.get("wake") if isinstance(data.get("wake"), dict) else {}
    data["wake"] = raw
    return raw


def _scripts() -> str:
    return str(Path(__file__).resolve().parent)


def _running(data: dict) -> bool:
    import orchestrator

    return orchestrator.listener_should_run(data)


def setup_lines(beam_path: Path, data: dict) -> list:
    """What /warp-start and /warp-resume print so the parent keeps the timer. Empty when watchSeconds is 0."""
    every = interval(data, beam_path)
    if not every:
        return ["timer: off (watchSeconds 0)"]
    beam_file = Path(beam_path).resolve()
    name = timer_name(data)
    return [
        "timer: MUST DO keep one recurring timer (a subscription) named %s that fires every %d seconds. Create it now if "
        "it is not there, and delete any older Warp timer for this run (one timer per run). Its prompt is the output of: "
        "python3 %s/wake.py prompt --beam %s" % (name, every, _scripts(), beam_file),
        "timer: then record it: python3 %s/wake.py armed --beam %s --name %s" % (_scripts(), beam_file, name),
    ]


def prompt_text(beam_path: Path, data: dict) -> str:
    """The timer's prompt: self-contained, so a wake in a new turn knows what to do."""
    every = interval(data, beam_path) or DEFAULT_SECONDS
    beam_file = Path(beam_path).resolve()
    root = beam_file.parent.parent
    scripts = _scripts()
    name = timer_name(data)
    try:
        import herald_fmt

        cfg = herald_fmt.read_config(root)
    except Exception:
        cfg = {}
    channel = cfg.get("slackChannel") or cfg.get("teamsChannel") or "the Warp channel"
    run = str(data.get("run") or "") or "(unnamed)"
    import agents

    return "\n".join([
        "%s: Warp watch timer for run %s (warp:%s), every %d seconds." % (name, run, agents.instance_id(data) or "?", every),
        "Goal: keep the whole lifecycle open, corrected, and moving. Unblock and fix what is stuck, start what can start, "
        "and add work where a blocker has no owner. Never wait for a person when a line below tells you what to do.",
        "",
        "1. Run the wake (heartbeat, pending commands, unstick, the supervise pass, new work), from %s:" % root,
        "   python3 %s/wake.py run --beam %s" % (scripts, beam_file),
        "   `wake: stacked …` or `wake: a pass is running …` means this fire does nothing more: end here with no summary.",
        "   `wake: the run is paused|stopped|finished` means delete this timer (%s) and end." % name,
        "2. Act on every line the wake printed, the way the Warp skill says. In particular:",
        "   - Start or resume every Shuttle a `start` or `resume` line names, and only those: run `checkout.py launch` with "
        "that line, give a background Shuttle the lines below its `prompt:` mark, on the model the prompt names, then "
        "bind the new cloud agent id with the `checkout.py bind` line it prints.",
        "   - Launch every alarm repair (`alarm-repair: launch <id>`) and every `fixer:` and `dispatch-base-fix` line.",
        "   - Do not merge by hand on a `merge: queue` or `merge: request` line: the pass merges. Only a `merge: MUST DO` is yours.",
        "   - Do not start a second listener while one is live: only a `listener: poll` line starts one.",
        "   - `pr: MUST DO <id>`: open the pull request with the GitHub MCP and record it with `orchestrator.py opened`.",
        "     A Shuttle's result line goes to `checkout.py result` only when it contains `ok` and was not recorded before.",
        "   - Reviews: launch the fix the pass prints for red CI. When Cursor Bugbot skipped a pull request (too large), "
        "post `bugbot run` on it. Record Bugbot only when GitHub shows a finished Bugbot check on that exact head commit.",
        "   - Jira: do every `jira: MUST DO` (In Progress, In Review, Done) and record each with the `jira_sync.py record` "
        "line it prints.",
        "   - `wake: new work` lines: create the ticket with the `wake.py add-ticket` command it prints, then its Jira issue, "
        "then record the key with `jira_sync.py resolve`.",
        "3. Slack: when the output says `herald: post .warp/notify-pass.json …`, post that payload to %s and run the "
        "`notify.py posted` command it names." % channel,
        "4. Close: python3 %s/session_note.py --type session-stop --root %s" % (scripts, root),
        "5. Reply in at most five lines: only what changed in this wake (started, merged, fixed, unstuck, added). "
        "Nothing changed: say `no change`.",
    ])


def armed(beam_path: Path, name: str, now=None) -> str:
    import beam as beam_mod
    import beamlock

    path = Path(beam_path)
    with beamlock.run_lock(path):
        data = beam_mod.load_json(path)
        now_s = beam_mod.coerce_now(now)[1]
        raw = _record(data)
        raw.update({"name": name or timer_name(data), "armedAt": now_s, "every": interval(data, path)})
        raw.pop("goneSince", None)
        raw.pop("goneAlarmed", None)
        beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
    return "timer: %s recorded, every %ds. Each wake runs wake.py run." % (raw["name"], raw["every"])


def check_lines(data: dict, beam_path: Optional[Path], now=None) -> list:
    """For each supervise pass: say the timer is gone (no wake for three intervals) and how to create it again, at most
    every 30 minutes, with one alarm per gap. Nothing until the timer was armed or has woken once. The caller writes
    the beam."""
    import beam as beam_mod

    every = interval(data, beam_path)
    raw = data.get("wake") if isinstance(data.get("wake"), dict) else {}
    if not every or not raw or not _running(data):
        return []
    last = beam_mod.parse_ts(str(raw.get("at") or raw.get("armedAt") or ""))
    if last is None:
        return []
    now_dt, now_s = beam_mod.coerce_now(now)
    quiet = (now_dt - last).total_seconds()
    if quiet < every * GONE_INTERVALS:
        return []
    raw.setdefault("goneSince", raw.get("at") or raw.get("armedAt"))
    said = beam_mod.parse_ts(str(raw.get("goneSaidAt") or ""))
    if said is not None and (now_dt - said).total_seconds() < REMIND_MINUTES * 60:
        return []
    raw["goneSaidAt"] = now_s
    name = raw.get("name") or timer_name(data)
    lines = ["timer: %s has not woken for %dm (every %ds). MUST DO create it again: its prompt is the output of "
             "python3 %s/wake.py prompt --beam %s, then run wake.py armed --name %s." % (
                 name, int(quiet // 60), every, _scripts(), Path(beam_path or ".warp/beam.json").resolve(), name)]
    if not raw.get("goneAlarmed"):
        raw["goneAlarmed"] = True
        lines.append("herald: ALARM the Warp watch timer %s has not woken for %dm. The parent is creating it again." % (name, int(quiet // 60)))
    return lines


def halt_lines(data: dict) -> list:
    """When the run is paused, stopped, or finished: delete the timer, once."""
    raw = data.get("wake") if isinstance(data.get("wake"), dict) else {}
    if not raw or raw.get("deleteSaid"):
        return []
    raw["deleteSaid"] = True
    return ["timer: MUST DO delete the watch timer %s: the run is not running. /warp-resume prints it again." % (
        raw.get("name") or timer_name(data))]


# ---- unstick -----------------------------------------------------------------------------------------------------


def _plan_widen(beam_path: Path, tid: str, locks: list) -> None:
    """Put a widened lock list in the plan file too, so a later restore from the plan keeps it."""
    import lockkeep

    path = Path(beam_path).parent / lockkeep.PLAN_FILE
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    changed = False
    for row in doc.get("tickets") or [] if isinstance(doc, dict) else []:
        if isinstance(row, dict) and str(row.get("id")) == str(tid):
            have = [str(p) for p in row.get("locks") or []]
            for lock in locks:
                if lock not in have:
                    have.append(lock)
                    changed = True
            row["locks"] = have
    if changed:
        path.write_text(json.dumps(doc, indent=2) + "\n")


def unstick(beam_path: Path, now=None) -> list:
    """Lock-escapes whose paths nobody holds are widened, stale stops on a ticket with an open pull request go back to
    reviewing, and the auto-scan runs now (capped and parked tickets get their fresh budget). Never raises."""
    import beam as beam_mod
    import heal
    import orchestrator

    path = Path(beam_path)
    lines: list = []
    try:
        data = beam_mod.load_json(path)
        now_s = beam_mod.coerce_now(now)[1]
        changed = False
        for tid, action, detail in heal.plan(data):
            ticket = data["tickets"][tid]
            if action not in {"clear", "widen"} or str(ticket.get("alarm") or "") != "lock-escape":
                continue
            if orchestrator.shuttle_still_out(ticket, data, now=now):
                continue
            widened = heal._paths(ticket)[1] if action == "widen" else []
            lines.append("wake: " + heal._clear_lock_escape(data, ticket, now_s, widened, now=now, beam_path=path))
            if widened:
                _plan_widen(path, tid, list(ticket.get("locks") or []))
            changed = True
        for tid, ticket in sorted((data.get("tickets") or {}).items(), key=lambda kv: str(kv[0])):
            if not isinstance(ticket, dict) or ticket.get("status") != "alarm":
                continue
            alarm = str(ticket.get("alarm") or "")
            url = str((ticket.get("pr") or {}).get("url") or "")
            if alarm not in FALSE_STOPS or not url or orchestrator.shuttle_still_out(ticket, data, now=now):
                continue
            ticket["status"] = "review"
            ticket["phase"] = "reviewing"
            ticket["alarm"] = None
            ticket["updatedAt"] = now_s
            watch = ticket.get("watch") if isinstance(ticket.get("watch"), dict) else None
            if watch is not None:
                watch.pop("stalledAt", None)
            lines.append("wake: %s %s alarm cleared; its pull request %s is open, so it goes back to reviewing" % (tid, alarm, url))
            changed = True
        if changed:
            beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
    except Exception as exc:
        lines.append("wake: unstick skipped (%s)" % exc)
    try:
        import autoscan

        lines.extend(autoscan.run_on_disk(path, now=now, force=True))
    except Exception as exc:
        lines.append("wake: auto-scan skipped (%s)" % exc)
    return lines


# ---- new work ----------------------------------------------------------------------------------------------------


def orphan_blockers(data: dict) -> list:
    """[(blocker, [waiting ticket ids])] for each blocker that is no ticket in this beam and no known run's."""
    import orchestrator
    import runs

    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    found: dict = {}
    for tid, ticket in tickets.items():
        if not isinstance(ticket, dict) or ticket.get("status") in orchestrator.SETTLED:
            continue
        for dep in orchestrator.blockers(ticket):
            if dep in tickets or runs.external_status(data, dep) is not None:
                continue
            if ":" in dep:
                continue  # another run's ticket: read from its published beam, named by runs.py when missing
            found.setdefault(dep, []).append(str(tid))
    return sorted((dep, sorted(set(ids))) for dep, ids in found.items())


def new_work_lines(beam_path: Path, data: dict) -> list:
    out = []
    beam_file = Path(beam_path).resolve()
    for dep, waiting in orphan_blockers(data):
        first = data["tickets"][waiting[0]]
        out.append("wake: new work: %s blocks %s, and no ticket or run owns it. MUST DO add it: python3 %s/wake.py add-ticket "
                   "--beam %s --id %s --summary \"<what %s must deliver for %s>\" --from %s%s, then create its Jira issue "
                   "and record the key with jira_sync.py resolve --ticket %s --key <KEY>." % (
                       dep, ", ".join(waiting), _scripts(), beam_file, dep, dep, waiting[0], waiting[0],
                       (" --module %s" % first.get("module")) if first.get("module") else "", dep))
    return out


def _pack_ticket_file(root: Path) -> Optional[Path]:
    import acceptance

    try:
        files = acceptance.ticket_files(root, None)
    except Exception:
        files = []
    for rel in files:
        path = root / rel
        if path.name in {"tickets.json"} and path.is_file():
            return path
    return None


def add_ticket(beam_path: Path, tid: str, summary: str, from_id: str = "", locks: Optional[list] = None,
               size: str = "M", module: str = "", deps: Optional[list] = None, now=None) -> list:
    """Add a ticket to the beam (queued), to the pack's tickets.json when the run has one, and to
    `.warp/added-tickets.json`. The Jira issue is the parent's (MUST DO line)."""
    import beam as beam_mod
    import beamlock

    path = Path(beam_path)
    tid = str(tid).strip()
    if not tid or not summary.strip():
        return ["refuse: add-ticket needs --id and --summary"]
    lines = []
    with beamlock.run_lock(path):
        data = beam_mod.load_json(path)
        tickets = data.setdefault("tickets", {})
        if tid in tickets:
            return ["wake: %s is already a ticket in this run (%s)" % (tid, tickets[tid].get("status"))]
        now_s = beam_mod.coerce_now(now)[1]
        source = tickets.get(from_id) if from_id else None
        size = str(size or "M").upper() if str(size or "M").upper() in {"S", "M", "L", "XL"} else "M"
        row = {
            "id": tid, "status": "queued", "summary": summary.strip(), "size": size,
            "module": module or (source or {}).get("module"), "repo": (source or {}).get("repo"),
            "lane": (source or {}).get("lane"), "gate": (source or {}).get("gate"),
            "locks": [str(p).strip() for p in (locks or []) if str(p).strip()], "deps": list(deps or []),
            "autoMerge": size in {"S", "M"}, "acs": [], "pr": {}, "attempts": 0, "alarm": None,
            "jiraKey": None, "jiraKeySource": "manual", "addedBy": "wake", "addedFor": from_id or None,
            "addedAt": now_s, "priority": "high", "updatedAt": now_s,
        }
        tickets[tid] = row
        beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
    lines.append("wake: %s added to the beam (queued, %s, %d lock%s)%s" % (
        tid, size, len(row["locks"]), "" if len(row["locks"]) == 1 else "s", (" for %s" % from_id) if from_id else ""))
    if not row["locks"]:
        lines.append("wake: %s has no locks yet; it starts once it has some (add them to the beam or rerun add-ticket)." % tid)
    log = path.parent / "added-tickets.json"
    try:
        rows = json.loads(log.read_text()) if log.is_file() else []
    except (OSError, ValueError):
        rows = []
    rows.append({"id": tid, "summary": row["summary"], "for": from_id or None, "at": row["addedAt"], "locks": row["locks"], "size": size})
    log.write_text(json.dumps(rows, indent=2) + "\n")
    import runs

    pack = _pack_ticket_file(runs.repo_root(path))
    if pack is not None:
        try:
            doc = json.loads(pack.read_text())
            items = doc.get("tickets") if isinstance(doc, dict) else doc
            if isinstance(items, list) and not any(isinstance(t, dict) and str(t.get("tempId") or t.get("id")) == tid for t in items):
                items.append({"tempId": tid, "summary": row["summary"], "issueType": "Task", "layer": "backend",
                              "size": size, "blockedByTempIds": row["deps"], "why": "Blocks %s; added by the Warp watch." % (from_id or "a ticket"),
                              "what": row["summary"], "acceptanceCriteria": [],
                              "technicalDetails": {"services": row["locks"], "apiContract": "", "dataMigration": "", "configKeys": []}})
                pack.write_text(json.dumps(doc, indent=2) + "\n")
                lines.append("wake: %s added to the pack's %s" % (tid, pack.relative_to(runs.repo_root(path))))
        except (OSError, ValueError) as exc:
            lines.append("wake: %s not added to the pack (%s)" % (tid, exc))
    cfg = dict(data.get("config") or {})
    try:
        import herald_fmt

        cfg.update({k: v for k, v in herald_fmt.read_config(runs.repo_root(path)).items() if v not in (None, "")})
    except Exception:
        pass
    project = cfg.get("jiraProject") or "<project>"
    lines.append("jira: MUST DO create a Jira issue in %s: summary \"[%s] %s\", label warp:%s, through the connected Jira MCP "
                 "(createJiraIssue), then record it: python3 %s/jira_sync.py resolve --ticket %s --key <KEY>." % (
                     project, tid, row["summary"], tid, _scripts(), tid))
    return lines


# ---- one wake ----------------------------------------------------------------------------------------------------


def _captured(fn, *args) -> list:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            fn(*args)
        except SystemExit:
            pass
    return [line for line in out.getvalue().splitlines() if line.strip()]


def run(beam_path: Path, now=None, supervise=None) -> list:
    """One wake. Lines for the parent to act on."""
    import beam as beam_mod
    import beamlock
    import inbound
    import orchestrator

    path = Path(beam_path)
    if not path.is_file():
        return ["wake: no beam at %s" % path]
    data = beam_mod.load_json(path)
    if not _running(data):
        state = "finished" if (data.get("runState") or "running") == "running" else str(data.get("runState") or "stopped")
        return ["wake: the run is %s. Delete the watch timer %s and end; /warp-resume prints it again." % (
            state, (data.get("wake") or {}).get("name") or timer_name(data))]
    every = interval(data, path) or DEFAULT_SECONDS
    now_dt, now_s = beam_mod.coerce_now(now)
    last = beam_mod.parse_ts(str((data.get("wake") or {}).get("at") or "")) if isinstance(data.get("wake"), dict) else None
    if last is not None and (now_dt - last).total_seconds() < max(30, every * 0.4):
        return ["wake: stacked (the last wake ran %ds ago); this fire does nothing" % int((now_dt - last).total_seconds())]
    with beamlock.try_run_lock(path) as held:
        if not held:
            return ["wake: a pass is running now; it covers this fire"]
        data = beam_mod.load_json(path)
        raw = _record(data)
        raw.update({"at": now_s, "count": int(raw.get("count") or 0) + 1, "every": every,
                    "name": raw.get("name") or timer_name(data)})
        for key in ("goneSince", "goneSaidAt", "goneAlarmed", "deleteSaid"):
            raw.pop(key, None)
        count = raw["count"]
        # Recorded first, so the pass below sees the timer alive.
        beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
        lines = ["wake: %s at %sZ (wake %d)" % (raw["name"], now_s[11:19], count)]
        lines.extend(orchestrator.parent_alive(path, every=every, now=now))
        lines.extend("commands: " + line for line in _captured(inbound.main, ["apply-pending", "--beam", str(path)]))
        lines.extend(unstick(path, now=now))
        lines.extend((supervise or orchestrator.supervise)(path, now=now))
        lines.extend(new_work_lines(path, beam_mod.load_json(path)))
        lines.append("wake: done (wake %d). Act on the lines above, post the Slack payload if one is named, then run "
                     "session_note.py --type session-stop. Reply with only what changed." % count)
    return lines


def status_lines(beam_path: Path, now=None) -> list:
    import beam as beam_mod

    path = Path(beam_path)
    data = beam_mod.load_json(path)
    raw = data.get("wake") if isinstance(data.get("wake"), dict) else {}
    every = interval(data, path)
    if not every:
        return ["timer: off (watchSeconds 0)"]
    name = raw.get("name") or timer_name(data)
    if not raw.get("at"):
        return ["timer: %s every %ds; %s, no wake yet" % (name, every, "armed at %s" % raw["armedAt"] if raw.get("armedAt") else "not armed")]
    last = beam_mod.parse_ts(str(raw["at"]))
    age = int((beam_mod.coerce_now(now)[0] - last).total_seconds() // 60) if last else -1
    return ["timer: %s every %ds; last wake %dm ago (%s, wake %s)" % (name, every, age, raw["at"], raw.get("count") or 0)]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="The Warp watch timer", epilog=HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("prompt", "armed", "run", "status", "setup", "add-ticket"):
        p = sub.add_parser(name)
        p.add_argument("--beam", default=".warp/beam.json")
        if name == "armed":
            p.add_argument("--name", default="")
        if name == "add-ticket":
            p.add_argument("--id", required=True)
            p.add_argument("--summary", required=True)
            p.add_argument("--from", dest="from_id", default="")
            p.add_argument("--locks", default="", help="comma-separated lock paths")
            p.add_argument("--size", default="M")
            p.add_argument("--module", default="")
            p.add_argument("--deps", default="", help="comma-separated blockers of the new ticket")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    import runs as _runs

    _runs.use_live_beam(args)
    path = Path(args.beam)
    if not path.is_file():
        print("wake: no beam at %s" % path)
        return 2
    import beam as beam_mod

    if args.cmd == "prompt":
        print(prompt_text(path, beam_mod.load_json(path)))
        return 0
    if args.cmd == "setup":
        for line in setup_lines(path, beam_mod.load_json(path)):
            print(line)
        return 0
    if args.cmd == "armed":
        print(armed(path, args.name))
        return 0
    if args.cmd == "status":
        for line in status_lines(path):
            print(line)
        return 0
    if args.cmd == "add-ticket":
        split = lambda text: [item.strip() for item in str(text or "").split(",") if item.strip()]
        for line in add_ticket(path, args.id, args.summary, from_id=args.from_id, locks=split(args.locks), size=args.size,
                               module=args.module, deps=split(args.deps)):
            print(line)
        return 0
    for line in run(path):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
