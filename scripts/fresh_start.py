#!/usr/bin/env python3
"""Clean up what a killed process left behind, before /warp-start and /warp-resume.

Two kinds of leftovers, handled differently:

- Warp's temporary files (`.warp/**/*.tmp`, from a save that died between writing and renaming). A save
  renames its temp file within milliseconds, so one older than `STALE_MINUTES` (10) is an orphan. Removed.
  `.warp/worktrees/` is ticket code and is never touched.
- Git's lock files (`index.lock`, `HEAD.lock`, `config.lock`, `packed-refs.lock`, `refs/**/*.lock`,
  `objects/maintenance.lock`, `worktrees/*/index.lock`) in this checkout and, in a multi-repo run, each
  sibling clone. Git treats such a file as "locked" for as long as it exists, so one left by a killed git makes
  every later git command (and Warp's state publish) fail with `File exists`. One is removed only when it is
  older than `STALE_MINUTES` and no git process is running; otherwise it is named and left alone. A lock that
  cannot be deleted is moved to `<git dir>/_delete/`.

Warp's own lock files (`.warp/.listener.lock`, `.beam.lock`, …) are never removed: they are kernel locks the
operating system releases when their process dies, so a stale one blocks nothing, and deleting one that a
live process holds would let a second process lock a new file at the same time.

  python3 scripts/fresh_start.py --root .            what start and resume run
  python3 scripts/fresh_start.py --root . --dry-run  list without removing

?, help, -h, and --help print this text.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

HELP = __doc__
STALE_MINUTES = 10
GIT_LOCKS = ("index.lock", "HEAD.lock", "ORIG_HEAD.lock", "FETCH_HEAD.lock", "config.lock", "packed-refs.lock",
             "shallow.lock", "objects/maintenance.lock")


def _age_minutes(path: Path, now: float) -> float:
    try:
        return (now - path.stat().st_mtime) / 60.0
    except OSError:
        return 0.0


def git_running() -> Optional[bool]:
    """True when any git process runs on this machine, None when that cannot be told."""
    if shutil.which("pgrep"):
        try:
            return subprocess.run(["pgrep", "-x", "git"], capture_output=True, text=True, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return None
    if shutil.which("ps"):
        try:
            out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        return any(Path(line.strip()).name == "git" for line in out.splitlines())
    return None


def _git_dirs(repo: Path) -> list:
    """The repo's git dir and, for a linked worktree, the common dir: where its lock files live."""
    found = []
    for flag in ("--git-dir", "--git-common-dir"):
        try:
            out = subprocess.run(["git", "-C", str(repo), "rev-parse", flag], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            path = Path(out.stdout.strip())
            path = (repo / path).resolve() if not path.is_absolute() else path.resolve()
            if path.is_dir() and path not in found:
                found.append(path)
    return found


def git_lock_files(repo: Path) -> list:
    locks = []
    for git_dir in _git_dirs(repo):
        for rel in GIT_LOCKS:
            if (git_dir / rel).is_file():
                locks.append(git_dir / rel)
        refs = git_dir / "refs"
        if refs.is_dir():
            locks.extend(p for p in refs.rglob("*.lock") if p.is_file())
        trees = git_dir / "worktrees"
        if trees.is_dir():
            locks.extend(p for p in trees.glob("*/index.lock") if p.is_file())
    out = []
    for path in locks:
        if path not in out:
            out.append(path)
    return out


def _remove(path: Path, git_dir: Optional[Path], dry: bool) -> str:
    if dry:
        return "would remove"
    try:
        path.unlink()
        return "removed"
    except OSError:
        pass
    if git_dir is not None:
        try:
            trash = git_dir / "_delete"
            trash.mkdir(exist_ok=True)
            os.replace(path, trash / ("%s-%d_delete" % (path.name, int(time.time()))))
            return "moved to %s/_delete/ (delete it by hand)" % git_dir.name
        except OSError:
            pass
    return "could not remove"


def clean(root: Path, repos: Optional[list] = None, dry: bool = False, now: Optional[float] = None,
          running: Optional[Callable[[], Optional[bool]]] = None, stale: int = STALE_MINUTES) -> list:
    """Lines for what was cleaned or left. Never raises."""
    root = Path(root).resolve()
    now = time.time() if now is None else now
    lines = []
    try:
        warp = root / ".warp"
        tmps = []
        if warp.is_dir():
            for path in warp.rglob("*.tmp"):
                rel = path.relative_to(warp).parts
                if rel and rel[0] == "worktrees":
                    continue
                if path.is_file() and _age_minutes(path, now) >= stale:
                    tmps.append(path)
        removed = 0
        for path in tmps:
            if _remove(path, None, dry) in ("removed", "would remove"):
                removed += 1
        if removed:
            lines.append("start: %s %d leftover .tmp file%s in .warp (older than %dm)"
                         % ("would remove" if dry else "removed", removed, "" if removed == 1 else "s", stale))
        checked = None
        for repo in [root] + [Path(r) for r in (repos or [])]:
            if not (repo / ".git").exists():
                continue
            for lock in git_lock_files(repo):
                age = _age_minutes(lock, now)
                git_dir = next((d for d in _git_dirs(repo) if d in lock.parents), None)
                shown = "%s/%s" % (repo.name, lock.relative_to(git_dir.parent)) if git_dir else str(lock)
                if age < stale:
                    lines.append("start: %s is %dm old; a git command may be using it. Left in place." % (shown, age))
                    continue
                if checked is None:
                    checked = (running or git_running)()
                if checked is not False:
                    why = "a git process is running" if checked else "cannot tell whether git is running"
                    lines.append("start: %s is %dm old, but %s. Left in place; remove it once git is done." % (shown, age, why))
                    continue
                done = _remove(lock, git_dir, dry)
                lines.append("start: %s stale %s (%dm old, no git running)" % (done, shown, age))
    except Exception as exc:  # cleanup never stops a start
        lines.append("start: cleanup skipped (%s)" % exc)
    return lines


def sibling_paths(beam_path: Path) -> list:
    """A multi-repo run's sibling clones, from the beam."""
    try:
        import multirepo

        data = json.loads(Path(beam_path).read_text())
        home_root = Path(beam_path).resolve().parent.parent
        return [multirepo.sibling_path(home_root, data, name) for name in multirepo.siblings(data)] if multirepo.enabled(data) else []
    except Exception:
        return []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Clean leftovers before a start", epilog=HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".")
    parser.add_argument("--beam", default=".warp/beam.json")
    parser.add_argument("--dry-run", action="store_true")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    root = Path(args.root).resolve()
    beam_path = Path(args.beam) if Path(args.beam).is_absolute() else root / args.beam
    lines = clean(root, sibling_paths(beam_path), dry=args.dry_run)
    for line in lines or ["start: nothing to clean"]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
