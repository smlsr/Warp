#!/usr/bin/env python3
"""The auto-scan: every `autoScan` seconds (default 600), a thorough pass over every open ticket to unstick work.

The per-pass checks (reconcile, the wake, the fixer) handle what moves on its own. The auto-scan handles what a
cap or a lost step left standing, then makes room for new work:

  1. Capped and dead-ended alarms get a fresh budget, the way /warp-start and /warp-resume heal them:
     `worker-died` and `stuck` at `maxRecoveries`, `spawn-cap` (the ticket's spawn count starts again),
     a lock-escape whose repairs were given up (cleared or widened when its paths are free, its repair count
     reset otherwise), `ci-red` and `bugbot-failed`, and tickets parked at `maxFixAttempts` or `maxStallFixes`.
     With a pull request the ticket goes back to fix on it; without one it is queued and reuses its branch.
     Any other alarm that is still there one scan later gets the same. `gate-red` is left alone.
     Each ticket gets at most `autoScanResets` (default 5) of these; past that it is named and left for a person.
  2. A merge asked for and not confirmed for one scan interval is asked again.
  3. A pull request the run asked to open and never did is asked again.
  4. The run-wide repair sweep (`repairSweepMinutes`) runs now: stalled reviews (Bugbot asked again, CI rerun,
     rebase), orphan locks, the listener, the beam against main.
  5. The pass that runs it then refills every free Shuttle slot from the ready queue, as every pass does.

It runs inside a supervise pass (after reconcile, before the pipeline), and `parent-exit --wait` returns
`pass: work autoscan` when it is due, so an idle run still gets it. `autoScan: 0` (or an empty value) turns it
off. Every action prints an `autoscan:` line; when it did anything, Slack gets one summary line.

  python3 scripts/autoscan.py --beam .warp/beam.json            run it now, due or not
  python3 scripts/autoscan.py --beam .warp/beam.json --dry-run  list what it would do

?, help, -h, and --help print this text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

HELP = __doc__
DEFAULT_SECONDS = 600
DEFAULT_RESETS = 5
LEAVE = {"gate-red"}
CAPPED_PARK = {"parked", "stalled"}


def interval(data: dict, beam_path: Optional[Path]) -> int:
    """Seconds between scans; 0 is off. A value under 60 is read as 60."""
    import beam as beam_mod

    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    raw = cfg.get("autoScan", DEFAULT_SECONDS)
    if beam_path is not None:
        path = Path(beam_path).parent / "config.yaml"
        if path.is_file():
            import prompt_gate

            text = prompt_gate.yaml_scalars(path.read_text())
            if "autoScan" in text:
                raw = text["autoScan"]
    if raw in (None, "") or str(raw).strip().casefold() in {"0", "off", "false", "no"}:
        return 0
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        return DEFAULT_SECONDS
    del beam_mod
    return max(60, seconds) if seconds > 0 else 0


def due(data: dict, beam_path: Optional[Path], now=None) -> bool:
    import beam as beam_mod

    if (data.get("runState") or "running") != "running" or data.get("paused"):
        return False
    every = interval(data, beam_path)
    if not every:
        return False
    last = beam_mod.parse_ts(str((data.get("autoScan") or {}).get("at") or "")) if isinstance(data.get("autoScan"), dict) else None
    if last is None:
        return True
    now_dt = beam_mod.coerce_now(now)[0]
    return (now_dt - last).total_seconds() >= every


def _resets_cap(data: dict, beam_path: Optional[Path]) -> int:
    import beam as beam_mod

    try:
        return max(0, int(beam_mod.config_int(data, beam_path, "autoScanResets", DEFAULT_RESETS)))
    except Exception:
        return DEFAULT_RESETS


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def dead_end(data: dict, ticket: dict) -> str:
    """Why the pipeline will not move this ticket on its own, or empty."""
    cfg = data.get("config") or {}
    cap = _int(cfg.get("maxRecoveries"), 5)
    status = ticket.get("status")
    alarm = str(ticket.get("alarm") or "")
    if status == "parked":
        reason = str(ticket.get("parkReason") or "")
        if alarm in CAPPED_PARK or reason.startswith("fixer cap"):
            return "parked at %s" % (reason or ("maxFixAttempts" if alarm == "parked" else "maxStallFixes"))
        return ""
    if status != "alarm" or alarm in LEAVE:
        return ""
    if alarm == "worker-died" and _int(ticket.get("recoveries"), 0) >= cap:
        return "worker-died at maxRecoveries %d" % cap
    if alarm == "stuck" and _int(ticket.get("stuckRestarts"), 0) >= cap:
        return "stuck at maxRecoveries %d" % cap
    if alarm == "spawn-cap" or ticket.get("spawnCapped"):
        return "spawn-cap (maxSpawnsPerTicket)"
    if alarm == "lock-escape":
        info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
        if info.get("gaveUp") or info.get("state") == "gave-up":
            return "lock-escape repairs given up (maxAlarmRepairs)"
        return ""
    if alarm in {"ci-red", "bugbot-failed", "failed", "parked"}:
        return "%s at its fix cap" % alarm
    return ""


def _acs_pass(data: dict, ticket: dict) -> bool:
    import pipeline

    try:
        return bool(pipeline.acs_pass(ticket, data.get("_repoRoot")))
    except Exception:
        return False


def plan(data: dict, beam_path: Optional[Path] = None, now=None) -> list:
    """[(id, action, why)] without changing anything. action: reset, limit, remerge, reopen."""
    import beam as beam_mod
    import orchestrator

    now_dt = beam_mod.coerce_now(now)[0]
    cap = _resets_cap(data, beam_path)
    seen = ((data.get("autoScan") or {}).get("alarms") or {}) if isinstance(data.get("autoScan"), dict) else {}
    every = interval(data, beam_path) or DEFAULT_SECONDS
    rows = []
    for tid, t in sorted((data.get("tickets") or {}).items(), key=lambda kv: str(kv[0])):
        if not isinstance(t, dict):
            continue
        status = t.get("status")
        alarm = str(t.get("alarm") or "")
        why = dead_end(data, t)
        if not why and status == "alarm" and alarm not in LEAVE and seen.get(tid) == alarm:
            why = "%s still in alarm since the last scan" % alarm
        if why and status == "alarm" and alarm in {"worker-died", "stuck", "lock-escape"} and _acs_pass(data, t):
            # Its acceptance criteria pass: the pass opens its pull request (pipeline.open_despite_cap), no reset.
            continue
        if why and not orchestrator.shuttle_still_out(t, data, now=now):
            used = _int(t.get("autoScanResets"), 0)
            rows.append((str(tid), "reset" if used < cap else "limit", why))
            continue
        pr = t.get("pr") if isinstance(t.get("pr"), dict) else {}
        if status == "merging" and pr.get("mergeRequestedSha"):
            asked = beam_mod.parse_ts(str(pr.get("mergeRequestedAt") or ""))
            if asked is not None and (now_dt - asked).total_seconds() >= every:
                rows.append((str(tid), "remerge", "merge asked %dm ago and not confirmed" % ((now_dt - asked).total_seconds() // 60)))
                continue
        if status not in orchestrator.SETTLED and t.get("phase") == "pr-open" and not pr.get("url") and t.get("branch") \
                and not pr.get("openRequested") and not (t.get("shuttle") or {}).get("pending"):
            rows.append((str(tid), "reopen", "its pull request was never opened"))
    return rows


def run_on_disk(beam_path: Path, now=None, force: bool = False) -> list:
    """Run the scan when due (or forced) on the live beam. Lines for each action. Never raises."""
    import beam as beam_mod

    path = Path(beam_path)
    try:
        data = beam_mod.load_json(path)
    except (OSError, ValueError) as exc:
        return ["autoscan: not read (%s)" % exc]
    if not force and not due(data, path, now):
        return []
    if (data.get("runState") or "running") != "running" or data.get("paused"):
        return []
    try:
        return _run(path, data, now)
    except Exception as exc:
        return ["autoscan: stopped early (%s)" % exc]


def _run(path: Path, data: dict, now=None) -> list:
    import agents
    import beam as beam_mod
    import heal
    import inbound

    now_s = beam_mod.coerce_now(now)[1]
    rows = plan(data, path, now)
    lines = []
    retry = []
    done = {"reset": [], "limit": [], "remerge": [], "reopen": []}
    escapes = {tid: (action, detail) for tid, action, detail in heal.plan(data)}
    for tid, action, why in rows:
        t = data["tickets"][tid]
        if action == "limit":
            lines.append("autoscan: %s %s; it had %d auto-scan resets already, so it is left for a person (warp:retry %s)"
                         % (tid, why, _int(t.get("autoScanResets"), 0), tid))
            done["limit"].append(tid)
            continue
        if action == "remerge":
            t["pr"].pop("mergeRequestedSha", None)
            t["pr"].pop("mergeRequestedAt", None)
            t["status"] = "review"
            t["phase"] = "ready"
            lines.append("autoscan: %s %s; asking for the merge again" % (tid, why))
            done["remerge"].append(tid)
            continue
        if action == "reopen":
            t.setdefault("pr", {})["openRequested"] = True
            lines.append("autoscan: %s %s; opening it from %s" % (tid, why, t.get("branch")))
            done["reopen"].append(tid)
            continue
        t["autoScanResets"] = _int(t.get("autoScanResets"), 0) + 1
        if t.get("alarm") == "spawn-cap" or t.get("spawnCapped"):
            agents.reset_spawns(data, t, now=now, beam_path=path)
        heal_action = escapes.get(tid, ("", ""))[0] if t.get("alarm") == "lock-escape" else ""
        if heal_action in {"clear", "widen"}:
            widened = heal._paths(t)[1] if heal_action == "widen" else []
            lines.append("autoscan: %s %s: %s" % (tid, why, heal._clear_lock_escape(data, t, now_s, widened, now=now, beam_path=path)[len("heal: "):]))
        elif heal_action == "repair":
            t["alarmRepairs"] = 0
            info = t.get("alarmRepair") if isinstance(t.get("alarmRepair"), dict) else {}
            info.update({"gaveUp": False, "gaveUpPosted": False})
            if info.get("state") == "gave-up":
                info["state"] = "failed"
            t["alarmRepair"] = info
            lines.append("autoscan: %s %s; repair count reset (%s), it is repaired when the holder is done"
                         % (tid, why, escapes[tid][1]))
        else:
            retry.append((tid, why))
        done["reset"].append(tid)
    record = data.get("autoScan") if isinstance(data.get("autoScan"), dict) else {}
    record["at"] = now_s
    record["count"] = _int(record.get("count"), 0) + 1
    # The sweep runs in this same pass (watch.apply): stalled reviews, CI, rebases, orphan locks, the listener.
    sweep = data.get("repairSweep") if isinstance(data.get("repairSweep"), dict) else {}
    sweep["at"] = None
    data["repairSweep"] = sweep
    data["autoScan"] = record
    beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
    for tid, why in retry:
        text = inbound._requeue(path, tid, by="autoscan")
        lines.append("autoscan: %s %s: %s" % (tid, why, text.splitlines()[0]))
    after = beam_mod.load_json(path)
    record = after.get("autoScan") if isinstance(after.get("autoScan"), dict) else {}
    # What is still in alarm now: one scan later, anything still there is reset too.
    record["alarms"] = {str(tid): str(t.get("alarm") or "") for tid, t in (after.get("tickets") or {}).items()
                        if isinstance(t, dict) and t.get("status") == "alarm"}
    record["last"] = {key: value for key, value in done.items() if value}
    after["autoScan"] = record
    beam_mod.atomic_write(path, json.dumps(after, indent=2) + "\n", data=after)
    head = "autoscan: scan %d: %d reset, %d asked to merge again, %d pull requests to open, %d left for a person" % (
        record["count"], len(done["reset"]), len(done["remerge"]), len(done["reopen"]), len(done["limit"]))
    out = [head] + lines
    moved = done["reset"] or done["remerge"] or done["reopen"]
    if moved or done["limit"]:
        bits = []
        if done["reset"]:
            bits.append("reset %s" % ", ".join(done["reset"][:10]))
        if done["remerge"]:
            bits.append("asked to merge again: %s" % ", ".join(done["remerge"][:10]))
        if done["reopen"]:
            bits.append("opening pull requests: %s" % ", ".join(done["reopen"][:10]))
        if done["limit"]:
            bits.append("needs a person (auto-scan resets used up): %s" % ", ".join(done["limit"][:10]))
        if not moved and record.get("limitPosted") == sorted(done["limit"]):
            return out  # the same tickets left for a person: said once
        record["limitPosted"] = sorted(done["limit"])
        after["autoScan"] = record
        beam_mod.atomic_write(path, json.dumps(after, indent=2) + "\n", data=after)
        out.append("herald: Auto-scan: %s." % "; ".join(bits))
    return out


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="The auto-scan: unstick work, then start new work", epilog=HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--beam", default=".warp/beam.json")
    parser.add_argument("--dry-run", action="store_true")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    path = Path(args.beam)
    if not path.is_file():
        print("autoscan: no beam at %s" % path)
        return 2
    import beam as beam_mod

    if args.dry_run:
        rows = plan(beam_mod.load_json(path), path)
        for tid, action, why in rows or [("-", "nothing", "nothing to unstick")]:
            print("autoscan: would %s %s (%s)" % (action, tid, why))
        return 0
    import beamlock

    with beamlock.run_lock(path):
        for line in run_on_disk(path, force=True) or ["autoscan: the run is not running"]:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
