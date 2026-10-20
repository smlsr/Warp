#!/usr/bin/env python3
"""Is the base branch green? Every `baseCheckSeconds` (default 300; 0 is off) a pass reads the checks on the head of
the base branch (`main`) on GitHub and records them on the beam (1.7.9).

Before 1.7.9 nothing set `baseGreen` or `baseCi`, so a red main was never seen: every pull request failing the
checks main itself fails looked "red, back to fix", spent a fix attempt, and came back red.

  red      `base: main is red at <sha> (<checks>)`, one Slack alarm per red commit, `baseGreen: false` (the fixer then
           starts the base fix, `dispatch-base-fix`), and a pull request whose only red is the checks main fails
           waits for main instead of going to fix (`ci: <id> fails the same checks as main …`).
  green    `base: main is green again at <sha>` after a red, one Slack line; the base-fix count starts again.
  pending  nothing changes; a red main stays red until it is green.

  python3 scripts/basecheck.py --beam .warp/beam.json      read it now and print the result

?, help, -h, and --help print this text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

HELP = __doc__
DEFAULT_SECONDS = 300
ALARM_KEY = "base:base-red"


def interval(data: dict, beam_path: Optional[Path]) -> int:
    import beam as beam_mod

    try:
        seconds = int(beam_mod.config_int(data, beam_path, "baseCheckSeconds", DEFAULT_SECONDS))
    except Exception:
        seconds = DEFAULT_SECONDS
    return max(60, seconds) if seconds > 0 else 0


def _base(data: dict, root: Path) -> str:
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    if cfg.get("baseBranch"):
        return str(cfg["baseBranch"])
    try:
        import provider as provider_mod

        return str(provider_mod.resolve(root, cfg).get("baseBranch") or "main")
    except Exception:
        return "main"


def due(data: dict, beam_path: Optional[Path], now=None) -> bool:
    import beam as beam_mod

    every = interval(data, beam_path)
    if not every:
        return False
    record = data.get("baseCheck") if isinstance(data.get("baseCheck"), dict) else {}
    last = beam_mod.parse_ts(str(record.get("at") or ""))
    if last is None:
        return True
    return (beam_mod.coerce_now(now)[0] - last).total_seconds() >= every - min(30, every // 10)


def apply(data: dict, found: Optional[dict], now_s: str, base: str = "main") -> list:
    """Record what the base branch's checks say and return the lines. The caller writes the beam."""
    record = data.get("baseCheck") if isinstance(data.get("baseCheck"), dict) else {}
    record["at"] = now_s
    data["baseCheck"] = record
    if not found:
        record["state"] = "unknown"
        return []
    sha = str(found.get("sha") or "")
    state = str(found.get("state") or "")
    failed = [str(name) for name in found.get("failed") or []]
    was_red = data.get("baseGreen") is False
    record.update({"sha": sha, "state": state, "failed": failed, "base": base})
    data["baseCi"] = state
    lines: list = []
    posted = data.setdefault("postedAlarms", [])
    if state == "red":
        data["baseGreen"] = False
        record.setdefault("redSince", now_s)
        names = ", ".join(failed[:6]) or "a check"
        lines.append("base: %s is red at %s (%s)" % (base, sha[:12], names))
        if record.get("alarmedSha") != sha:
            record["alarmedSha"] = sha
            lines.append("slack: alarm base base-red")
            lines.append("herald: ALARM %s is red at %s: %s failing. Pull requests that fail the same checks are failing on "
                         "%s, not on their own change: they wait for %s instead of going to fix, and Warp starts a fix for %s."
                         % (base, sha[:12], names, base, base, base))
        return lines
    if state == "green":
        data["baseGreen"] = True
        if was_red:
            since = str(record.pop("redSince", "") or "")
            record.pop("alarmedSha", None)
            watch = data.get("watch") if isinstance(data.get("watch"), dict) else None
            if watch is not None:
                watch.pop("baseFixes", None)
                watch.pop("baseAnnouncedAt", None)
            if ALARM_KEY in posted:
                posted.remove(ALARM_KEY)
            lines.append("base: %s is green again at %s%s" % (base, sha[:12], " (red since %sZ)" % since[11:19] if since else ""))
            lines.append("herald: %s is green again at %s. Pull requests that waited on it go on." % (base, sha[:12]))
        else:
            record.pop("redSince", None)
        return lines
    # pending or absent: a red base stays red until it is green; otherwise nothing changes.
    return lines


def observe(beam_path: Path, now=None, reader: Optional[Callable] = None, force: bool = False) -> list:
    """Read the base branch's checks when due and record them on the live beam. Never raises."""
    import beam as beam_mod

    path = Path(beam_path)
    try:
        data = beam_mod.load_json(path)
    except (OSError, ValueError) as exc:
        return ["base: not read (%s)" % exc]
    if not force and not due(data, path, now):
        return []
    if (data.get("runState") or "running") != "running" or data.get("paused"):
        return []
    import runs

    root = runs.repo_root(path)
    base = _base(data, root)
    try:
        if reader is not None:
            found = reader(root, base)
        else:
            import provider as provider_mod

            found = provider_mod.base_checks(root, base)
    except Exception as exc:
        found = None
        out = ["base: %s checks not read (%s)" % (base, exc)]
    else:
        out = []
    now_s = beam_mod.coerce_now(now)[1]
    lines = apply(data, found, now_s, base)
    beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
    return out + lines


def same_as_base(data: dict, ticket: dict) -> list:
    """The pull request's failing checks when every one of them also fails on the red base, else []."""
    if data.get("baseGreen") is not False:
        return []
    record = data.get("baseCheck") if isinstance(data.get("baseCheck"), dict) else {}
    base_failed = {str(name).casefold() for name in record.get("failed") or []}
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    head = str(pr.get("headSha") or "")
    if str(pr.get("failedChecksSha") or "") != head:
        return []
    mine = [str(name) for name in pr.get("failedChecks") or []]
    if not mine or not base_failed:
        return []
    return mine if all(name.casefold() in base_failed for name in mine) else []


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Read the base branch's checks", epilog=HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--beam", default=".warp/beam.json")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    import runs as _runs

    _runs.use_live_beam(args)
    path = Path(args.beam)
    if not path.is_file():
        print("base: no beam at %s" % path)
        return 2
    lines = observe(path, force=True)
    data = json.loads(path.read_text())
    record = data.get("baseCheck") or {}
    for line in lines or ["base: %s is %s at %s" % (record.get("base") or "main", record.get("state") or "unknown",
                                                    str(record.get("sha") or "")[:12])]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
