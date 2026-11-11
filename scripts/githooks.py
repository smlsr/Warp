#!/usr/bin/env python3
"""Install Warp's git hooks and make targets in the project (1.7.24). Idempotent; init and upgrade run it.

  - .warp/hooks/no-case-collisions.sh, from the plugin's githooks/
  - a block between `# BEGIN WARP HOOK` and `# END WARP HOOK` in pre-commit and pre-push, in the hooks folder git uses
    (`git rev-parse --git-path hooks`, which honours core.hooksPath); an existing hook keeps its own lines, the block
    goes right after its first line so an `exit 0` at its end cannot skip it; a second run replaces the block
  - `make fix-gates` and `make fix-case-collisions`, between `# BEGIN WARP TARGETS` and `# END WARP TARGETS`, in an
    existing Makefile that has neither target (a repo without a Makefile is told the script to run instead)
  - `.backup/` in .git/info/exclude, for the repair script's backups

  python3 scripts/githooks.py install [--root .] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOK_NAME = "no-case-collisions.sh"
BEGIN, END = "# BEGIN WARP HOOK", "# END WARP HOOK"
TBEGIN, TEND = "# BEGIN WARP TARGETS", "# END WARP TARGETS"
PLUGIN_REL = ".cursor/plugins/warp"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def _block(kind: str) -> str:
    return "\n".join([
        BEGIN + " (no-case-collisions, 1.7.24; bypass with --no-verify)",
        'warp_top="$(git rev-parse --show-toplevel)"',
        'if [ -x "$warp_top/.warp/hooks/%s" ]; then "$warp_top/.warp/hooks/%s" %s || exit 1; fi' % (HOOK_NAME, HOOK_NAME, kind),
        END,
    ]) + "\n"


def _with_block(text: str, kind: str) -> str:
    block = _block(kind)
    pattern = re.compile(r"^%s.*?^%s[^\n]*\n?" % (re.escape(BEGIN), re.escape(END)), re.S | re.M)
    if pattern.search(text):
        return pattern.sub(lambda _m: block, text, count=1)
    if not text.strip():
        return "#!/bin/sh\n" + block
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        return lines[0] + (("" if lines[0].endswith("\n") else "\n")) + block + "".join(lines[1:])
    return block + text


def _targets() -> str:
    script = "%s/scripts/fix_gates_case.sh" % PLUGIN_REL
    return "\n".join([
        TBEGIN + " (1.7.24)",
        ".PHONY: fix-gates fix-case-collisions",
        "fix-gates:",
        "\tbash %s" % script,
        "fix-case-collisions:",
        "\tbash %s --all" % script,
        TEND,
    ]) + "\n"


def install(root: Path, dry: bool = False, plugin_root: Path = PLUGIN_ROOT) -> list:
    root = Path(root).resolve()
    steps = []
    top = _git(root, "rev-parse", "--show-toplevel")
    if not top:
        return [("skip", "not a git checkout: no git hooks installed")]
    top = Path(top)
    src = Path(plugin_root) / "githooks" / HOOK_NAME
    if not src.is_file():
        return [("warn", "githooks/%s is missing from the plugin" % HOOK_NAME)]
    dst = top / ".warp" / "hooks" / HOOK_NAME
    body = src.read_text()
    if not dst.is_file() or dst.read_text() != body:
        if not dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(body)
            dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        steps.append(("done", "wrote .warp/hooks/%s" % HOOK_NAME))
    else:
        steps.append(("skip", ".warp/hooks/%s is current" % HOOK_NAME))
    hooks = _git(top, "rev-parse", "--git-path", "hooks")
    hooks_dir = (top / hooks) if hooks and not os.path.isabs(hooks) else Path(hooks or (top / ".git" / "hooks"))
    for kind in ("pre-commit", "pre-push"):
        path = hooks_dir / kind
        old = path.read_text() if path.is_file() else ""
        new = _with_block(old, kind)
        if new != old:
            if not dry:
                hooks_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(new)
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            steps.append(("done", "%s the case-collision check in %s" % ("updated" if BEGIN in old else "added", path)))
        else:
            steps.append(("skip", "%s already runs the case-collision check" % path))
    make = top / "Makefile"
    if make.is_file():
        text = make.read_text()
        has_block = TBEGIN in text
        has_own = re.search(r"^fix-gates\s*:", text, re.M) and not has_block
        if has_own:
            steps.append(("skip", "Makefile has its own fix-gates target; left as is"))
        else:
            pattern = re.compile(r"^%s.*?^%s[^\n]*\n?" % (re.escape(TBEGIN), re.escape(TEND)), re.S | re.M)
            new = pattern.sub(lambda _m: _targets(), text, count=1) if has_block else \
                text + ("" if text.endswith("\n") or not text else "\n") + "\n" + _targets()
            if new != text:
                if not dry:
                    make.write_text(new)
                steps.append(("done", "added make fix-gates and make fix-case-collisions to the Makefile"))
            else:
                steps.append(("skip", "Makefile already has make fix-gates"))
    else:
        steps.append(("skip", "no Makefile: run `bash %s/scripts/fix_gates_case.sh` for fix-gates" % PLUGIN_REL))
    exclude = top / (_git(top, "rev-parse", "--git-path", "info/exclude") or ".git/info/exclude")
    current = exclude.read_text() if exclude.is_file() else ""
    if ".backup/" not in current.splitlines():
        if not dry:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(current + ("" if current.endswith("\n") or not current else "\n") + ".backup/\n")
        steps.append(("done", "ignored .backup/ in .git/info/exclude"))
    return steps


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("--root", default=".")
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    for status, msg in install(Path(args.root), dry=args.dry_run):
        print("  [%s] %s" % (status, msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
