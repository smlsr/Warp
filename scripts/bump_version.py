#!/usr/bin/env python3
"""Bump the Warp version in every place it lives, and stub the changelog.

VERSION is the source of truth. This writes that file, the same number in
.cursor-plugin/plugin.json, and a ## heading in CHANGELOG.md.

  bump_version.py                 patch: 1.3.1 -> 1.3.2
  bump_version.py --minor         1.3.1 -> 1.4.0
  bump_version.py --major         1.3.1 -> 2.0.0
  bump_version.py --note "text"   bullet under the new heading
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_version  # noqa: E402
import version  # noqa: E402


def bump(current: str, kind: str) -> str:
    major, minor, patch = check_version.parse(current)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def write_manifest(path: Path, new: str) -> None:
    text = path.read_text()
    updated, n = re.subn(r'("version"\s*:\s*")[^"]+(")', rf"\g<1>{new}\2", text, count=1)
    if n != 1:
        raise SystemExit(f"could not find version in {path}")
    path.write_text(updated)


def write_changelog(path: Path, new: str, note: str | None) -> None:
    bullet = note.strip() if note else ""
    if not bullet:
        bullet = "Describe this change."
    block = f"## {new}\n\n- {bullet}\n"
    if not path.is_file():
        path.write_text("# Changelog\n\n" + block)
        return
    text = path.read_text()
    if re.search(rf"^## {re.escape(new)}\s*$", text, re.M):
        raise SystemExit(f"CHANGELOG.md already has ## {new}")
    marker = "# Changelog\n"
    if text.startswith(marker):
        rest = text[len(marker):].lstrip("\n")
        path.write_text(marker + "\n" + block + "\n" + rest)
        return
    path.write_text(block + "\n" + text)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bump the Warp patch, minor, or major version")
    p.add_argument("--root", default=str(version.PLUGIN_ROOT))
    group = p.add_mutually_exclusive_group()
    group.add_argument("--patch", action="store_true", help="bump patch (default)")
    group.add_argument("--minor", action="store_true")
    group.add_argument("--major", action="store_true")
    p.add_argument("--note", help="changelog bullet; default is a stub you replace")
    args = p.parse_args(argv)
    root = Path(args.root).resolve()
    kind = "major" if args.major else "minor" if args.minor else "patch"
    current = version.read_version_file(root) or version.read_manifest_version(root)
    if not current:
        raise SystemExit(f"no version in {root}")
    new = bump(current, kind)
    (root / "VERSION").write_text(new + "\n")
    manifest = root / ".cursor-plugin" / "plugin.json"
    if not manifest.is_file():
        raise SystemExit(f"missing {manifest}")
    write_manifest(manifest, new)
    write_changelog(root / "CHANGELOG.md", new, args.note)
    print(f"{current} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
