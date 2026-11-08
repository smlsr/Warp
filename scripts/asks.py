#!/usr/bin/env python3
"""When Warp asks a person, and the remediation ladder for everything else (1.7.15).

Only these stop for a person (`humanAsk` in config):
  product-permission   who may do X (removing atlas.manage from members, …)
  data-deletion        deleting data, or a destructive migration
  public-api           renaming or removing a public API
  budget               a change to spend or budget
  unstoppable-agent    an agent nobody can stop
  protected-branch     a merge into a protected branch outside policy

Each is one question, posted once, with options and a recommended default; everything else keeps moving while it
waits. `warp: decide <ask-id> <option>` (or `wake.py decide`) answers it, and the answer is kept as a durable rule in
`.warp/decisions.md`, so the same question is never asked twice.

Every other stall climbs the ladder instead of repeating an alarm:
  nudge -> restaff -> new coordinator -> a human question with a default
`ladder.*After` (ticks with no progress) sets the rungs.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

DECISIONS = "decisions.md"
ASKS = "asks.json"
RUNGS = ("nudge", "restaff", "coordinator", "ask")
_KIND_WORDS = {
    "product-permission": ("permission", "role", "who may", "access", "manage", "rbac", "scope"),
    "data-deletion": ("delete data", "drop table", "destructive", "truncate", "purge", "drop column"),
    "public-api": ("public api", "rename endpoint", "remove endpoint", "breaking change", "deprecate"),
    "budget": ("budget", "spend", "cost", "billing", "quota increase"),
    "unstoppable-agent": ("cannot stop", "unstoppable", "needs-human-stop"),
    "protected-branch": ("protected branch", "outside policy", "bypass protection"),
}


def kind_of(text: str) -> str:
    low = str(text or "").lower()
    for kind, words in _KIND_WORDS.items():
        if any(word in low for word in words):
            return kind
    return ""


def _load(beam_path: Path) -> dict:
    try:
        doc = json.loads((Path(beam_path).parent / ASKS).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(beam_path: Path, doc: dict) -> None:
    (Path(beam_path).parent / ASKS).write_text(json.dumps(doc, indent=2) + "\n")


def decided(beam_path: Path, question: str) -> str:
    """The recorded answer for this question (same normalized text), or ""."""
    path = Path(beam_path).parent / DECISIONS
    if not path.is_file():
        return ""
    want = _norm(question)
    for line in path.read_text().splitlines():
        match = re.match(r"^- \[(?P<id>[^\]]+)\] (?P<q>.+?) => (?P<a>.+?)( \(.*\))?$", line)
        if match and _norm(match.group("q")) == want:
            return match.group("a")
    return ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def ask(beam_path: Path, kind: str, question: str, options: list, default: str, blocking: Optional[list] = None,
        cfg: Optional[dict] = None) -> list:
    """One question, once. Returns the herald and ask lines the first time, [] after. A category not in humanAsk is
    not asked: its default is applied and recorded (nothing waits on a person)."""
    import beam as beam_mod
    import selfdrive

    beam_path = Path(beam_path)
    cfg = cfg or selfdrive.config(None, beam_path)
    answer = decided(beam_path, question)
    if answer:
        return ["ask: already decided: %s => %s" % (question[:100], answer)]
    allowed = set(cfg.get("humanAsk") or [])
    if kind not in allowed:
        record(beam_path, "auto", question, default, by="warp (default; %s is not a human ask)" % (kind or "this"))
        return ["ask: %s is not a human decision here; applied the default %r and recorded it" % (question[:100], default)]
    aid = "ASK-" + hashlib.sha1(_norm(question).encode()).hexdigest()[:6].upper()
    doc = _load(beam_path)
    rows = doc.setdefault("open", {})
    if aid in rows:
        return []
    rows[aid] = {"kind": kind, "question": question, "options": options, "default": default,
                 "blocking": list(blocking or []), "askedAt": beam_mod.utcnow()}
    _save(beam_path, doc)
    shown = " | ".join("%s%s" % (opt, " (recommended)" if opt == default else "") for opt in options)
    return [
        "ask: %s %s: %s  Options: %s. Reply `warp: decide %s <option>`. Only %s waits; everything else keeps moving."
        % (aid, kind, question, shown, aid, ", ".join(blocking or []) or "this decision"),
        "herald: Decision needed (%s): %s Options: %s. Default if you agree: %s. Reply warp: decide %s <option>."
        % (kind, question, shown, default, aid),
    ]


def record(beam_path: Path, aid: str, question: str, answer: str, by: str = "") -> None:
    import beam as beam_mod

    path = Path(beam_path).parent / DECISIONS
    if not path.is_file():
        path.write_text("# Decisions\n\nDurable rules Warp follows. One line each: question => answer.\n\n")
    with path.open("a") as handle:
        handle.write("- [%s] %s => %s (%s%s)\n" % (aid, question.replace("\n", " "), answer, beam_mod.utcnow(),
                                                  (", " + by) if by else ""))


def decide(beam_path: Path, aid: str, option: str, by: str = "") -> list:
    doc = _load(beam_path)
    row = (doc.get("open") or {}).pop(aid, None)
    if not row:
        return ["ask: no open question %s" % aid]
    choice = option if option else row.get("default")
    if row.get("options") and choice not in row["options"] and choice != "default":
        return ["ask: %s takes one of %s" % (aid, ", ".join(row["options"]))]
    if choice == "default":
        choice = row.get("default")
    record(beam_path, aid, row["question"], choice, by=by)
    doc.setdefault("closed", {})[aid] = dict(row, answer=choice)
    _save(beam_path, doc)
    return ["ask: %s decided: %s (recorded in .warp/%s)%s" % (aid, choice, DECISIONS,
                                                             ("; %s may continue" % ", ".join(row["blocking"])) if row.get("blocking") else "")]


def rung(cfg: dict, ticks: int) -> str:
    """Which rung of the ladder a stall is on after `ticks` ticks with no progress ("" before the first)."""
    import selfdrive

    steps = [("ask", selfdrive.num(cfg, "ladder", "askAfter")),
             ("coordinator", selfdrive.num(cfg, "ladder", "coordinatorAfter")),
             ("restaff", selfdrive.num(cfg, "ladder", "restaffAfter")),
             ("nudge", selfdrive.num(cfg, "ladder", "nudgeAfter"))]
    for name, after in steps:
        if ticks >= after:
            return name
    return ""


def climb(data: dict, key: str, progressed: bool, cfg: dict) -> str:
    """Count ticks with no progress for `key` (a ticket, a pull request) and return the rung to act on, once per rung."""
    ladder = data.setdefault("ladder", {})
    row = ladder.get(key) if isinstance(ladder.get(key), dict) else {"ticks": 0, "done": []}
    if progressed:
        ladder.pop(key, None)
        return ""
    row["ticks"] = int(row.get("ticks") or 0) + 1
    step = rung(cfg, row["ticks"])
    ladder[key] = row
    if step and step not in row.get("done", []):
        row.setdefault("done", []).append(step)
        return step
    return ""
