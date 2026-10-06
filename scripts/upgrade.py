#!/usr/bin/env python3
"""Replace `.cursor/plugins/warp` with the current plugin source.

The source is this command's plugin tree, or `--source`. When that tree is a
git checkout, the default branch is fetched first and that tree is what gets
copied. A failed fetch leaves the installed copy in place. `.warp/config.yaml`
and the beam are not touched.

  python3 scripts/upgrade.py ?
  python3 scripts/upgrade.py --root .

?, help, -h, and --help print this text. Quote ? if the shell expands it.
Prints `Warp vX.Y.Z` and tells you to reload Cursor. Safe to run again.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import version  # noqa: E402

HELP = __doc__
PLUGIN_REL = Path(".cursor/plugins/warp")
COPY_IGNORE = {".git", ".cursor", ".warp", "__pycache__", "node_modules"}
KEEP_STATE = ("config.yaml", "beam.json")


def _git(root: Path, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _is_git(source: Path) -> bool:
    try:
        probe = _git(source, "rev-parse", "--is-inside-work-tree", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def default_branch(source: Path) -> str:
    head = _git(source, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD", timeout=10)
    if head.returncode == 0 and head.stdout.strip():
        name = head.stdout.strip()
        return name.split("/", 1)[-1]
    for branch in ("main", "master"):
        have = _git(source, "rev-parse", "--verify", "-q", "refs/remotes/origin/%s" % branch, timeout=10)
        if have.returncode == 0:
            return branch
    return "main"


def fetched_tree(source: Path) -> tuple[Optional[Path], str]:
    """Fetch origin's default branch. (tree, "") or (None, error)."""
    if not _is_git(source):
        return source, ""
    remote = _git(source, "remote", "get-url", "origin", timeout=10)
    if remote.returncode != 0 or not remote.stdout.strip():
        return source, ""
    branch = default_branch(source)
    try:
        fetched = _git(source, "fetch", "--no-tags", "origin", branch, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if fetched.returncode != 0:
        detail = (fetched.stderr or fetched.stdout or "fetch failed").strip()
        return None, detail
    dest = Path(tempfile.mkdtemp(prefix="warp-upgrade-"))
    archive = subprocess.run(
        ["git", "-C", str(source), "archive", "origin/%s" % branch],
        capture_output=True,
        timeout=30,
    )
    if archive.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        err = archive.stderr.decode("utf-8", "replace").strip() or "git archive failed"
        return None, err
    unpacked = subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, capture_output=True)
    if unpacked.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return None, "could not unpack the fetched tree"
    return dest, ""


def copy_tree(src: Path, dst: Path) -> int:
    """Overwrite dst with src. Returns the number of files copied."""
    count = 0
    wanted = set()
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in COPY_IGNORE for part in rel.parts):
            continue
        if not path.is_file():
            continue
        wanted.add(rel.as_posix())
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    if dst.is_dir():
        for path in sorted(dst.rglob("*"), reverse=True):
            if not path.is_file():
                continue
            rel = path.relative_to(dst).as_posix()
            if rel not in wanted and not any(part in COPY_IGNORE for part in Path(rel).parts):
                path.unlink()
    return count


def upgrade(root: Path, source: Optional[Path] = None) -> int:
    root = root.resolve()
    source = (source or version.PLUGIN_ROOT).resolve()
    plugin = root / PLUGIN_REL
    state = root / ".warp"
    before = {}
    for name in KEEP_STATE:
        path = state / name
        if path.is_file():
            before[name] = path.read_bytes()
    tree, err = fetched_tree(source)
    if tree is None:
        print("fetch failed: %s" % err)
        print("keeping the installed copy")
        return 0
    cleanup = tree != source
    try:
        if plugin.resolve() == tree.resolve():
            landed = version.version_of(plugin) or version.version_of(source)
            print(version.label(landed))
            print("Reload Cursor (Developer: Reload Window).")
            return 0
        plugin.parent.mkdir(parents=True, exist_ok=True)
        plugin.mkdir(parents=True, exist_ok=True)
        copy_tree(tree, plugin)
    finally:
        if cleanup:
            shutil.rmtree(tree, ignore_errors=True)
    for name, blob in before.items():
        path = state / name
        if not path.is_file() or path.read_bytes() != blob:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
    landed = version.version_of(plugin) or version.version_of(source)
    print(version.label(landed))
    print("Reload Cursor (Developer: Reload Window).")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Upgrade the installed Warp plugin", epilog=HELP)
    parser.add_argument("--root", default=".")
    parser.add_argument("--source", help="plugin tree to copy (default: the tree this command is running from)")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    source = Path(args.source).resolve() if args.source else None
    return upgrade(Path(args.root), source)


if __name__ == "__main__":
    sys.exit(main())
