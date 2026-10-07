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
from typing import Optional

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
INSTALLED_REL = Path(".cursor/plugins/warp")
RECORDED_REL = Path(".warp/version")


def read_version_file(root: Path) -> Optional[str]:
    path = root / "VERSION"
    if not path.is_file():
        return None
    text = path.read_text().strip()
    return text or None


def read_manifest_version(root: Path) -> Optional[str]:
    path = root / ".cursor-plugin" / "plugin.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text()).get("version")
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def version_of(root: Path) -> Optional[str]:
    """VERSION if that file is present, otherwise the manifest."""
    return read_version_file(root) or read_manifest_version(root)


def recorded_version(root: Path) -> Optional[str]:
    path = root / RECORDED_REL
    if not path.is_file():
        return None
    text = path.read_text().strip()
    return text or None


def label(version: Optional[str]) -> str:
    return f"Warp v{version}" if version else "Warp"


def upgrade_line(installed: str, source: str) -> str:
    return f"plugin is v{installed}, repo copy is v{source}: run /warp-upgrade"


# The project copy of the upgrade script. It runs without /warp-upgrade.
UPGRADE_SCRIPT = "python3 .cursor/plugins/warp/scripts/upgrade.py"


def upgrade_fallback() -> str:
    """Script lines for a palette that does not list /warp-upgrade."""
    return f"{UPGRADE_SCRIPT}\n{UPGRADE_SCRIPT} ?"


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
    try:
        import prompt_gate
        import orchestrator

        beam_path = root / ".warp" / "beam.json"
        beam = {}
        if beam_path.is_file():
            try:
                loaded = json.loads(beam_path.read_text())
                beam = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError, UnicodeError):
                beam = {}
        cfg = prompt_gate.effective_config(beam, root)
        note = orchestrator.override_note(cfg)
        if note:
            lines.append(note)
        lines.append(orchestrator.effective_text(cfg))
    except Exception as exc:
        lines.append("effective: unavailable (%s)" % exc)
    lines.append("If /warp-upgrade is not in the command list:")
    lines.append(upgrade_fallback())
    return "\n".join(lines)


def find_root(arg: Optional[str]) -> Path:
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


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Print the Warp plugin version",
        epilog="examples:\n  python3 scripts/version.py ?\n  python3 scripts/version.py --root .\n\n"
        "Prints installed (.cursor/plugins/warp), source (this plugin tree), and recorded (.warp/version).\n"
        "When /warp-upgrade is missing from an older command list, it also prints:\n"
        "  python3 .cursor/plugins/warp/scripts/upgrade.py\n"
        "  python3 .cursor/plugins/warp/scripts/upgrade.py ?\n"
        "That script does not need the slash command. ? prints its flags. Quote ? if the shell expands it.\n"
        "?, help, -h, and --help print this text.\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--root", help="repo whose .cursor/plugins/warp copy to read")
    import usage

    args = p.parse_args(usage.normalize_argv(argv))
    print(describe(find_root(args.root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
