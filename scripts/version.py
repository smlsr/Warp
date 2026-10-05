#!/usr/bin/env python3
"""Print the Warp plugin version.

VERSION at the plugin root is the source of truth. `.cursor-plugin/plugin.json`
carries the same number for Cursor. `scripts/check_version.py` fails when they
disagree.

  version.py --root .     installed copy, source copy, and .warp/version

The installed copy is `.cursor/plugins/warp`. The source copy is the plugin
tree this script lives in (the GitHub or local checkout Cursor loaded).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
INSTALLED_REL = Path(".cursor/plugins/warp")
RECORDED_REL = Path(".warp/version")


def read_version_file(root: Path) -> str | None:
    path = root / "VERSION"
    if not path.is_file():
        return None
    text = path.read_text().strip()
    return text or None


def read_manifest_version(root: Path) -> str | None:
    path = root / ".cursor-plugin" / "plugin.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text()).get("version")
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def version_of(root: Path) -> str | None:
    """VERSION if that file is present, otherwise the manifest."""
    return read_version_file(root) or read_manifest_version(root)


def recorded_version(root: Path) -> str | None:
    path = root / RECORDED_REL
    if not path.is_file():
        return None
    text = path.read_text().strip()
    return text or None


def label(version: str | None) -> str:
    return f"Warp v{version}" if version else "Warp"


def upgrade_line(installed: str, source: str) -> str:
    return f"plugin is v{installed}, repo copy is v{source}: run /warp-uninstall then /warp-init"


def describe(root: Path) -> str:
    root = root.resolve()
    source = version_of(PLUGIN_ROOT)
    installed = version_of(root / INSTALLED_REL)
    recorded = recorded_version(root)
    lines = []
    if installed and source and installed != source:
        lines.append(upgrade_line(installed, source))
    else:
        lines.append(label(source or installed))
    lines.append(f"installed: {installed or 'none'} ({INSTALLED_REL})")
    lines.append(f"source: {source or 'none'}")
    if recorded:
        lines.append(f"recorded: {recorded} ({RECORDED_REL})")
    return "\n".join(lines)


def find_root(arg: str | None) -> Path:
    if arg:
        return Path(arg).resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd().resolve()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Print the Warp plugin version",
        epilog="examples:\n  python3 scripts/version.py ?\n  python3 scripts/version.py --root .\n\n"
        "Prints installed (.cursor/plugins/warp), source (this plugin tree), and recorded (.warp/version).\n"
        "?, help, -h, and --help print this text. Quote ? if the shell expands it.\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--root", help="repo whose .cursor/plugins/warp copy to read")
    import usage

    args = p.parse_args(usage.normalize_argv(argv))
    print(describe(find_root(args.root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
