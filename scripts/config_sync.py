#!/usr/bin/env python3
"""Bring the beam's config up to `.warp/config.yaml` before anything reads it.

Most of Warp reads its settings from the beam (`beam["config"]`), which the scan filled from `.warp/config.yaml`
and the defaults of that day. Before 1.7.2 a later change to `.warp/config.yaml` (say `maxSpawnsPerHour: 40` raised
to 200) never reached the beam, so a restarted run kept the old value. Now:

- `/warp-start` and `/warp-resume` sync first, before the start checks, the prompt gate, and the dispatch, and
  every supervise pass syncs again when `.warp/config.yaml` changed since the last sync.
- Every one-line `key: value` in `.warp/config.yaml` with a value, for a key the beam's config carries (every key
  with a default, and any the scan set), is copied into the beam, typed like the value it replaces (a number stays
  a number, `true`/`false` a boolean). Secrets and machine keys (`siblingRoot`, `worktreeRoot`, …) stay out of the
  beam, which is published. A key whose beam value is a list or a map is left to the code that parses it.
- A key that is not in `.warp/config.yaml` and still holds a default an older Warp wrote (`maxSpawnsPerHour: 40`,
  `maxInProgress: 20`, `pollSeconds: 300`) takes today's default.

Each change prints `config: <key> <old> -> <new> (.warp/config.yaml)` or `(new default)`.

  python3 scripts/config_sync.py --beam .warp/beam.json            sync and save
  python3 scripts/config_sync.py --beam .warp/beam.json --dry-run  list what would change

?, help, -h, and --help print this text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

HELP = __doc__
# Defaults an older Warp wrote into the beam, by key: replaced by today's default when the key is not in config.yaml.
OLD_DEFAULTS = {
    "maxSpawnsPerHour": {40},
    "maxInProgress": {20},
    "pollSeconds": {300},
}


def _typed(raw: str, like):
    text = str(raw).strip()
    low = text.casefold()
    if isinstance(like, bool) or (like is None and low in {"true", "false", "yes", "no", "on", "off"}):
        if low in {"true", "yes", "on", "1"}:
            return True
        if low in {"false", "no", "off", "0"}:
            return False
        return text
    if isinstance(like, int) or (like is None and re.fullmatch(r"-?\d+", text)):
        try:
            return int(text)
        except ValueError:
            return text
    if isinstance(like, float):
        try:
            return float(text)
        except ValueError:
            return text
    if re.fullmatch(r"-?\d+", text) and isinstance(like, str) and like.casefold() == "auto":
        return int(text)
    return text


def _same(a, b) -> bool:
    if a == b:
        return True
    return str(a).strip().casefold() == str(b).strip().casefold()


def sync(data: dict, root: Path) -> list:
    """Mutates data["config"]. Lines for each change."""
    import beam as beam_mod
    import prompt_gate

    cfg = data.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
        data["config"] = cfg
    path = Path(root) / ".warp" / "config.yaml"
    text = path.read_text() if path.is_file() else ""
    written = {k: v for k, v in prompt_gate.yaml_scalars(text).items() if v != ""}
    import runs
    import state_commit

    defaults = beam_mod.default_config()
    lines = []
    for key, raw in sorted(written.items()):
        if key not in cfg and key not in defaults:
            continue
        if key in runs.MACHINE_KEYS or state_commit.secret_key(key):
            continue
        old = cfg.get(key)
        if isinstance(old, (list, dict)):
            continue
        if raw.lstrip().startswith(("[", "{")):
            continue
        new = _typed(raw, old)
        if key in cfg and _same(old, new):
            continue
        cfg[key] = new
        lines.append("config: %s %s -> %s (.warp/config.yaml)" % (key, "unset" if old is None else old, new))
    for key, olds in OLD_DEFAULTS.items():
        if key in written or key not in cfg:
            continue
        try:
            stale = int(cfg[key]) in olds
        except (TypeError, ValueError):
            stale = False
        if stale and key in defaults and not _same(cfg[key], defaults[key]):
            lines.append("config: %s %s -> %s (new default; set it in .warp/config.yaml to keep the old value)" % (key, cfg[key], defaults[key]))
            cfg[key] = defaults[key]
    try:
        data["configSync"] = {"mtime": path.stat().st_mtime if path.is_file() else 0, "at": beam_mod.utcnow()}
    except OSError:
        pass
    return lines


def due(data: dict, root: Path) -> bool:
    """True when `.warp/config.yaml` changed since the last sync (or none ran)."""
    path = Path(root) / ".warp" / "config.yaml"
    seen = (data.get("configSync") or {}).get("mtime") if isinstance(data.get("configSync"), dict) else None
    try:
        now = path.stat().st_mtime if path.is_file() else 0
    except OSError:
        return False
    return seen is None or float(seen) != float(now)


def sync_on_disk(beam_path: Path, only_if_changed: bool = False) -> list:
    """Load, sync, and save the live beam. Never raises."""
    import beam as beam_mod

    path = Path(beam_path)
    try:
        if not path.is_file():
            return []
        data = beam_mod.load_json(path)
        root = path.resolve().parent.parent
        if only_if_changed and not due(data, root):
            return []
        before = json.dumps(data, sort_keys=True)
        lines = sync(data, root)
        if json.dumps(data, sort_keys=True) != before:
            beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
        return lines
    except Exception as exc:
        return ["config: not synced into the beam (%s)" % exc]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Sync .warp/config.yaml into the beam", epilog=HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--beam", default=".warp/beam.json")
    parser.add_argument("--dry-run", action="store_true")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    path = Path(args.beam)
    if not path.is_file():
        print("config: no beam at %s" % path)
        return 2
    if args.dry_run:
        import beam as beam_mod

        lines = sync(beam_mod.load_json(path), path.resolve().parent.parent)
    else:
        lines = sync_on_disk(path)
    for line in lines or ["config: the beam matches .warp/config.yaml"]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
