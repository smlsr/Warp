#!/usr/bin/env python3
"""What the AI really spent on each ticket (1.7.22): working time, code written, and why it went back.

Before 1.7.22 a ticket's `minutes` were the clock time of every Shuttle row in the agent registry, from its start until
Warp marked it ended, or until now while it still said starting or running, and the total never came down. On Forge-QA
that gave 20,973 agent-hours against ~5,000 estimated: phantom agents that never ran (3,429 h), 42 rows stuck in
`starting` since the day before (2,032 h and growing), rows left open until a sweep or the merge closed them (dead rows
6,463 h, merged rows 6,365 h), and replacement Shuttles on one ticket all counted at once (~3,800 h).

Working time now counts only the AI working, turn by turn:
  - measured run time wins: Cursor's `duration_ms` for the turn (the subagent hook), stored on the row as `runMs`
  - otherwise each turn of a row (a resumed row has several) runs from its start to the first of:
      its end; the next Shuttle's start on the same ticket (a replacement ends the one it replaces); the agent's last
      sign of life in the turn (a ticket-folder state, a heartbeat) plus `idleGraceMinutes` (5); its start plus
      `maxTurnMinutes` (120)
  - a turn with no sign of life at all counts at most `silentTurnMinutes` (30), and is marked estimated
  - a row closed with no end time (a bulk stop) ends at the next Shuttle's start or its start plus maxTurnMinutes,
    then the same bounds apply
  - a Shuttle that never launched (phantom, not started, still `starting` with no sign of life) counts 0
The total is recomputed every pass from the registry, never ratcheted; `droppedMinutes` says how much clock time was
left out as idle. A Shuttle's own `beam.py spend --minutes` report is kept apart, as `reportedMinutes`.

Every AI use is counted by step (`ai.runs`, `ai.minutes`): implement, fix, restart, repair, review, aireview, rebase.
A fix round is also counted by why it went back (`fixCounts`): `bug` (Bugbot, Warp's AI review, or a review that
failed), `ci` (CI, the rollup, the check command, a red merge check), `acceptance`, `conflict` (rebase, merge), `restart`
(no product commit), `stalled`, `other`. A round with Bugbot findings and red CI counts once in each, and once in
`rounds`.

Code: lines added and deleted outside `.warp/` and an approximate count of the output tokens that code took (added
characters / 4), from git (`<merge sha>^1..<merge sha>`, or `origin/<base>...origin/<branch>` while open). Approximate:
it measures the final diff, not every draft the model wrote.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Callable, Optional

DEFAULTS = {"maxTurnMinutes": 120, "idleGraceMinutes": 5, "silentTurnMinutes": 30, "codeBudget": 25}
CHARS_PER_TOKEN = 4.0
NEVER_RAN = ("phantom", "never launched", "no agent was started", "not-launched", "not launched")
FIX_KINDS = ("bug", "ci", "acceptance", "conflict", "restart", "stalled", "other")
STEPS = ("implement", "fix", "restart", "repair", "review", "aireview", "rebase", "verify", "work")


# ---- why a ticket went back --------------------------------------------------------------------------------------


def classify(reasons) -> list:
    """The kinds of one fix round, from its reasons (begin_fix's list, batched or not)."""
    kinds = []
    for raw in reasons or []:
        text = str(raw or "").strip()
        low = text.casefold()
        if not low or low.startswith(("one round, one push", "bugbot: fix every bugbot finding")):
            continue
        if low.startswith(("bugbot", "review:", "shuttle review fail", "ai review")) or "bugbot finding" in low:
            kind = "bug"
        elif low.startswith(("ci red", "rollup red", "check red", "merge check red", "ci:", "check:", "main red")) or \
                " ci " in " %s " % low and "red" in low:
            kind = "ci"
        elif low.startswith("ac "):
            kind = "acceptance"
        elif low.startswith(("rebase", "gap-report:", "conflict", "dirty")) or "merge main" in low or "conflict" in low:
            kind = "conflict"
        elif "without a product commit" in low or "is not done" in low or "no product commit" in low:
            kind = "restart"
        elif low.startswith("stalled"):
            kind = "stalled"
        else:
            kind = "other"
        if kind not in kinds:
            kinds.append(kind)
    return kinds or ["other"]


def note_fix(ticket: dict, reasons, now_s: str, head: str = "") -> list:
    """Count one fix round on the ticket. Returns its kinds (also kept for the launch row)."""
    kinds = classify(reasons)
    counts = ticket.get("fixCounts") if isinstance(ticket.get("fixCounts"), dict) else {}
    counts["rounds"] = int(counts.get("rounds") or 0) + 1
    for kind in kinds:
        counts[kind] = int(counts.get(kind) or 0) + 1
    ticket["fixCounts"] = counts
    log = ticket.get("fixLog") if isinstance(ticket.get("fixLog"), list) else []
    log.append({"at": now_s, "head": str(head or "")[:12], "kinds": kinds,
                "reasons": [str(r)[:160] for r in (reasons or []) if str(r).strip()][:6]})
    ticket["fixLog"] = log[-30:]
    ticket["fixKinds"] = kinds
    return kinds


# ---- signs of life ---------------------------------------------------------------------------------------------


def seen(ticket: dict, stamp: str) -> None:
    """A sign the Shuttle is working (heartbeat, a ticket-folder state). Kept for the working-time bound."""
    stamp = str(stamp or "").strip()
    if not stamp:
        return
    marks = ticket.get("aiSeen") if isinstance(ticket.get("aiSeen"), list) else []
    if stamp in marks[-5:]:
        return
    marks.append(stamp)
    ticket["aiSeen"] = sorted(set(marks))[-200:]


def _parse(stamp):
    import beam as beam_mod

    return beam_mod.parse_ts(str(stamp or "")) if stamp else None


def _folder_stamps(beam_path: Optional[Path], tid: str) -> list:
    out = []
    if beam_path is None:
        return out
    beam_dir = Path(beam_path).resolve().parent
    roots = [beam_dir / "tickets" / tid, beam_dir.parent / "tickets" / tid]
    try:
        roots.append(beam_dir.parents[2] / ".warp" / "tickets" / tid)
    except IndexError:
        pass
    for folder in roots:
        log = folder / "log.jsonl"
        if not log.is_file():
            continue
        try:
            for line in log.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("at"):
                    out.append(str(row["at"]))
        except OSError:
            continue
    return out


def stamps_for(ticket: dict, beam_path: Optional[Path]) -> list:
    marks = list(ticket.get("aiSeen") or [])
    if ticket.get("lastSeenAt"):
        marks.append(ticket["lastSeenAt"])
    marks.extend(_folder_stamps(beam_path, str(ticket.get("id") or "")))
    parsed = sorted({p for p in (_parse(m) for m in marks) if p is not None})
    return parsed


# ---- working time ----------------------------------------------------------------------------------------------


def _cfg(data: dict) -> dict:
    cfg = dict(DEFAULTS)
    raw = (data.get("config") or {}).get("effort")
    if isinstance(raw, dict):
        for key in DEFAULTS:
            try:
                cfg[key] = float(raw.get(key, cfg[key]))
            except (TypeError, ValueError):
                pass
    return cfg


def never_ran(row: dict) -> bool:
    reason = str(row.get("endReason") or "").casefold()
    return any(word in reason for word in NEVER_RAN)


def turns(row: dict, now_dt) -> list:
    """(start, end) of each turn of a row. A resumed row keeps its earlier turns in `turns`."""
    out = []
    for turn in row.get("turns") or []:
        if isinstance(turn, (list, tuple)) and len(turn) == 2:
            start, end = _parse(turn[0]), _parse(turn[1])
            if start and end and end >= start:
                out.append((start, end))
    start = _parse(row.get("turnStart") or row.get("resumedAt") or row.get("started"))
    if start is None:
        return out
    end = _parse(row.get("ended"))
    if end is None and str(row.get("state") or "") in {"starting", "running"}:
        end = now_dt
    if end is None:
        out.append((start, None))  # closed without an end time: bounded by the next start and maxTurnMinutes
    elif end >= start:
        out.append((start, end))
    return out


def row_minutes(row: dict, next_start, stamps: list, now_dt, cfg: dict) -> tuple:
    """(counted, dropped, estimated) minutes for one registry row."""
    import datetime as dt

    cap = dt.timedelta(minutes=cfg["maxTurnMinutes"])

    def close(start, end):
        if end is not None:
            return end
        bound = start + cap
        return min(bound, next_start) if next_start is not None and next_start > start else bound

    spans = [(s, close(s, e)) for s, e in turns(row, now_dt)]
    clock = sum((e - s).total_seconds() for s, e in spans) / 60.0
    if never_ran(row):
        return 0.0, clock, 0.0
    if isinstance(row.get("runMs"), (int, float)) and row["runMs"] >= 0:
        counted = float(row["runMs"]) / 60000.0
        return counted, max(0.0, clock - counted), 0.0
    counted = estimated = 0.0
    for start, end in spans:
        stop = end
        if next_start is not None and next_start > start:
            stop = min(stop, next_start)
        stop = min(stop, start + dt.timedelta(minutes=cfg["maxTurnMinutes"]))
        life = [m for m in stamps if start <= m <= stop]
        if life:
            stop = min(stop, max(life) + dt.timedelta(minutes=cfg["idleGraceMinutes"]))
        else:
            if str(row.get("state") or "") == "starting":
                continue  # never showed a sign of life: not launched yet, or never
            stop = min(stop, start + dt.timedelta(minutes=cfg["silentTurnMinutes"]))
            estimated += max(0.0, (stop - start).total_seconds() / 60.0)
        counted += max(0.0, (stop - start).total_seconds() / 60.0)
    return counted, max(0.0, clock - counted), estimated


def _step_of(row: dict, spawns: dict) -> str:
    step = str(row.get("step") or spawns.get(str(row.get("id") or "")) or "")
    if step == "replace":
        step = ""
    return step if step in STEPS else ("work" if not step else step)


def compute(data: dict, beam_path: Optional[Path] = None, now=None, doc: Optional[dict] = None) -> dict:
    """{ticket id: {"minutes", "dropped", "runs": {step: n}, "byStep": {step: minutes}, "byFix": {kind: minutes}}}."""
    import agents
    import beam as beam_mod

    if doc is None:
        doc = agents.registry(data, beam_path)
    now_dt, _ = beam_mod.coerce_now(now)
    cfg = _cfg(data)
    spawns = {}
    for spawn in agents._spawns(doc):
        if isinstance(spawn, dict) and spawn.get("id") and spawn.get("step"):
            spawns.setdefault(str(spawn["id"]), str(spawn["step"]))
    by_ticket: dict = {}
    for row in agents._rows(doc):
        if isinstance(row, dict) and row.get("role") == "shuttle" and row.get("ticket"):
            by_ticket.setdefault(str(row["ticket"]), []).append(row)
    tickets = data.get("tickets") or {}
    out = {}
    for tid, rows in by_ticket.items():
        ticket = tickets.get(tid) if isinstance(tickets.get(tid), dict) else {}
        stamps = stamps_for(ticket, beam_path)
        rows = sorted(rows, key=lambda r: str(r.get("started") or ""))
        total = dropped = guessed = 0.0
        runs: dict = {}
        by_step: dict = {}
        by_fix: dict = {}
        for index, row in enumerate(rows):
            nxt = None
            for later in rows[index + 1:]:
                nxt = _parse(later.get("started"))
                if nxt is not None:
                    break
            counted, lost, est = row_minutes(row, nxt, stamps, now_dt, cfg)
            total += counted
            dropped += lost
            guessed += est
            if counted <= 0:
                continue
            step = _step_of(row, spawns)
            runs[step] = runs.get(step, 0) + 1
            by_step[step] = by_step.get(step, 0.0) + counted
            if step == "fix":
                for kind in row.get("fixKinds") or ["unknown"]:
                    by_fix[kind] = by_fix.get(kind, 0.0) + counted
        out[tid] = {"minutes": total, "dropped": dropped, "estimated": guessed, "runs": runs,
                    "byStep": {k: round(v) for k, v in by_step.items()},
                    "byFix": {k: round(v) for k, v in by_fix.items()}}
    return out


def update(data: dict, beam_path: Optional[Path] = None, now=None, run: Optional[Callable] = None) -> None:
    """Every supervise pass: each ticket's working time and AI uses from the registry (no ratchet), and code counts."""
    try:
        import agents

        agents.close_orphans(data, beam_path, now)
    except Exception:
        pass
    try:
        work = compute(data, beam_path, now)
    except Exception:
        return
    for tid, row in work.items():
        ticket = (data.get("tickets") or {}).get(tid)
        if not isinstance(ticket, dict):
            continue
        ticket["minutes"] = int(round(row["minutes"]))
        ticket["ai"] = {"runs": row["runs"], "minutes": row["byStep"], "fixMinutes": row["byFix"],
                        "droppedMinutes": int(round(row["dropped"])), "estimatedMinutes": int(round(row["estimated"]))}
    try:
        code_pass(data, beam_path, run=run)
    except Exception:
        pass


# ---- code written ----------------------------------------------------------------------------------------------


def _git(root: Path, args: list, run: Optional[Callable] = None):
    if run is not None:
        return run(["git", *args], root)
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, timeout=60)


def diff_counts(root: Path, old: str, new: str, run: Optional[Callable] = None) -> Optional[dict]:
    """Lines, files and characters added outside .warp/ between two refs. None when git cannot tell."""
    numstat = _git(root, ["diff", "--numstat", old, new, "--", ".", ":(exclude).warp"], run)
    if getattr(numstat, "returncode", 1) != 0:
        return None
    added = deleted = files = 0
    for line in (numstat.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        if parts[0].isdigit():
            added += int(parts[0])
        if parts[1].isdigit():
            deleted += int(parts[1])
    patch = _git(root, ["diff", "-U0", "--no-color", old, new, "--", ".", ":(exclude).warp"], run)
    chars = 0
    if getattr(patch, "returncode", 1) == 0:
        for line in (patch.stdout or "").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                chars += len(line) - 1
    return {"added": added, "deleted": deleted, "files": files, "chars": chars,
            "tokens": int(math.ceil(chars / CHARS_PER_TOKEN)) if chars else int(added * 10)}


def code_for(ticket: dict, root: Path, base: str, run: Optional[Callable] = None) -> Optional[dict]:
    pr = ticket.get("pr") or {}
    merge = str(pr.get("mergeSha") or pr.get("mergeCommit") or "").strip()
    status = str(ticket.get("status") or "")
    if merge and status in {"merged", "done"}:
        got = diff_counts(root, "%s^1" % merge, merge, run)
        if got is not None:
            got.update(source="git", sha=merge[:12], final=True)
            return got
    branch = str(ticket.get("branch") or pr.get("branch") or "").strip()
    head = str(pr.get("headSha") or "").strip()
    if branch and head:
        merge_base = _git(root, ["merge-base", "origin/%s" % base, head], run)
        old = (merge_base.stdout or "").strip() if getattr(merge_base, "returncode", 1) == 0 else ""
        got = diff_counts(root, old, head, run) if old else None
        if got is not None:
            got.update(source="git", sha=head[:12], final=False)
            return got
    return None


def code_pass(data: dict, beam_path: Optional[Path], run: Optional[Callable] = None) -> int:
    """Count the code of tickets whose diff changed since it was last counted. At most codeBudget per pass."""
    if beam_path is None and run is None:
        return 0
    root = Path(data.get("_repoRoot") or (Path(beam_path).resolve().parents[3] if beam_path else "."))
    if beam_path is not None and not data.get("_repoRoot"):
        here = Path(beam_path).resolve().parent
        for up in [here, *here.parents]:
            if (up / ".git").exists():
                root = up
                break
    base = str((data.get("config") or {}).get("baseBranch") or "main")
    budget = int(_cfg(data)["codeBudget"])
    done = 0
    for ticket in (data.get("tickets") or {}).values():
        if done >= budget:
            break
        if not isinstance(ticket, dict) or not (ticket.get("pr") or {}).get("url"):
            continue
        pr = ticket.get("pr") or {}
        want = str(pr.get("mergeSha") or "")[:12] if ticket.get("status") in {"merged", "done"} else str(pr.get("headSha") or "")[:12]
        have = ticket.get("code") if isinstance(ticket.get("code"), dict) else {}
        if not want or have.get("sha") == want or have.get("missing") == want:
            continue
        got = code_for(ticket, root, base, run)
        done += 1
        if got is None:
            ticket["code"] = dict(have, missing=want)
            continue
        ticket["code"] = got
    return done


# ---- totals for the beam and the view --------------------------------------------------------------------------


def totals(data: dict) -> dict:
    tickets = [t for t in (data.get("tickets") or {}).values() if isinstance(t, dict)]
    runs: dict = {}
    minutes: dict = {}
    fix_minutes: dict = {}
    fix_counts: dict = {}
    code = {"added": 0, "deleted": 0, "files": 0, "tokens": 0, "tickets": 0}
    dropped = estimated = 0
    for t in tickets:
        ai = t.get("ai") if isinstance(t.get("ai"), dict) else {}
        for k, v in (ai.get("runs") or {}).items():
            runs[k] = runs.get(k, 0) + int(v or 0)
        for k, v in (ai.get("minutes") or {}).items():
            minutes[k] = minutes.get(k, 0) + int(v or 0)
        for k, v in (ai.get("fixMinutes") or {}).items():
            fix_minutes[k] = fix_minutes.get(k, 0) + int(v or 0)
        dropped += int(ai.get("droppedMinutes") or 0)
        estimated += int(ai.get("estimatedMinutes") or 0)
        for k, v in (t.get("fixCounts") or {}).items():
            fix_counts[k] = fix_counts.get(k, 0) + int(v or 0)
        c = t.get("code") if isinstance(t.get("code"), dict) else {}
        if c.get("source"):
            code["tickets"] += 1
            for k in ("added", "deleted", "files", "tokens"):
                code[k] += int(c.get(k) or 0)
    back = {k: sum(1 for t in tickets if int((t.get("fixCounts") or {}).get(k) or 0) > 0) for k in ("bug", "ci")}
    return {"aiRuns": runs, "aiMinutes": minutes, "fixMinutes": fix_minutes, "fixCounts": fix_counts,
            "ticketsBack": back, "droppedMinutes": dropped,
            "estimatedMinutes": estimated, "code": code}
