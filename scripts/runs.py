#!/usr/bin/env python3
"""Named runs: one repo, several Warp runs.

A run is one beam, one parent, and one `warp:<instance>` tag. Each checkout
(or cloud agent) runs one run, and `/warp-init <name>` names it. The name is
`run:` in the local `.warp/config.yaml`.

Local files stay where they always were: `.warp/beam.json`, `.warp/tickets/`,
and the rest. What changes is the copy that goes into git. A named run
publishes to its own folder, `.warp/runs/<name>/`, on the base branch and on
its ticket branches, and it reads other runs only from there. Two runs in
one repo never write the same path.

The folder holds the beam, the journal, the agent registry, status and
board files, `scan.json`, `stats.json`, a copy of the run's config with the
machine-only keys and secrets removed, and every ticket's state and log.
`/warp-init <name>` on a fresh checkout reads that folder back, so a run
whose agent is gone picks up where it left off.

  python3 scripts/runs.py ?
  python3 scripts/runs.py list
  python3 scripts/runs.py show admin
  python3 scripts/runs.py delete admin
  python3 scripts/runs.py delete admin --apply
  python3 scripts/runs.py delete admin --apply --branches
  python3 scripts/runs.py name

delete is a dry run until --apply. It removes .warp/runs/<name>/ from the base
branch in one commit and pushes it. It refuses a run that is running, or that
another run waits on, unless --force. Branches stay unless --branches.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

HELP = __doc__

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
GATE_LIKE_RE = re.compile(r"^g\d+$")
RESERVED = {"runs", "tickets", "all", "new", "none", "worktrees", "local", "previous", "help"}
BRANCH_RE = re.compile(r"^warp/([a-z0-9][a-z0-9-]*)/.+")

# Keys that describe this machine or this agent, not the run. They stay in the
# local config and are not copied into the repo or taken back from it.
MACHINE_KEYS = (
    "run",
    "runner",
    "launch",
    "worktreeRoot",
    "stateDir",
    "subagentVm",
    "memoryCheck",
    "cloudSnapshot",
)
# Files a run publishes, beside tickets/.
STATE_FILES = (
    "beam.json",
    "journal.jsonl",
    "STATUS.md",
    "status.json",
    "BOARD.md",
    "board.html",
    "agents.json",
)
RUN_FILES = STATE_FILES + ("scan.json", "stats.json", "config.yaml")
# Live files a named run keeps beside the repo's own files. They are written to
# .git/info/exclude so `git status` does not list a second copy of what the run
# folder already holds.
LIVE_EXCLUDE = (
    ".warp/beam.json",
    ".warp/journal.jsonl",
    ".warp/STATUS.md",
    ".warp/status.json",
    ".warp/BOARD.md",
    ".warp/board.html",
    ".warp/agents.json",
    ".warp/scan.json",
    ".warp/stats.json",
    ".warp/checkpoint.json",
    ".warp/tickets/",
)
LIVE_MINUTES = 30
DEFAULT_CHECKPOINT_MINUTES = 10


# --- names and paths -------------------------------------------------------


def valid_name(name: str) -> tuple:
    """(ok, reason). A run name is lowercase letters, digits, and dashes."""
    text = str(name or "")
    if not text:
        return False, "a run name is required"
    if GATE_LIKE_RE.match(text.lower()):
        return False, "run name %r looks like a gate. Pick a name such as core, admin, or ui" % text
    if not NAME_RE.match(text):
        return False, "run name %r must be 1 to 32 lowercase letters, digits, or dashes, starting with a letter or digit" % text
    if GATE_LIKE_RE.match(text):
        return False, "run name %r looks like a gate. Pick a name such as core, admin, or ui" % text
    if text in RESERVED:
        return False, "run name %r is reserved" % text
    return True, ""


def folder_rel(name: str) -> str:
    """Repo-relative folder a named run publishes to."""
    return ".warp/runs/%s" % name


def git_rel(name: str, rel: str) -> str:
    """Map a local `.warp/...` path to where the run publishes it.

    No run name is the unnamed run: the path is unchanged.
    """
    if not name or not rel.startswith(".warp/"):
        return rel
    return "%s/%s" % (folder_rel(name), rel[len(".warp/"):])


def repo_root(beam_path) -> Path:
    path = Path(beam_path)
    return path.parent.parent if path.parent.name == ".warp" else path.parent


def config_path(root) -> Path:
    return Path(root) / ".warp" / "config.yaml"


# --- config text -----------------------------------------------------------


def read_key(text: str, key: str) -> Optional[str]:
    """The value of an active top-level `key: value` line, or None."""
    match = re.search(r"^%s:[ \t]*(.*?)\s*$" % re.escape(key), text or "", re.M)
    if not match:
        return None
    value = match.group(1)
    if value[:1] in {'"', "'"}:
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value.strip("\"'")
    return value.split(" #", 1)[0].strip()


def set_key(text: str, key: str, value: str, quote: bool = False) -> str:
    """Replace the active `key:` line, or append one. Comments are kept."""
    rendered = '"%s"' % value if quote else value
    line = "%s: %s" % (key, rendered)
    pattern = re.compile(r"^%s:.*$" % re.escape(key), re.M)
    if pattern.search(text or ""):
        return pattern.sub(lambda _m: line, text, count=1)
    body = text or ""
    if body and not body.endswith("\n"):
        body += "\n"
    return body + line + "\n"


def read_run(root) -> str:
    """`run:` in this checkout's config, or empty for the unnamed run."""
    path = config_path(root)
    try:
        text = path.read_text()
    except OSError:
        return ""
    value = (read_key(text, "run") or "").strip().lower()
    return value if valid_name(value)[0] else ""


def write_run(root, name: str) -> None:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.is_file() else ""
    path.write_text(set_key(text, "run", name))


def read_scan_folder(root) -> str:
    path = config_path(root)
    try:
        return (read_key(path.read_text(), "scanFolder") or "").strip()
    except OSError:
        return ""


def write_scan_folder(root, folder: str) -> None:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.is_file() else ""
    path.write_text(set_key(text, "scanFolder", folder, quote=True))


def _git(root, *args: str, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, input=input_text)


def current(root, data: Optional[dict] = None, branch: Optional[str] = None, explicit: str = "") -> str:
    """The run this process belongs to, or empty for the unnamed run.

    Order: an explicit name, `WARP_RUN`, this checkout's config, the beam's
    own `run`, then a ticket branch named `warp/<run>/<id>`. The branch is how
    a Shuttle on its own VM, which has no local config, finds its run.
    """
    for candidate in (explicit, os.environ.get("WARP_RUN", ""), read_run(root)):
        name = str(candidate or "").strip().lower()
        if name and valid_name(name)[0]:
            return name
    if isinstance(data, dict):
        name = str(data.get("run") or (data.get("config") or {}).get("run") or "").strip().lower()
        if name and valid_name(name)[0]:
            return name
    if branch is None:
        shown = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
        branch = shown.stdout.strip() if shown.returncode == 0 else ""
    match = BRANCH_RE.match(branch or "")
    if match and valid_name(match.group(1))[0]:
        return match.group(1)
    return ""


def for_beam(beam_path, data: Optional[dict] = None) -> str:
    return current(repo_root(beam_path), data)


def branch_prefix(config: Optional[dict]) -> str:
    """`warp/<run>/` for a named run, `warp/` otherwise."""
    name = str((config or {}).get("run") or "").strip().lower()
    if name and valid_name(name)[0]:
        return "warp/%s/" % name
    return "warp/"


# --- the published copy of the config -------------------------------------


def mirror_config(text: str) -> str:
    """The run's config as it is published: no machine keys, no secrets."""
    import state_commit

    kept = []
    for line in (text or "").splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z0-9_]*):", line)
        if match:
            key = match.group(1)
            if key in MACHINE_KEYS or state_commit.secret_key(key):
                continue
        kept.append(state_commit.redact_text(line))
    return "\n".join(kept) + "\n"


def overlay_config(local: str, mirror: str) -> tuple:
    """Put the published config's values into the local config text.

    Returns (text, changed keys, skipped keys). Machine keys keep this
    machine's value. A key whose value spans lines is skipped and named.
    """
    lines = (mirror or "").splitlines()
    text = local or ""
    changed, skipped = [], []
    for index, line in enumerate(lines):
        match = re.match(r"^([A-Za-z][A-Za-z0-9_]*):(.*)$", line)
        if not match:
            continue
        key = match.group(1)
        if key in MACHINE_KEYS:
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if not match.group(2).strip() or following[:1] in {" ", "\t", "-"}:
            if following[:1] in {" ", "\t", "-"}:
                skipped.append(key)
                continue
        pattern = re.compile(r"^%s:.*$" % re.escape(key), re.M)
        before = text
        if pattern.search(text):
            text = pattern.sub(lambda _m, _l=line: _l, text, count=1)
        else:
            text = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
        if text != before:
            changed.append(key)
    return text, changed, skipped


# --- stats -----------------------------------------------------------------


def build_stats(beam: dict, run: str = "", now: Optional[str] = None) -> dict:
    """Per-ticket and total numbers for stats.json. Numbers only."""
    tickets = beam.get("tickets") if isinstance(beam.get("tickets"), dict) else {}
    totals = {"tokens": 0, "minutes": 0, "attempts": 0, "recoveries": 0, "cost": 0.0, "tokensIn": 0, "tokensOut": 0, "tokensCached": 0}
    by_status: dict = {}
    per: dict = {}
    for tid, row in tickets.items():
        if not isinstance(row, dict):
            continue
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        pr = row.get("pr") if isinstance(row.get("pr"), dict) else {}
        status = row.get("status") or ""
        by_status[status] = by_status.get(status, 0) + 1
        entry = {
            "status": status,
            "size": row.get("size"),
            "tokens": int(row.get("tokens") or 0),
            "minutes": int(row.get("minutes") or 0),
            "attempts": int(row.get("attempts") or 0),
            "recoveries": int(row.get("recoveries") or 0),
            "cost": float(usage.get("cost") or 0),
            "tokensIn": int(usage.get("tokensIn") or 0),
            "tokensOut": int(usage.get("tokensOut") or 0),
            "tokensCached": int(usage.get("tokensCached") or 0),
            "claimedAt": row.get("claimedAt"),
            "openedAt": pr.get("openedAt"),
            "mergedAt": pr.get("mergedAt"),
        }
        for key in totals:
            totals[key] += entry[key]
        per[tid] = entry
    totals["cost"] = round(totals["cost"], 4)
    return {"run": run or None, "generatedAt": now, "totals": totals, "byStatus": by_status, "tickets": per}


# --- reading a run from git -----------------------------------------------


def base_name(root, config: Optional[dict] = None) -> str:
    """The base branch: baseBranch in the config, else origin's default, else main or master, else this branch."""
    import state_commit

    configured = str((config or {}).get("baseBranch") or "").strip()
    if not configured:
        try:
            configured = (read_key(config_path(root).read_text(), "baseBranch") or "").strip()
        except OSError:
            configured = ""
    if configured:
        return configured
    shown = _git(root, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    if shown.returncode == 0 and shown.stdout.strip().startswith("origin/"):
        return shown.stdout.strip()[len("origin/"):]
    for name in ("main", "master"):
        for ref in ("refs/remotes/origin/%s" % name, "refs/heads/%s" % name):
            if _git(root, "rev-parse", "--verify", "-q", ref).returncode == 0:
                return name
    return state_commit._base_name(Path(root), {})


def fetch_ref(root, fetch: bool = True) -> str:
    """origin/<base> after a fetch, else the local base branch."""
    import state_commit

    base = base_name(root)
    if fetch and "origin" in state_commit._remote_names(Path(root)):
        done = _git(root, "fetch", "-q", "origin", base)
        if done.returncode == 0 and _git(root, "rev-parse", "--verify", "-q", "refs/remotes/origin/%s" % base).returncode == 0:
            return "origin/%s" % base
    return base


def show(root, ref: str, name: str, rel: str) -> Optional[str]:
    """A published file of run `name` at `ref`. `rel` is the local `.warp/...` path."""
    shown = _git(root, "show", "%s:%s" % (ref, git_rel(name, rel)))
    return shown.stdout if shown.returncode == 0 else None


def show_json(root, ref: str, name: str, rel: str) -> Optional[dict]:
    text = show(root, ref, name, rel)
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def list_runs(root, ref: str) -> list:
    """Run names that have a published beam at `ref`."""
    listed = _git(root, "ls-tree", "--name-only", ref, ".warp/runs/")
    if listed.returncode != 0:
        return []
    names = []
    for line in listed.stdout.splitlines():
        name = line.strip().rsplit("/", 1)[-1]
        if not valid_name(name)[0]:
            continue
        if _git(root, "cat-file", "-e", "%s:%s" % (ref, git_rel(name, ".warp/beam.json"))).returncode == 0:
            names.append(name)
    return sorted(names)


def known_names(root) -> set:
    """Run names published on the base branch, from refs already fetched. No network."""
    base = base_name(root)
    names: set = set()
    for ref in ("origin/%s" % base, base):
        if _git(root, "rev-parse", "--verify", "-q", ref).returncode == 0:
            names |= set(list_runs(root, ref))
    return names


def owners(root, run: str, ref: str) -> dict:
    """Ticket ids that another run has published, mapped to that run."""
    found: dict = {}
    for other in list_runs(root, ref):
        if other == run:
            continue
        beam = show_json(root, ref, other, ".warp/beam.json") or {}
        for tid in (beam.get("tickets") or {}):
            found.setdefault(str(tid), other)
    return found


def _parse_time(stamp) -> Optional[datetime]:
    if not stamp:
        return None
    text = str(stamp).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_minutes(stamp, now: Optional[datetime] = None) -> Optional[int]:
    then = _parse_time(stamp)
    if then is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - then).total_seconds() // 60))


def _ago(minutes: Optional[int]) -> str:
    if minutes is None:
        return "never"
    if minutes < 2:
        return "just now"
    if minutes < 120:
        return "%dm ago" % minutes
    if minutes < 60 * 48:
        return "%dh ago" % (minutes // 60)
    return "%dd ago" % (minutes // (60 * 24))


_OPEN_OFF = {"queued", "blocked", "merged", "done", "parked", "skipped"}


def summary(root, ref: str, name: str, now: Optional[datetime] = None) -> Optional[dict]:
    """What a published run holds, or None when `name` has no run at `ref`."""
    beam = show_json(root, ref, name, ".warp/beam.json")
    if beam is None:
        return None
    scan = show_json(root, ref, name, ".warp/scan.json") or {}
    cfg_text = show(root, ref, name, ".warp/config.yaml") or ""
    tickets = beam.get("tickets") if isinstance(beam.get("tickets"), dict) else {}
    counts = {"merged": 0, "open": 0, "queued": 0, "parked": 0}
    for row in tickets.values():
        status = (row or {}).get("status") if isinstance(row, dict) else None
        if status in {"merged", "done"}:
            counts["merged"] += 1
        elif status in {"queued", "blocked"}:
            counts["queued"] += 1
        elif status in {"parked", "skipped"}:
            counts["parked"] += 1
        elif status:
            counts["open"] += 1
    saved = _git(root, "log", "-1", "--format=%aI\t%an", ref, "--", folder_rel(name))
    saved_at, saved_by = "", ""
    if saved.returncode == 0 and saved.stdout.strip():
        saved_at, _, saved_by = saved.stdout.strip().partition("\t")
    folder = read_key(cfg_text, "scanFolder")
    if folder is None:
        folder = str(scan.get("folder") or "")
    if folder == ".":
        folder = ""
    seen = beam.get("checkpointAt") or saved_at
    minutes = age_minutes(seen, now)
    state = beam.get("runState") or ("paused" if beam.get("paused") else "stopped")
    return {
        "name": name,
        "ref": ref,
        "runState": state,
        "folder": folder,
        "scannedAt": scan.get("scannedAt"),
        "scanned": bool(scan) or bool(tickets),
        "tickets": len(tickets),
        "gates": len(beam.get("gates") or []),
        "counts": counts,
        "savedAt": saved_at,
        "savedBy": saved_by,
        "seenMinutes": minutes,
        "live": state == "running" and minutes is not None and minutes <= LIVE_MINUTES,
        "hasConfig": bool(cfg_text.strip()),
        "owner": beam.get("owner") if isinstance(beam.get("owner"), dict) else {},
        "startAfter": parse_start_after(read_key(cfg_text, "startAfter") or ""),
    }


def notice_lines(info: dict) -> list:
    """What `/warp-init <name>` prints when the run already exists."""
    name = info["name"]
    ref = info["ref"]
    lines = ["run: %s already exists on %s. You are taking up a previous run." % (name, ref)]
    if info["hasConfig"]:
        who = " by %s" % info["savedBy"] if info["savedBy"] else ""
        lines.append("config: %s/config.yaml, last saved %s%s" % (folder_rel(name), _ago(age_minutes(info["savedAt"])), who))
    else:
        lines.append("config: none saved. This checkout's config is used.")
    lines.append("folder: %s" % (info["folder"] or "whole repo"))
    if info["scanned"]:
        scanned = (info["scannedAt"] or "")[:10] or "date unknown"
        lines.append("scan: %s, %d tickets, %d gates" % (scanned, info["tickets"], info["gates"]))
    else:
        lines.append("scan: none yet")
    lines.append("state: %s, parent last seen %s" % (info["runState"], _ago(info["seenMinutes"])))
    if info.get("owner"):
        lines.append("owner: %s" % owner_text(info["owner"]))
    if info.get("startAfter"):
        lines.append("startAfter: %s" % ", ".join("%s:%s" % pair for pair in info["startAfter"]))
    counts = info["counts"]
    lines.append("progress: %d merged, %d open, %d queued" % (counts["merged"], counts["open"], counts["queued"]))
    if info["live"]:
        lines.append(
            "warn: the last parent saved state %s and the run is marked running. Another agent may still be running it. "
            "Starting from another machine needs /warp-start --takeover." % _ago(info["seenMinutes"])
        )
    lines.append("next: /warp-start" if info["scanned"] else "next: /warp-scan [folder]")
    return lines


# --- init, restore, adopt --------------------------------------------------


def ensure_excluded(root) -> bool:
    """Keep the live copies out of `git status` in a checkout that runs a named run."""
    shown = _git(root, "rev-parse", "--git-path", "info/exclude")
    if shown.returncode != 0 or not shown.stdout.strip():
        return False
    path = Path(shown.stdout.strip())
    if not path.is_absolute():
        path = Path(root) / path
    try:
        existing = path.read_text() if path.is_file() else ""
        missing = [line for line in LIVE_EXCLUDE if line not in existing.splitlines()]
        if not missing:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        head = "" if (not existing or existing.endswith("\n")) else "\n"
        path.write_text(existing + head + "# Warp: live copies of a named run's files; the run folder holds the published copy\n" + "\n".join(missing) + "\n")
    except OSError:
        return False
    return True


def local_has_run(root) -> int:
    """How many tickets the live beam in this checkout holds."""
    path = Path(root) / ".warp" / "beam.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    tickets = data.get("tickets") if isinstance(data, dict) else None
    return len(tickets) if isinstance(tickets, dict) else 0


def _ticket_files(root, ref: str, name: str) -> list:
    listed = _git(root, "ls-tree", "-r", "--name-only", ref, "%s/tickets" % folder_rel(name))
    if listed.returncode != 0:
        return []
    return [line.strip() for line in listed.stdout.splitlines() if line.strip()]


def restore_files(root, ref: str, name: str) -> list:
    """Write a published run's files into this checkout's live `.warp/`."""
    root = Path(root)
    warp = root / ".warp"
    warp.mkdir(parents=True, exist_ok=True)
    wrote = []
    for fname in STATE_FILES + ("scan.json", "stats.json"):
        text = show(root, ref, name, ".warp/%s" % fname)
        if text is None:
            continue
        if fname == "scan.json":
            try:
                found = json.loads(text)
                if isinstance(found, dict):
                    found["root"] = str(root)
                    text = json.dumps(found, indent=2) + "\n"
            except json.JSONDecodeError:
                pass
        (warp / fname).write_text(text)
        wrote.append(fname)
    prefix = folder_rel(name) + "/tickets/"
    count = 0
    for rel in _ticket_files(root, ref, name):
        text = _git(root, "show", "%s:%s" % (ref, rel))
        if text.returncode != 0:
            continue
        target = warp / "tickets" / rel[len(prefix):]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.stdout)
        count += 1
    if count:
        wrote.append("tickets/ (%d files)" % count)
    return wrote


def _move_aside(root, old: str) -> str:
    """Keep the files of the run this checkout leaves, instead of deleting them."""
    warp = Path(root) / ".warp"
    dest = warp / ("previous-%s" % (old or "unnamed"))
    n = 1
    while dest.exists():
        n += 1
        dest = warp / ("previous-%s-%d" % (old or "unnamed", n))
    dest.mkdir(parents=True)
    for fname in STATE_FILES + ("scan.json", "stats.json", "checkpoint.json"):
        if (warp / fname).is_file():
            shutil.move(str(warp / fname), str(dest / fname))
    if (warp / "tickets").is_dir():
        shutil.move(str(warp / "tickets"), str(dest / "tickets"))
    return str(dest.relative_to(root))


def take_up(root, name: str, switch: bool = False, adopt: bool = False, dry: bool = False) -> tuple:
    """`/warp-init <name>`. Returns (exit code, lines).

    A name nobody has used is a new run. A name that is published is taken
    up: this checkout gets the run's config and state back, and the notice
    says what it found. One checkout runs one run.
    """
    root = Path(root)
    ok, why = valid_name(name)
    if not ok:
        return 2, ["refuse: %s" % why]
    local = read_run(root)
    live = local_has_run(root)
    if local and local != name and not switch:
        return 2, [
            "refuse: this checkout runs %s. One checkout runs one run." % local,
            "Use another checkout or agent for %s, or /warp-init %s --switch to move this checkout over. "
            "The files here are kept in .warp/previous-%s/." % (name, name, local),
        ]
    if not local and live and not adopt and not switch:
        return 2, [
            "refuse: this checkout has an unnamed run with %d tickets." % live,
            "/warp-init %s --adopt names it %s and keeps its state. For a different run use another checkout, "
            "or add --switch to set the unnamed run aside." % (name, name),
        ]
    ref = fetch_ref(root)
    info = summary(root, ref, name)
    lines: list = []
    if info is None:
        lines.append("run: %s is new." % name)
        if adopt and live and not local:
            lines.append("adopt: this checkout's unnamed run (%d tickets) is now %s. Its state is kept and published to %s/." % (live, name, folder_rel(name)))
        lines.append("next: /warp-start" if adopt and live else "next: /warp-scan [folder]")
        if dry:
            return 0, lines + ["dry run: nothing written"]
        if switch and (local != name or (not local and live)):
            lines.append("moved: the previous files are in %s/" % _move_aside(root, local))
            live = 0
        write_run(root, name)
        ensure_excluded(root)
        if adopt and live:
            _stamp_beam(root, name)
        return 0, lines
    if adopt:
        return 2, ["refuse: run %s already exists on %s. --adopt would overwrite it. Pick another name." % (name, info["ref"])]
    lines.extend(notice_lines(info))
    if dry:
        return 0, lines + ["dry run: nothing written"]
    keep = local == name and live > 0
    if switch and (local != name or (not local and live)):
        moved = _move_aside(root, local)
        lines.append("moved: the previous files are in %s/" % moved)
        keep = False
    write_run(root, name)
    ensure_excluded(root)
    if keep:
        lines.append("restore: this checkout already has %s's live state. It was kept." % name)
        return 0, lines
    wrote = restore_files(root, info["ref"], name)
    cfg = config_path(root)
    mirror = show(root, info["ref"], name, ".warp/config.yaml")
    if mirror:
        text, changed, skipped = overlay_config(cfg.read_text() if cfg.is_file() else "", mirror)
        cfg.write_text(text)
        lines.append("config: restored %d keys from the repo; runner, launch, worktreeRoot and the other machine keys stay as they are here." % len(changed))
        if skipped:
            lines.append("config: not restored (multi-line value): %s. Set them by hand." % ", ".join(skipped))
    if wrote:
        lines.append("restore: " + ", ".join(wrote))
    _stamp_beam(root, name)
    return 0, lines


def _stamp_beam(root, name: str) -> None:
    """Put the run name on the live beam, so a process with no config can find it."""
    path = Path(root) / ".warp" / "beam.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    data["run"] = name
    if isinstance(data.get("config"), dict):
        data["config"]["run"] = name
    path.write_text(json.dumps(data, indent=2) + "\n")


def start_blocker(root, beam_path) -> Optional[str]:
    """Why `/warp-start` must not run yet, or None. A run needs a scan first."""
    name = current(root)
    if not name:
        return None
    path = Path(beam_path)
    try:
        data = json.loads(path.read_text()) if path.is_file() else None
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("tickets"), dict) and data["tickets"]:
        return None
    return "refuse: %s has no scan. Run /warp-scan [folder] first." % name


# --- owner and takeover ------------------------------------------------------


def _git_user() -> str:
    try:
        done = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    except OSError:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def identity() -> dict:
    """Who is running this process: a name, the machine id, and the host."""
    import agents

    by = _git_user() or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    return {"by": by, "machine": agents.machine_id(), "host": agents._hostname()}


def stamp_owner(beam: dict, session: str = "") -> dict:
    """Record who started this run just now. The caller writes the beam."""
    who = identity()
    who["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    who["session"] = session
    beam["owner"] = who
    return who


def owner_text(owner: dict) -> str:
    where = owner.get("host") or owner.get("machine") or "unknown host"
    return "%s on %s, since %s" % (owner.get("by") or "unknown", where, (owner.get("at") or "unknown")[:16].replace("T", " "))


def owner_conflict(root, run: str, beam: dict, ref: str) -> Optional[dict]:
    """The published owner when another machine took this run over after we did, else None."""
    mine = beam.get("owner") if isinstance(beam, dict) else None
    if not isinstance(mine, dict) or not mine.get("machine"):
        return None
    published = (show_json(root, ref, run, ".warp/beam.json") or {}).get("owner")
    if not isinstance(published, dict) or not published.get("machine"):
        return None
    if published.get("machine") == mine.get("machine"):
        return None
    if str(published.get("at") or "") > str(mine.get("at") or ""):
        return published
    return None


def owner_blocker(root, run: str, takeover: bool = False) -> tuple:
    """(refusal or None, notes). A run another machine is running needs `--takeover`."""
    if not run:
        return None, []
    ref = fetch_ref(root)
    info = summary(root, ref, run)
    if info is None or not info["live"]:
        return None, []
    published = info.get("owner") or {}
    if not published.get("machine") or published.get("machine") == identity()["machine"]:
        return None, []
    seen = _ago(info["seenMinutes"])
    if takeover:
        return None, [
            "takeover: %s is running under %s (parent last seen %s). This session takes it over. "
            "Their parent ends its turn on its next pass." % (run, owner_text(published), seen)
        ]
    return (
        "refuse: %s is running under %s (parent last seen %s). /warp-start --takeover takes it over. "
        "/warp-init %s shows its state." % (run, owner_text(published), seen, run),
        [],
    )


# --- start after, cross-run dependencies -------------------------------------


def parse_start_after(text: str) -> list:
    """`core:G2, admin:G1` -> [("core", "G2"), ("admin", "G1")]. Bad pieces are left out."""
    found = []
    for piece in re.split(r"[,\s]+", str(text or "").strip().strip("[]")):
        piece = piece.strip("\"'")
        if ":" not in piece:
            continue
        run, _, gate = piece.partition(":")
        run, gate = run.strip().lower(), gate.strip().upper()
        if valid_name(run)[0] and re.match(r"^G\d+$", gate):
            found.append((run, gate))
    return found


def split_dep(dep: str) -> tuple:
    """`core:P-014` -> ("core", "P-014"). A plain id gives ("", id)."""
    text = str(dep or "").strip()
    head, sep, tail = text.partition(":")
    if sep and tail and valid_name(head.lower())[0]:
        return head.lower(), tail.strip()
    return "", text


def dependents(root, ref: str, name: str) -> list:
    """Runs that wait on `name`: through startAfter, or a ticket dependency `name:<id>`."""
    found = []
    for other in list_runs(root, ref):
        if other == name:
            continue
        cfg_text = show(root, ref, other, ".warp/config.yaml") or ""
        waits = [gate for run, gate in parse_start_after(read_key(cfg_text, "startAfter") or "") if run == name]
        beam = show_json(root, ref, other, ".warp/beam.json") or {}
        for row in (beam.get("tickets") or {}).values():
            if isinstance(row, dict):
                for dep in (row.get("deps") or []) + (row.get("after") or []):
                    if split_dep(dep)[0] == name:
                        waits.append("ticket %s" % split_dep(dep)[1])
        if waits:
            found.append((other, sorted(set(waits))))
    return found


# --- delete ---------------------------------------------------------------


def delete_run(root, name: str, apply: bool = False, force: bool = False, branches: bool = False) -> int:
    """`/warp-delete <name>`. A dry run unless `apply`. Returns an exit code."""
    import state_commit

    root = Path(root)
    ok, why = valid_name(name)
    if not ok:
        print("refuse: %s" % why)
        return 2
    ref = fetch_ref(root)
    info = summary(root, ref, name)
    listed = _git(root, "ls-tree", "-r", "--name-only", ref, folder_rel(name) + "/")
    files = [f for f in listed.stdout.splitlines() if f.strip()] if listed.returncode == 0 else []
    if info is None and not files:
        print("run: %s not found on %s. Nothing to delete." % (name, ref))
        return 2
    base = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
    print("delete: run %s on %s: %d files in %s/" % (name, ref, len(files), folder_rel(name)))
    if info is not None:
        counts = info["counts"]
        print("state: %s, parent last seen %s. %d merged, %d open, %d queued" % (info["runState"], _ago(info["seenMinutes"]), counts["merged"], counts["open"], counts["queued"]))
    refused = []
    if info is not None and info["live"]:
        refused.append("%s is running (parent last seen %s). /warp-stop it first" % (name, _ago(info["seenMinutes"])))
    for other, waits in dependents(root, ref, name):
        refused.append("%s waits on %s (%s)" % (other, name, ", ".join(waits)))
    if refused and not force:
        for line in refused:
            print("refuse: %s." % line)
        print("--force deletes it anyway.")
        return 2
    for line in refused:
        print("warn: %s. Deleting anyway (--force)." % line)
    heads = _git(root, "ls-remote", "--heads", "origin", "refs/heads/warp/%s/*" % name)
    names = [row.split("refs/heads/", 1)[1] for row in heads.stdout.splitlines() if "refs/heads/" in row] if heads.returncode == 0 else []
    print("branches: %d on origin under warp/%s/. %s" % (len(names), name, "They are deleted too (--branches)." if branches else "They are kept. --branches deletes them, and closes their open pull requests."))
    print("kept: merged code, Jira, and pull requests that are already merged.")
    if not apply:
        print("dry run: nothing deleted. Add --apply to remove %s/ from %s." % (folder_rel(name), base))
        return 0
    code = state_commit.remove_run_folder(root, name, base)
    if code != 0:
        return code
    if branches and names:
        gone = _git(root, "push", "origin", "--delete", *names)
        print("branches: deleted %d" % len(names) if gone.returncode == 0 else "branches: delete failed: %s" % (gone.stderr or gone.stdout).strip())
    if read_run(root) == name:
        moved = _move_aside(root, name)
        path = config_path(root)
        path.write_text(set_key(path.read_text(), "run", ""))
        print("local: this checkout ran %s. Its files are kept in %s/ and run: is cleared." % (name, moved))
    print("deleted: %s from %s. Merge or pull %s to share it." % (name, base, base))
    return 0


# --- what other runs hold: start after, dependencies, locks, paths -----------

EXTERNAL_MINUTES = 5


def start_after_status(root, ref: str, pairs: list) -> list:
    """For each (run, gate): (run, gate, met, why). Met means the gate and every block before it are cleared."""
    import blocks

    out = []
    for other, gate in pairs:
        beam = show_json(root, ref, other, ".warp/beam.json")
        if beam is None:
            out.append((other, gate, False, "no run named %s is published. /warp-init %s, /warp-scan, and /warp-start it first" % (other, other)))
            continue
        order = blocks.keys(beam)
        if gate not in order:
            out.append((other, gate, False, "%s has no gate %s. It has: %s" % (other, gate, ", ".join(order) or "none")))
            continue
        rows = blocks.states(beam)
        upto = rows[: order.index(gate) + 1]
        stuck = next((row for row in upto if not row["cleared"]), None)
        if stuck is None:
            out.append((other, gate, True, "%s cleared %s" % (other, gate)))
        else:
            out.append(
                (other, gate, False, "%s is at %s (%d of %d merged). It must clear %s first" % (other, stuck["key"], stuck["merged"], stuck["total"], gate))
            )
    return out


def start_after_blocker(root, run: str, text: str) -> tuple:
    """(refusal or None, lines). A run with `startAfter: core:G2` waits until core's G2 is cleared."""
    pairs = parse_start_after(text)
    if not pairs:
        return None, []
    ref = fetch_ref(root)
    status = start_after_status(root, ref, pairs)
    lines = ["startAfter: %s:%s %s." % (o, g, "met (%s)" % why if met else "not met (%s)" % why) for o, g, met, why in status]
    if all(met for _o, _g, met, _why in status):
        return None, lines
    waits = ", ".join("%s:%s" % (o, g) for o, g, met, _why in status if not met)
    return "refuse: %s starts after %s. /warp-start --now starts it anyway." % (run, waits), lines


def _active_locks(beam: dict, other: str) -> list:
    import orchestrator
    from beam import ACTIVE

    rows = []
    for tid, row in (beam.get("tickets") or {}).items():
        if isinstance(row, dict) and row.get("status") in ACTIVE:
            locks = orchestrator.lock_paths(row)
            if locks:
                rows.append({"run": other, "id": str(tid), "locks": locks})
    return rows


def refresh_external(root, beam: dict, force: bool = False, now: Optional[datetime] = None) -> bool:
    """Record what other runs hold, on the live beam: statuses of tickets this run depends on, and active locks.

    Reads only what the other runs published. Throttled to every few minutes
    unless `force`. Returns True when the beam changed. The caller saves it.
    """
    me = str(beam.get("run") or "")
    if not me:
        return False
    held = beam.get("external") if isinstance(beam.get("external"), dict) else {}
    stamp = _parse_time(held.get("at"))
    now = now or datetime.now(timezone.utc)
    if not force and stamp is not None and (now - stamp).total_seconds() < EXTERNAL_MINUTES * 60:
        return False
    ref = fetch_ref(root)
    wanted: dict = {}
    for row in (beam.get("tickets") or {}).values():
        for dep in (row.get("deps") or []) + (row.get("after") or []):
            other, tid = split_dep(dep)
            if other and other != me:
                wanted.setdefault(other, set()).add(tid)
    status: dict = {}
    locks: list = []
    here = list_runs(root, ref)
    for other in here:
        if other == me:
            continue
        published = show_json(root, ref, other, ".warp/beam.json") or {}
        rows = published.get("tickets") or {}
        for tid in wanted.get(other, ()):
            if tid in rows:
                status["%s:%s" % (other, tid)] = (rows[tid] or {}).get("status")
        locks.extend(_active_locks(published, other))
    # Runs in another repo (a build-schedule/2 program across repos): externalRepos names where.
    remote = {}
    elsewhere = sorted(set(wanted) - set(here) - {me})
    if elsewhere:
        import buildsched

        where = buildsched.parse_map(beam.get("externalRepos"))
        for other in elsewhere:
            if other not in where:
                continue
            got = external_repo_ref(root, other, where[other])
            remote[other] = got or "unreachable"
            if not got:
                continue
            published = show_json(root, got, other, ".warp/beam.json") or {}
            rows = published.get("tickets") or {}
            for tid in wanted.get(other, ()):
                if tid in rows:
                    status["%s:%s" % (other, tid)] = (rows[tid] or {}).get("status")
            # Locks in another repo never overlap this repo's paths, so they are not held here.
    beam["external"] = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "ref": ref, "status": status, "locks": locks}
    if remote:
        beam["external"]["repos"] = remote
    return True


def external_repo_ref(root, run: str, where: str) -> Optional[str]:
    """Fetch another repo's base branch into refs/warp/external/<run> and return that ref, or None.

    `where` is a git URL or a local path, optionally `#branch` (default: main, then master).
    """
    text = str(where or "").strip()
    if not text:
        return None
    url, _, branch = text.partition("#")
    ref = "refs/warp/external/%s" % run
    for name in ([branch] if branch else ["main", "master"]):
        done = _git(root, "fetch", "-q", "--depth=1", url, "+%s:%s" % (name, ref))
        if done.returncode == 0:
            return ref
    return None


def external_status(beam: dict, dep: str) -> Optional[str]:
    """Status of a dependency in this beam, or in another run's published beam when it is written `run:id`."""
    row = (beam.get("tickets") or {}).get(dep)
    if row is not None:
        return row.get("status")
    held = beam.get("external") if isinstance(beam.get("external"), dict) else {}
    return (held.get("status") or {}).get(dep)


def external_locks(beam: dict) -> list:
    """[(label, locks)] other runs hold right now, as last read."""
    held = beam.get("external") if isinstance(beam.get("external"), dict) else {}
    return [("%s:%s" % (row.get("run"), row.get("id")), list(row.get("locks") or [])) for row in held.get("locks") or []]


def parse_paths(text) -> list:
    """`HOS/UI/Admin, packages/admin` -> ["HOS/UI/Admin", "packages/admin"]. A list is cleaned the same way."""
    raw = text if isinstance(text, (list, tuple)) else re.split(r"[,\n]+", str(text or "").strip().strip("[]"))
    found = []
    for piece in raw:
        item = str(piece).strip().strip("\"'").strip().strip("/")
        if item and item not in {".", "*", "**"} and item not in found:
            found.append(item)
    return found


def _lock_base(lock: str) -> str:
    text = str(lock or "").strip().strip("/")
    for tail in ("/**", "/*"):
        if text.endswith(tail):
            text = text[: -len(tail)]
    return text


def outside_paths(paths: list, locks: list) -> list:
    """The locks that fall outside every path in `paths`. No paths means no scope: nothing is outside."""
    if not paths:
        return []
    out = []
    for lock in locks or []:
        base = _lock_base(lock)
        if not any(base == p or base.startswith(p + "/") for p in paths):
            out.append(lock)
    return out


def scope_warnings(root, run: str, beam: dict) -> list:
    """Lines for a scan: tickets whose locks leave this run's paths, and dependencies on runs that are not published."""
    lines = []
    paths = parse_paths(read_key(config_path(root).read_text() if config_path(root).is_file() else "", "paths") or "")
    if paths:
        stray = {}
        for tid, row in (beam.get("tickets") or {}).items():
            bad = outside_paths(paths, row.get("locks") or [])
            if bad:
                stray[tid] = bad
        if stray:
            lines.append(
                "paths: %d tickets have locks outside this run's paths (%s) and will not start: %s"
                % (len(stray), ", ".join(paths), ", ".join("%s (%s)" % (t, ", ".join(b[:2])) for t, b in sorted(stray.items())[:6]))
            )
    try:
        ref = fetch_ref(root)
        known = set(list_runs(root, ref))
    except Exception:
        return lines
    missing = {}
    for tid, row in (beam.get("tickets") or {}).items():
        for dep in (row.get("deps") or []) + (row.get("after") or []):
            other, dep_id = split_dep(dep)
            if not other or other == run:
                continue
            if other not in known:
                missing.setdefault(other, []).append("%s needs %s" % (tid, dep))
            else:
                published = show_json(root, ref, other, ".warp/beam.json") or {}
                if dep_id not in (published.get("tickets") or {}):
                    missing.setdefault(other, []).append("%s needs %s, which %s does not have" % (tid, dep, other))
    for other, rows in sorted(missing.items()):
        lines.append("warn: %s is not ready for %s: %s. Those tickets wait until it is." % (other, run, "; ".join(rows[:4])))
    return lines


# --- checkpoints -----------------------------------------------------------


def checkpoint_file(warp) -> Path:
    return Path(warp) / "checkpoint.json"


def signature(beam: dict) -> str:
    rows = sorted((str(tid), str((row or {}).get("status"))) for tid, row in (beam.get("tickets") or {}).items() if isinstance(row, dict))
    text = json.dumps([beam.get("runState"), bool(beam.get("paused")), rows], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def checkpoint_minutes(beam: dict) -> int:
    raw = (beam.get("config") or {}).get("checkpointMinutes")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CHECKPOINT_MINUTES
    return value if value > 0 else DEFAULT_CHECKPOINT_MINUTES


def checkpoint_due(warp, beam: dict, now: Optional[datetime] = None) -> bool:
    """True when the published copy is behind: a status changed, or the interval passed."""
    path = checkpoint_file(warp)
    try:
        last = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(last, dict) or last.get("sig") != signature(beam):
        return True
    minutes = age_minutes(last.get("at"), now)
    return minutes is None or minutes >= checkpoint_minutes(beam)


def record_checkpoint(warp, beam: dict, at: str) -> None:
    try:
        checkpoint_file(warp).write_text(json.dumps({"at": at, "sig": signature(beam)}) + "\n")
    except OSError:
        pass


# --- command line ----------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Named Warp runs", epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("list", "name"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=".")
    del_p = sub.add_parser("delete")
    del_p.add_argument("run")
    del_p.add_argument("--root", default=".")
    del_p.add_argument("--apply", action="store_true", help="remove the run folder from the base branch")
    del_p.add_argument("--force", action="store_true", help="delete a running run, or one another run waits on")
    del_p.add_argument("--branches", action="store_true", help="also delete the run's branches on origin")
    show_p = sub.add_parser("show")
    show_p.add_argument("run")
    show_p.add_argument("--root", default=".")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    root = Path(args.root).resolve()
    if args.cmd == "name":
        name = current(root)
        print("run: %s" % name if name else "run: none (unnamed run)")
        return 0
    if args.cmd == "delete":
        return delete_run(root, args.run, apply=args.apply, force=args.force, branches=args.branches)
    ref = fetch_ref(root)
    if args.cmd == "list":
        names = list_runs(root, ref)
        mine = current(root)
        if mine and mine not in names:
            print("run: %s (this checkout) not published yet" % mine)
        if not names:
            print("runs: none on %s" % ref)
            return 0
        print("runs: %d on %s" % (len(names), ref))
        for name in names:
            info = summary(root, ref, name)
            if info is None:
                continue
            counts = info["counts"]
            owner = info.get("owner") or {}
            pairs = info.get("startAfter") or []
            checked = start_after_status(root, ref, pairs) if pairs else []
            wait = ",".join("%s:%s(%s)" % (o, g, "met" if met else "waits") for o, g, met, _why in checked) or "-"
            print(
                "run: %s%s state=%s%s owner=%s folder=%s merged=%d open=%d queued=%d seen=%s startAfter=%s"
                % (
                    name,
                    " (this checkout)" if name == mine else "",
                    info["runState"],
                    " live" if info["live"] else "",
                    (owner.get("by") or "-").replace(" ", "_"),
                    info["folder"] or "whole repo",
                    counts["merged"],
                    counts["open"],
                    counts["queued"],
                    _ago(info["seenMinutes"]),
                    wait,
                )
            )
        return 0
    info = summary(root, ref, args.run)
    if info is None:
        print("run: %s not found on %s" % (args.run, ref))
        return 2
    for line in notice_lines(info):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
