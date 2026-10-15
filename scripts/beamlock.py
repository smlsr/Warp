#!/usr/bin/env python3
"""Concurrent writers of .warp/beam.json, without lost updates.

Many processes write the live beam: the parent's supervise pass, its launches, Slack commands, and every
Shuttle (`beam.py set`, `beam.py heartbeat`, `ticket_state.py append`, `bugbot.py record`). Each one loads
the whole file, changes it, and saves the whole file. Before 1.7.0 a save replaced the file outright, so a
pass that loaded the beam, then saved a minute later, threw away whatever a Shuttle saved in between: a
status change, a heartbeat, a recorded review.

Two locks fix it:

1. **The save merges** (`write`, called by `beam.atomic_write` for a live beam). Every save carries a revision
   `_rev` and a random `_revKey`; the versions saved are kept in this process and in `.warp/.beam-revs/`. A save
   takes `.warp/.beam.lock` (held for milliseconds), reads the file, and if another process saved since this
   save's version was read, merges three ways against that version: a field only one side changed takes that
   side; a field both changed keeps this save, except heartbeat times like `lastSeenAt` (the later one), lists
   both sides only appended to (both appends), and a ticket's status, phase, and alarm, decided as one group
   by rank, not save order (`_status_winner`): finished, then a lock-escape alarm, then any status over a
   liveness alarm, then the status further along. The caller's
   dict is then brought up to the saved version in place, so its next save is built on what is on disk.
   The pass prints `beam: merged <n> saves by other processes (<k> of their changes kept…)`.
2. **One run lock** (`run_lock`, `.warp/.listener.lock`) for the commands that decide what starts: a
   supervise pass, Slack commands, `alarm_repair.py next/paths`, and `checkout.py launch`. It is re-entrant
   in a process, so a pass that calls the repair or a Slack command that runs a sync does not wait on itself.
   Shuttles never take it: their small saves merge in and never wait on a pass.

  python3 scripts/beamlock.py status --beam .warp/beam.json     revision, kept versions, lock holders

?, help, -h, and --help print this text.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import gzip
import json
import os
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Optional

HELP = __doc__
REV = "_rev"
REV_KEY = "_revKey"  # unique per save: a beam loaded from main restarts the numbers, never the keys
KEEP = 128
REVS_DIR = ".beam-revs"
LOCK_NAME = ".beam.lock"
RUN_LOCK_NAME = ".listener.lock"
LATEST_WINS = {"lastSeenAt", "updatedAt", "heartbeatAt", "parentSeenAt", "checkpointAt"}
_MISSING = object()
STATUS_GROUP = ("status", "phase", "alarm")
FINISHED = ("merged", "done", "skipped")
LIVENESS_ALARMS = ("stuck", "worker-died", "stalled", "stale")
PROGRESS = ("queued", "blocked", "claimed", "recovering", "planning", "coding", "fix", "review", "bugbot_running",
            "awaiting_approval", "merging", "parked")
CACHE_SIZE = 32
_held: dict = {}
last_merge: dict = {}
_cache: "OrderedDict[str, dict]" = OrderedDict()  # versions this process saved, by _revKey


def is_live_beam(path: Path) -> bool:
    """The working beam (`.warp/beam.json` or a stateDir's), not a published copy under runs/."""
    path = Path(path)
    return path.name == "beam.json" and path.parent.name != "runs" and path.parent.parent.name != "runs"


@contextlib.contextmanager
def _flock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def run_lock(beam_path):
    """The run's decision lock, re-entrant inside one process."""
    key = str((Path(beam_path).parent / RUN_LOCK_NAME).resolve())
    if _held.get(key):
        _held[key] += 1
        try:
            yield
        finally:
            _held[key] -= 1
        return
    with _flock(Path(key)):
        _held[key] = 1
        try:
            yield
        finally:
            _held.pop(key, None)


def _snapshot_path(beam_path: Path, rev: int, key: str) -> Path:
    return beam_path.parent / REVS_DIR / ("%s.%d.%s.json.gz" % (beam_path.stem, rev, key))


def _version(beam_path: Path, rev, key) -> Optional[dict]:
    """The version saved as `key`: from this process's cache, else the kept copy. None when neither has it."""
    if not isinstance(key, str) or not key.isalnum():
        return None
    if key in _cache:
        return json.loads(json.dumps(_cache[key]))
    if not isinstance(rev, int):
        return None
    try:
        with gzip.open(_snapshot_path(beam_path, rev, key), "rt") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) and doc.get(REV_KEY) == key else None
    except Exception:  # missing, truncated by a killed writer, or not JSON: no base
        return None


def _remember(beam_path: Path, doc: dict, text: str) -> None:
    _cache[doc[REV_KEY]] = json.loads(text)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    folder = beam_path.parent / REVS_DIR
    try:
        folder.mkdir(parents=True, exist_ok=True)
        final = _snapshot_path(beam_path, doc[REV], doc[REV_KEY])
        fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=final.name + ".", suffix=".tmp")
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as gz:
            gz.write(text.encode())
        os.replace(tmp, final)
        kept = sorted(folder.glob("%s.*.json.gz" % beam_path.stem), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in kept[KEEP:]:
            old.unlink()
        for stale in folder.glob("*.tmp"):
            if stale.stat().st_mtime < os.path.getmtime(final) - 300:
                stale.unlink()
    except OSError:
        pass


def merge3(base, ours, theirs, path: tuple = (), stats: Optional[dict] = None):
    """Three-way merge of JSON values. `ours` is this save, `theirs` the file, `base` what this save was built on.

    One-sided changes are taken. Both-sided: this save, except heartbeat times (the later one), lists both
    sides only appended to (both appends), and a ticket's status, phase, and alarm, decided as one group by
    `_status_winner`; the next pass decides on that ticket again.
    """
    stats = stats if stats is not None else {}
    if ours == theirs:
        return ours
    if base is not _MISSING and ours == base:
        stats["taken"] = stats.get("taken", 0) + 1
        return theirs
    if base is not _MISSING and theirs == base:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        b = base if isinstance(base, dict) else None
        out = {}
        group = set()
        if len(path) == 2 and path[0] == "tickets" and b is not None:
            def val(doc, k):
                return doc.get(k, _MISSING) if isinstance(doc, dict) else _MISSING
            clash = any(val(ours, k) != val(b, k) and val(theirs, k) != val(b, k) and val(ours, k) != val(theirs, k)
                        for k in STATUS_GROUP)
            if clash:
                winner = _status_winner(ours, theirs)
                group = {k: (val(winner, k)) for k in STATUS_GROUP}
                stats.setdefault("statusTickets", []).append(path[1])
        for k in list(ours.keys()) + [k for k in theirs.keys() if k not in ours]:
            bv = b.get(k, _MISSING) if b is not None else _MISSING
            ov = ours.get(k, _MISSING)
            tv = theirs.get(k, _MISSING)
            if k in group:
                if group[k] is not _MISSING:
                    out[k] = group[k]
                continue
            if ov is _MISSING:
                if bv is not _MISSING and tv == bv:
                    continue  # deleted by this save
                if bv is _MISSING:
                    out[k] = tv  # added by the other process
                    stats["taken"] = stats.get("taken", 0) + 1
                    continue
                stats["conflicts"] = stats.get("conflicts", 0) + 1
                continue
            if tv is _MISSING:
                if bv is not _MISSING and ov == bv:
                    continue  # deleted by the other process
                out[k] = ov
                continue
            out[k] = merge3(bv, ov, tv, path + (k,), stats)
        return out
    if isinstance(ours, list) and isinstance(theirs, list):
        b = base if isinstance(base, list) else None
        if b is not None and ours[:len(b)] == b and theirs[:len(b)] == b:
            extra = theirs[len(b):]
            stats["taken"] = stats.get("taken", 0) + len(extra)
            return ours + [x for x in extra if x not in ours[len(b):]]
    key = path[-1] if path else ""
    if key in LATEST_WINS and isinstance(ours, str) and isinstance(theirs, str):
        return max(ours, theirs)
    stats["conflicts"] = stats.get("conflicts", 0) + 1
    return ours


def _status_winner(ours: dict, theirs: dict) -> dict:
    """Which side's status, phase, and alarm a ticket keeps when both changed them. Not save order:

    a finished status (merged, done, skipped); then a lock-escape alarm (a safety stop); then any status over a
    liveness alarm (stuck, worker-died, stalled), which a pass decides on older heartbeats a Shuttle's report
    disproves; then the status further along. A tie keeps the file's (the other process's) value.
    """
    def rank(row: dict) -> tuple:
        status = str(row.get("status") or "")
        alarm = str(row.get("alarm") or "")
        if status in FINISHED:
            return (4, 0)
        if status == "alarm" and alarm == "lock-escape":
            return (3, 0)
        if status == "alarm" and (alarm in LIVENESS_ALARMS or not alarm):
            return (0, 0)
        if status == "alarm":
            return (2, 0)
        return (1, PROGRESS.index(status) if status in PROGRESS else 0)
    return ours if rank(ours) > rank(theirs) else theirs


def sync_into(target, source):
    """Make `target` equal `source` in place, keeping its nested dicts and lists the same objects."""
    if isinstance(target, dict) and isinstance(source, dict):
        for k in [k for k in target if k not in source]:
            del target[k]
        for k, v in source.items():
            cur = target.get(k, _MISSING)
            if isinstance(cur, dict) and isinstance(v, dict) or isinstance(cur, list) and isinstance(v, list):
                sync_into(cur, v)
            else:
                target[k] = v
        return target
    if isinstance(target, list) and isinstance(source, list):
        if len(target) == len(source) and all(isinstance(a, (dict, list)) for a in target):
            for a, s in zip(target, source):
                if type(a) is type(s):
                    sync_into(a, s)
                else:
                    target[:] = source
                    return target
        else:
            target[:] = source
        return target
    return source


def _stamp(doc: dict, *revs) -> None:
    doc[REV] = max([r for r in revs if isinstance(r, int)] or [0]) + 1
    doc[REV_KEY] = os.urandom(6).hex()


def note_read(path: Path, doc) -> None:
    """Keep the version a process just read (beam.load_json), so its first save finds its base here."""
    if isinstance(doc, dict) and isinstance(doc.get(REV_KEY), str) and is_live_beam(Path(path)):
        if doc[REV_KEY] not in _cache:
            _cache[doc[REV_KEY]] = json.loads(json.dumps(doc))
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)


def _read(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def write(path: Path, text: str, replace, data: Optional[dict] = None) -> None:
    """Save the live beam under the beam lock, merging what another process saved since this one read it.

    `data` is the caller's dict: it is brought up to the saved version in place, so its next save is built
    on what is on disk, not on what it first read.
    """
    path = Path(path)
    try:
        ours = json.loads(text)
    except ValueError:
        ours = None
    if not isinstance(ours, dict) or not is_live_beam(path):
        replace(path, text)
        return
    key = str(path.resolve())
    with _flock(path.parent / LOCK_NAME):
        theirs = _read(path)
        result = ours
        built_on = ours.get(REV_KEY)
        if theirs is not None and built_on and theirs.get(REV_KEY) and theirs.get(REV_KEY) != built_on:
            base = _version(path, ours.get(REV), built_on)
            strip = lambda doc: {k: v for k, v in doc.items() if k not in (REV, REV_KEY)} if isinstance(doc, dict) else doc
            stats: dict = {}
            result = merge3(strip(base) if base is not None else _MISSING, strip(ours), strip(theirs), (), stats)
            total = last_merge.setdefault(key, {"taken": 0, "conflicts": 0, "saves": 0, "statusTickets": []})
            total["taken"] += stats.get("taken", 0)
            total["conflicts"] += stats.get("conflicts", 0)
            total["saves"] += 1
            total["statusTickets"] += [t for t in stats.get("statusTickets", []) if t not in total["statusTickets"]]
        _stamp(result, ours.get(REV), theirs.get(REV) if theirs else None)
        out = json.dumps(result, indent=2) + "\n"
        replace(path, out)
        _remember(path, result, out)
    if isinstance(data, dict):
        sync_into(data, result)


def update(path: Path, change, replace) -> None:
    """Read, change, and save the live beam in one step under the beam lock (no merge needed)."""
    path = Path(path)
    with _flock(path.parent / LOCK_NAME):
        doc = _read(path)
        if doc is None:
            return
        new = change(doc)
        doc = new if isinstance(new, dict) else doc
        _stamp(doc, doc.get(REV))
        out = json.dumps(doc, indent=2) + "\n"
        replace(path, out)
        _remember(path, doc, out)


def replace_whole(path: Path, doc: dict, replace) -> None:
    """Put a whole beam in place on purpose (one loaded from main): no merge, a fresh revision, kept as a base."""
    path = Path(path)
    with _flock(path.parent / LOCK_NAME):
        disk = _read(path)
        doc = dict(doc)
        _stamp(doc, doc.get(REV), disk.get(REV) if disk else None)
        out = json.dumps(doc, indent=2) + "\n"
        replace(path, out)
        _remember(path, doc, out)


def merged_line(path: Path, reset: bool = True) -> str:
    """What this process merged into its saves of the beam; empty when nothing was."""
    key = str(Path(path).resolve())
    info = last_merge.pop(key, None) if reset else last_merge.get(key)
    if not info or not info.get("saves"):
        return ""
    line = "beam: merged %d saves by other processes (%d of their changes kept" % (info["saves"], info["taken"])
    if info["statusTickets"]:
        line += "; status changed on both sides for %s, ranked (finished, lock-escape, live over a liveness alarm, further along)" % ", ".join(info["statusTickets"])
    if info["conflicts"]:
        line += "; %d fields both changed, this pass's value kept" % info["conflicts"]
    return line + ")"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Beam write locks", epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    st.add_argument("--beam", default=".warp/beam.json")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    path = Path(args.beam)
    rev = None
    try:
        rev = json.loads(path.read_text()).get(REV)
    except (OSError, ValueError):
        pass
    kept = sorted((path.parent / REVS_DIR).glob("%s.*.json.gz" % path.stem)) if (path.parent / REVS_DIR).is_dir() else []
    print("beam: rev %s, %d versions kept in %s" % (rev if rev is not None else "none (not saved since 1.7.0)", len(kept), path.parent / REVS_DIR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
