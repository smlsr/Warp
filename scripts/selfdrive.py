#!/usr/bin/env python3
"""Warp as a self-driving supervisor (1.7.15, part 2): the cadences, their config, the lease, and say-it-once.

A human coordinator used to do this by hand on every 5-minute tick: audit agents, push blockers to the front, file
gap tickets, clear alarms, unstick merges, and rebalance work across coordinators. Warp does it now, on four clocks:

  tick    300 s   the merge lane, blockers, holds, main red, hung CI, Bugbot, Jira, herald      (under 90 s)
  sweep   900 s   agent health: duplicates, finished-but-running, stalled, stale waits, orphan PRs
  audit  1800 s   throughput, load and rebalance, coordinators, gap discovery, the report
  deep  21600 s   waiver expiry, stale-ticket cleanup, history and cost

`wake.py run` (the watch timer) runs the supervise pass and then every cadence that is due, from
`.warp/cadence.json`. `wake.py tick|sweep|audit|deep` runs one by hand. A sweep, audit or deep job runs in the
background under a lease when `cadence.background` is on (the default), so it never holds up the 5-minute tick; the
next tick prints what it found.

Principles every cadence keeps:
  - never wait for a person except for a product decision (asks.py); every other stall has a next step
  - every alarm has a remediation that runs; past N ticks with no progress the remediation escalates, not the noise
  - one sole pusher per pull request (`prOwners` on the beam); push only for a real conflict, a real failure, or a
    needed main merge; never rerun old CI; never force-push except an approved, logged one-off (exceptions.json)
  - REST first for GitHub (ghrest.py)
  - batch the fixes: one fix round per head covers every red check and every Bugbot finding, one push
  - every decision goes to the journal and the board, and to Slack once (say_once)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULTS = {
    "cadence": {"tick": 300, "sweep": 900, "audit": 1800, "fullAudit": 3600, "deep": 21600, "background": True,
                "leaseSeconds": 1500},
    "blockers": {"topN": 10},
    "refresh": {"timeout": 30, "budget": 2},
    "ci": {"hungMinutes": 60},
    "sweep": {"activeMinutes": 45, "restaffMinutes": 30},
    "load": {"high": 25, "low": 8, "spawnAt": 30, "unownedSpawnAt": 15, "retireAfter": 2},
    "agents": {"ceiling": 120},
    "gaps": {"autoCreate": True, "autoStaffTopN": 10},
    "humanAsk": ["product-permission", "data-deletion", "public-api", "budget", "unstoppable-agent", "protected-branch"],
    "ladder": {"nudgeAfter": 1, "restaffAfter": 3, "coordinatorAfter": 6, "askAfter": 10},
    "batch": {"fixes": True},
}
CADENCES = ("tick", "sweep", "audit", "fullAudit", "deep")
CADENCE_FILE = "cadence.json"


# ---- config -----------------------------------------------------------------------------------------------------


def _scalar(text: str):
    text = text.strip().strip("\"'")
    low = text.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _flow(text: str):
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        out = {}
        for part in re.split(r",(?![^\[]*\])", text[1:-1]):
            if ":" in part:
                key, value = part.split(":", 1)
                out[key.strip().strip("\"'")] = _flow(value)
        return out
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(item) for item in text[1:-1].split(",") if item.strip()]
    return _scalar(text)


def parse_sections(text: str, names) -> dict:
    """Top-level `name:` keys of a YAML file as dicts or lists: flow style (`cadence: {tick: 300}`) or a block of
    indented `key: value` / `- item` lines. Enough YAML for these keys; nothing else is read here."""
    out: dict = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^([A-Za-z][\w]*):[ \t]*(.*?)[ \t]*(#.*)?$", line)
        if not match or match.group(1) not in names:
            continue
        name, rest = match.group(1), match.group(2)
        if rest:
            out[name] = _flow(rest)
            continue
        block: dict = {}
        items: list = []
        for sub in lines[index + 1:]:
            if not sub.strip() or sub.lstrip().startswith("#"):
                continue
            if not sub.startswith((" ", "\t")):
                break
            body = sub.strip()
            if body.startswith("- "):
                items.append(_scalar(body[2:]))
            elif ":" in body:
                key, value = body.split(":", 1)
                block[key.strip()] = _flow(value.split(" #")[0])
        out[name] = items if items and not block else block
    return out


def config(data: Optional[dict], beam_path: Optional[Path]) -> dict:
    """DEFAULTS, then the beam's config, then `.warp/config.yaml`."""
    merged = json.loads(json.dumps(DEFAULTS))
    sources = []
    if isinstance(data, dict) and isinstance(data.get("config"), dict):
        sources.append(data["config"])
    if beam_path is not None:
        path = Path(beam_path).parent / "config.yaml"
        if path.is_file():
            try:
                sources.append(parse_sections(path.read_text(), set(DEFAULTS)))
            except OSError:
                pass
    for src in sources:
        for key, default in DEFAULTS.items():
            value = src.get(key)
            if value in (None, ""):
                continue
            if isinstance(default, dict) and isinstance(value, dict):
                merged[key].update(value)
            elif isinstance(default, list) and isinstance(value, list):
                merged[key] = list(value)
    return merged


def num(cfg: dict, section: str, key: str) -> float:
    try:
        return float(cfg.get(section, {}).get(key, DEFAULTS[section][key]))
    except (TypeError, ValueError, KeyError):
        return float(DEFAULTS[section][key])


# ---- cadences and the lease -------------------------------------------------------------------------------------


def _cadence_path(beam_path: Path) -> Path:
    return Path(beam_path).parent / CADENCE_FILE


def load_cadence(beam_path: Path) -> dict:
    try:
        doc = json.loads(_cadence_path(beam_path).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cadence(beam_path: Path, doc: dict) -> None:
    try:
        _cadence_path(beam_path).write_text(json.dumps(doc, indent=2) + "\n")
    except OSError:
        pass


def due(beam_path: Path, cfg: dict, now: float) -> list:
    """Cadences whose interval has passed since they last finished (or never ran), and whose lease is free."""
    doc = load_cadence(beam_path)
    out = []
    for name in CADENCES:
        every = num(cfg, "cadence", name)
        if every <= 0:
            continue
        row = doc.get(name) if isinstance(doc.get(name), dict) else {}
        if lease_held(row, now):
            continue
        last = float(row.get("finishedAt") or row.get("startedAt") or 0)
        if now - last >= every - 5:
            out.append(name)
    return out


def lease_held(row: dict, now: float) -> bool:
    lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
    return bool(lease) and float(lease.get("until") or 0) > now


def take_lease(beam_path: Path, name: str, cfg: dict, now: float, holder: str) -> bool:
    doc = load_cadence(beam_path)
    row = doc.get(name) if isinstance(doc.get(name), dict) else {}
    if lease_held(row, now) and row["lease"].get("holder") != holder:
        return False
    row["lease"] = {"holder": holder, "since": now, "until": now + num(cfg, "cadence", "leaseSeconds")}
    row["startedAt"] = now
    doc[name] = row
    save_cadence(beam_path, doc)
    return True


def finish(beam_path: Path, name: str, now: float, lines: list, took: float) -> None:
    doc = load_cadence(beam_path)
    row = doc.get(name) if isinstance(doc.get(name), dict) else {}
    row.pop("lease", None)
    row.update(finishedAt=now, seconds=round(took, 1), runs=int(row.get("runs") or 0) + 1)
    # A background job's lines are printed by the next tick, once.
    row["pending"] = list(lines)[-200:]
    doc[name] = row
    save_cadence(beam_path, doc)


def take_pending(beam_path: Path) -> list:
    """Lines a background sweep, audit or deep job left for the parent. Printed once."""
    doc = load_cadence(beam_path)
    out = []
    changed = False
    for name in CADENCES:
        row = doc.get(name) if isinstance(doc.get(name), dict) else {}
        if row.get("pending") and not lease_held(row, time.time()):
            out.extend(row.pop("pending"))
            changed = True
    if changed:
        save_cadence(beam_path, doc)
    return out


# ---- say once ------------------------------------------------------------------------------------------------------


def say_once(data: dict, key: str, line: str, now_s: str = "") -> list:
    """`line` the first time `key` is seen with this text; [] after. Kept on the beam under `said` (last 2000)."""
    said = data.setdefault("said", {})
    if not isinstance(said, dict):
        said = data["said"] = {}
    if said.get(key) == line:
        return []
    said[key] = line
    if len(said) > 2000:
        for old in list(said)[: len(said) - 2000]:
            said.pop(old, None)
    return [line]


def journal(beam_path: Path, kind: str, **fields) -> None:
    try:
        import beam as beam_mod

        beam_mod.journal(Path(beam_path), dict({"type": kind}, **fields))
    except Exception:
        pass


def report_path(beam_path: Path, folder: str, now_s: str) -> Path:
    stamp = re.sub(r"[^0-9T]", "", now_s)[:15] or str(int(time.time()))
    out = Path(beam_path).parent / folder / ("%s.md" % stamp)
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def scripts_dir() -> str:
    return str(Path(__file__).resolve().parent)


def background(beam_path: Path, name: str) -> bool:
    """Start `wake.py <name> --beam <beam> --lease bg` detached. True when it started."""
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "wake.py"),
                          "full-audit" if name == "fullAudit" else name, "--beam",
                          str(Path(beam_path).resolve()), "--lease", "background"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(Path(beam_path).resolve().parent.parent))
        return True
    except OSError:
        return False


def in_test() -> bool:
    return bool(os.environ.get("WARP_NO_BACKGROUND"))
