#!/usr/bin/env python3
"""Is each Shuttle making progress? A Shuttle with no new commit on its branch for `stallNoPushMinutes` (default 30;
0 is off) is flagged stalled, with one Slack alarm per step (1.7.9).

Before 1.7.9 a Shuttle was judged by its heartbeat only. One that kept writing heartbeats and never committed (looping
on a test, waiting on something) held its slot until `stallImplementingMinutes` (90) and was never named.

Each pass reads the tip of every working Shuttle's branch: the local branch (a worktree Shuttle commits into the
parent's own repository, so its commits show at once), else the branch on origin (one `git ls-remote` for all of them,
at most every 5 minutes; a Shuttle on its own VM pushes), else the pull request's head. The clock starts when the step
starts; a new tip starts it again.

  stall: <id> <step> Shuttle has no new commit for 34m (branch <branch> at <sha> since 12:01Z); flagged stalled
  herald: ALARM <id> …

Review and verify steps are not judged: they do not commit. The Shuttle prompt says to commit at least every 20
minutes and, while there is no pull request, to push each commit.

  python3 scripts/progress.py --beam .warp/beam.json      print each working Shuttle's last commit and its age

?, help, -h, and --help print this text.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

HELP = __doc__
DEFAULT_MINUTES = 30
REMOTE_EVERY = 300
JUDGED_STEPS = {"implement", "fix", "restart", "rebase", "repair", ""}


def limit_minutes(data: dict, beam_path: Optional[Path]) -> int:
    import beam as beam_mod

    try:
        return max(0, int(beam_mod.config_int(data, beam_path, "stallNoPushMinutes", DEFAULT_MINUTES)))
    except Exception:
        return DEFAULT_MINUTES


def _git(root: Path, *args, timeout: int = 20):
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def local_tips(root: Path) -> dict:
    """branch -> sha for every local branch."""
    done = _git(root, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    out = {}
    if done is not None and done.returncode == 0:
        for line in (done.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 2:
                out[parts[0]] = parts[1]
    return out


def remote_tips(root: Path) -> dict:
    """branch -> sha for every branch on origin (one ls-remote)."""
    done = _git(root, "ls-remote", "--heads", "origin", timeout=30)
    out = {}
    if done is not None and done.returncode == 0:
        for line in (done.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                out[parts[1][len("refs/heads/"):]] = parts[0]
    return out


def _working(ticket: dict, data: dict, now) -> bool:
    import orchestrator

    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    if str(shuttle.get("step") or "") not in JUDGED_STEPS:
        return False
    return bool(ticket.get("branch")) and orchestrator.shuttle_still_out(ticket, data, now=now)


def apply(data: dict, tips: Callable[[str], str], now_s: str, limit: int) -> list:
    """Track each working Shuttle's branch tip; flag the ones with no new commit for `limit` minutes. The caller writes
    the beam. `tips(branch)` returns the branch's tip or ''."""
    import beam as beam_mod

    if limit <= 0:
        return []
    now_dt = beam_mod.coerce_now(now_s)[0]
    lines: list = []
    for tid, ticket in sorted((data.get("tickets") or {}).items(), key=lambda kv: str(kv[0])):
        if not isinstance(ticket, dict):
            continue
        if not _working(ticket, data, now_s):
            ticket.pop("progress", None)
            continue
        shuttle = ticket.get("shuttle") or {}
        step_key = "%s@%s" % (shuttle.get("step") or "", shuttle.get("startedAt") or shuttle.get("markedAt") or "")
        tip = tips(str(ticket.get("branch") or "")) or str((ticket.get("pr") or {}).get("headSha") or "")
        record = ticket.get("progress") if isinstance(ticket.get("progress"), dict) else {}
        if record.get("step") != step_key:
            start = str(shuttle.get("startedAt") or shuttle.get("markedAt") or now_s)
            record = {"step": step_key, "tip": tip, "at": start}
        elif tip and tip != record.get("tip"):
            if record.get("flagged"):
                lines.append("progress: %s committed again (%s); no longer stalled" % (tid, tip[:12]))
            record = {"step": step_key, "tip": tip, "at": now_s}
        ticket["progress"] = record
        since = beam_mod.parse_ts(str(record.get("at") or ""))
        if since is None:
            continue
        quiet = int((now_dt - since).total_seconds() // 60)
        if quiet < limit:
            continue
        where = "%s at %s" % (ticket.get("branch"), str(record.get("tip") or "")[:12]) if record.get("tip") else \
            "%s, nothing committed yet" % ticket.get("branch")
        lines.append("stall: %s %s Shuttle has no new commit for %dm (branch %s since %sZ); flagged stalled" % (
            tid, shuttle.get("step") or "implement", quiet, where, str(record.get("at") or "")[11:19]))
        if not record.get("flagged"):
            record["flagged"] = now_s
            watch = ticket.setdefault("watch", {})
            if isinstance(watch, dict):
                watch["noCommitSince"] = record.get("at")
            lines.append("herald: ALARM %s: its %s Shuttle has made no new commit for %dm (stallNoPushMinutes %d). It is "
                         "flagged stalled; warp:retry %s restarts it." % (tid, shuttle.get("step") or "implement", quiet, limit, tid))
    return lines


def observe(beam_path: Path, now=None, local: Optional[Callable] = None, remote: Optional[Callable] = None) -> list:
    """Read the branch tips and apply. Never raises."""
    import beam as beam_mod

    path = Path(beam_path)
    try:
        data = beam_mod.load_json(path)
    except (OSError, ValueError) as exc:
        return ["progress: not read (%s)" % exc]
    if (data.get("runState") or "running") != "running" or data.get("paused"):
        return []
    limit = limit_minutes(data, path)
    if not limit:
        return []
    now_dt, now_s = beam_mod.coerce_now(now)
    if not any(isinstance(t, dict) and _working(t, data, now_s) for t in (data.get("tickets") or {}).values()):
        return []
    import multirepo
    import runs

    root = runs.repo_root(path)
    cache: dict = {}
    record = data.get("progressRead") if isinstance(data.get("progressRead"), dict) else {}
    last_remote = beam_mod.parse_ts(str(record.get("remoteAt") or ""))
    fresh_remote = last_remote is None or (now_dt - last_remote).total_seconds() >= REMOTE_EVERY
    if fresh_remote:
        record["remoteAt"] = now_s
    record["at"] = now_s
    data["progressRead"] = record
    saved_remote = record.get("remote") if isinstance(record.get("remote"), dict) else {}

    def tips_for(repo: Path) -> tuple:
        key = str(repo)
        if key not in cache:
            mine = (local or local_tips)(repo)
            if fresh_remote:
                theirs = {b: s for b, s in (remote or remote_tips)(repo).items() if b in by_branch}
                saved_remote[key] = theirs
            else:
                theirs = saved_remote.get(key) or {}
            cache[key] = (mine, theirs)
        return cache[key]

    by_branch: dict = {}
    for ticket in (data.get("tickets") or {}).values():
        if isinstance(ticket, dict) and ticket.get("branch"):
            try:
                by_branch[str(ticket["branch"])] = Path(multirepo.root_for(data, ticket, root))
            except Exception:
                by_branch[str(ticket["branch"])] = root

    def tip(branch: str) -> str:
        mine, theirs = tips_for(by_branch.get(branch, root))
        # The newer of the two is not knowable without a fetch; a local branch that moved is progress, so is a push.
        return "%s|%s" % (mine.get(branch, ""), theirs.get(branch, "")) if (mine.get(branch) or theirs.get(branch)) else ""

    lines = apply(data, tip, now_s, limit)
    record["remote"] = saved_remote
    data["progressRead"] = record
    beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
    return [line.replace("|", " / ") if line.startswith("stall:") else line for line in lines]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Each working Shuttle's last commit", epilog=HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--beam", default=".warp/beam.json")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    import runs as _runs

    _runs.use_live_beam(args)
    path = Path(args.beam)
    if not path.is_file():
        print("progress: no beam at %s" % path)
        return 2
    for line in observe(path):
        print(line)
    import beam as beam_mod

    data = beam_mod.load_json(path)
    now_dt = beam_mod.coerce_now(None)[0]
    shown = 0
    for tid, ticket in sorted((data.get("tickets") or {}).items()):
        record = ticket.get("progress") if isinstance(ticket, dict) else None
        if not isinstance(record, dict):
            continue
        since = beam_mod.parse_ts(str(record.get("at") or ""))
        age = int((now_dt - since).total_seconds() // 60) if since else -1
        print("progress: %s last commit %dm ago (%s)%s" % (tid, age, str(record.get("tip") or "none").replace("|", " / ")[:40],
                                                         " STALLED" if record.get("flagged") else ""))
        shown += 1
    if not shown:
        print("progress: no working Shuttle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
