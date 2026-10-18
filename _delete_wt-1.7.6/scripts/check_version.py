#!/usr/bin/env python3
"""Fail when the version files disagree, the changelog has no entry, or a PR did not bump.

  check_version.py                  this repo: VERSION, manifest, CHANGELOG
  check_version.py --against REF    also require this version to be newer than REF

An equal version passes when the working tree matches REF. That is a pull
request that is already merged: the check often starts after main has the
same tree, and the version is no longer newer. A change that leaves the
version equal to REF still fails.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import version  # noqa: E402

def parse(value: str) -> tuple[int, int, int]:
    parts = value.strip().lstrip("v").split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"not a major.minor.patch version: {value!r}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def problems(root: Path) -> list[str]:
    root = root.resolve()
    errors = []
    file_v = version.read_version_file(root)
    manifest = version.read_manifest_version(root)
    if not file_v:
        errors.append("VERSION is missing")
    if not manifest:
        errors.append(".cursor-plugin/plugin.json has no version")
    if file_v and manifest and file_v != manifest:
        errors.append(f"VERSION is {file_v} but plugin.json is {manifest}")
    current = file_v or manifest
    changelog = root / "CHANGELOG.md"
    if not changelog.is_file():
        errors.append("CHANGELOG.md is missing")
    elif current and not re.search(rf"^## {re.escape(current)}\s*$", changelog.read_text(), re.M):
        errors.append(f"CHANGELOG.md has no ## {current} entry")
    return errors


def version_at(ref: str, cwd: Path) -> str:
    for rel in ("VERSION", ".cursor-plugin/plugin.json"):
        out = subprocess.run(
            ["git", "show", f"{ref}:{rel}"], cwd=cwd, capture_output=True, text=True
        )
        if out.returncode != 0:
            continue
        if rel.endswith(".json"):
            try:
                value = json.loads(out.stdout).get("version")
            except json.JSONDecodeError:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
        elif out.stdout.strip():
            return out.stdout.strip()
    raise ValueError(f"no version at {ref}")


def trees_match(root: Path, ref: str) -> bool:
    """True when no tracked path differs from ref. Untracked files do not count."""
    out = subprocess.run(
        ["git", "diff", "--quiet", ref, "--"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return out.returncode == 0


def against(root: Path, ref: str) -> list[str]:
    current = version.read_version_file(root) or version.read_manifest_version(root)
    if not current:
        return ["no current version to compare"]
    try:
        base = version_at(ref, root)
        if parse(current) > parse(base):
            return []
        if parse(current) == parse(base) and trees_match(root, ref):
            return []
    except ValueError as e:
        return [str(e)]
    return [f"version {current} is not newer than {ref} ({base})"]


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Check Warp version files")
    p.add_argument("--root", default=str(version.PLUGIN_ROOT))
    p.add_argument("--against", help="git ref that this version must be newer than")
    import usage

    args = p.parse_args(usage.normalize_argv(argv))
    root = Path(args.root).resolve()
    found = problems(root)
    if args.against:
        found += against(root, args.against)
    if found:
        for item in found:
            print(item, file=sys.stderr)
        return 1
    current = version.version_of(root)
    print(f"version {current} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
