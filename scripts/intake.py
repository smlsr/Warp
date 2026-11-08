#!/usr/bin/env python3
"""READY and GAPPR intake (1.7.15): pull requests other coordinators say are ready, verified before they are queued.

Lines, from ticket logs (`.warp/tickets/*/`) and coordinator outbox files (`.warp/outbox/*.md`,
`.warp/coordinators/*/outbox*.md`, and `intake.paths` in config):

  READY <pr> <sha> waive=<test>[,<test>] bugbot=<pass|clean|n/a>
  GAPPR <pr> <sha> [gap=<G-…>] [waive=…]

Each is verified over REST before it is queued:
  - the head is exactly <sha>
  - required checks are green, or each red one is waived by an active waiver that covers this head (waivers.gate)
  - Bugbot finished on that exact head with 0 open findings
  - mergeable is clean
A line that fails says why once, and is looked at again when the head changes. Verified entries are queued in
`.warp/lane.json`, ordered by blocker score; the lane merges them (lane.py).
"""

from __future__ import annotations

import glob
import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Optional

LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:\S+\s+)?(READY|GAPPR)\s+(#?\d+|https://github\.com/\S+/pull/\d+)\s+([0-9a-f]{7,40})\b(.*)$")
DEFAULT_PATHS = (".warp/tickets/*/*.log", ".warp/tickets/*/*.md", ".warp/tickets/*/*.jsonl", ".warp/outbox/*.md",
                 ".warp/coordinators/*/outbox*.md", ".warp/outbox.md")
LANE = "lane.json"


def parse(text: str, source: str = "") -> list:
    out = []
    for line in str(text or "").splitlines():
        match = LINE.match(line)
        if not match:
            continue
        kind, pr, sha, rest = match.groups()
        fields = dict(re.findall(r"(\w+)=(\S+)", rest))
        out.append({"kind": kind, "pr": pr.lstrip("#") if pr.startswith("#") or pr.isdigit() else pr, "sha": sha,
                    "waive": [w for w in fields.get("waive", "").split(",") if w and w not in {"none", "-"}],
                    "bugbot": fields.get("bugbot", ""), "gap": fields.get("gap", ""), "source": source,
                    "line": line.strip()})
    return out


def scan(root: Path, paths=None) -> list:
    found = []
    for pattern in paths or DEFAULT_PATHS:
        for name in sorted(glob.glob(str(Path(root) / pattern))):
            try:
                text = Path(name).read_text(errors="replace")
            except OSError:
                continue
            found.extend(parse(text, source=str(Path(name).relative_to(root))))
    return found


def load_lane(beam_path: Path) -> dict:
    try:
        doc = json.loads((Path(beam_path).parent / LANE).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_lane(beam_path: Path, doc: dict) -> None:
    (Path(beam_path).parent / LANE).write_text(json.dumps(doc, indent=2) + "\n")


def _key(entry: dict) -> str:
    return "%s@%s" % (entry["pr"], entry["sha"][:12])


def verify(root: Path, entry: dict, data: dict, doc_reader: Callable, findings_reader: Optional[Callable] = None,
           contains: Optional[Callable] = None) -> tuple:
    """(ok, why, pull request doc). doc_reader(pr) gives the REST doc (ghrest.pr_doc)."""
    import provider
    import waivers

    doc = doc_reader(entry["pr"])
    if not doc:
        return False, "the pull request could not be read", None
    if doc.get("state") != "OPEN":
        return False, "the pull request is %s" % str(doc.get("state") or "?").lower(), doc
    head = str(doc.get("headRefOid") or "")
    if not head.startswith(entry["sha"]) and not entry["sha"].startswith(head[: len(entry["sha"])]):
        return False, "the head is %s, not %s (re-send READY for the new head)" % (head[:12], entry["sha"][:12]), doc
    checks = doc.get("statusCheckRollup") or []
    ci_rows = [c for c in checks if not provider._bugbot_check(c)]
    state = provider.interpret_rollup(ci_rows) if ci_rows else "absent"
    failed = provider.failed_check_names(ci_rows)
    if state in {"pending", "running"}:
        return False, "CI is still running on %s" % head[:12], doc
    if failed:
        ok, why = waivers.gate(data, head, failed, contains=contains, merge_ref=str(doc.get("testMergeSha") or ""))
        if not ok:
            return False, why, doc
    bugbot = provider._bugbot_from_checks(checks)
    if bugbot in {"pending", "running"}:
        return False, "Bugbot has not finished on %s" % head[:12], doc
    if findings_reader is not None and bugbot != "absent":
        found = findings_reader(doc.get("url") or entry["pr"], head)
        if found:
            return False, "Bugbot has %d open finding%s on %s" % (len(found), "" if len(found) == 1 else "s", head[:12]), doc
    elif bugbot == "fail":
        return False, "Bugbot reported findings on %s" % head[:12], doc
    merge_state = str(doc.get("mergeStateStatus") or "").upper()
    if merge_state in {"DIRTY", "CONFLICTING"} or str(doc.get("mergeable") or "").upper() == "CONFLICTING":
        return False, "it conflicts with its base (dirty)", doc
    if merge_state not in {"CLEAN", "HAS_HOOKS", "UNSTABLE", "UNKNOWN", ""} and merge_state != "BEHIND":
        return False, "mergeable is %s" % merge_state.lower(), doc
    return True, "", doc


def intake(beam_path: Path, data: dict, doc_reader: Callable, findings_reader: Optional[Callable] = None,
           contains: Optional[Callable] = None, now=None, paths=None) -> list:
    """Scan, verify, queue. Lines: what was queued, what was refused and why (once per head)."""
    import beam as beam_mod
    import blockgraph
    import selfdrive

    root = Path(beam_path).resolve().parent.parent
    lane = load_lane(beam_path)
    queue = lane.setdefault("queue", {})
    seen = lane.setdefault("seen", {})
    out = []
    now_s = beam_mod.coerce_now(now)[1]
    scores = blockgraph.scores(data, now=now)
    by_pr = {}
    for tid, t in (data.get("tickets") or {}).items():
        number = re.search(r"/pull/(\d+)", str(((t or {}).get("pr") or {}).get("url") or "")) if isinstance(t, dict) else None
        if number:
            by_pr[number.group(1)] = tid
    for entry in scan(root, paths):
        key = _key(entry)
        if key in queue or seen.get(key) in {"merged", "refused-final"}:
            continue
        ok, why, doc = verify(root, entry, data, doc_reader, findings_reader, contains)
        number = re.sub(r"\D", "", str((doc or {}).get("number") or entry["pr"].rsplit("/", 1)[-1]))
        if not ok:
            if seen.get(key) != why:
                seen[key] = why
                out.extend(selfdrive.say_once(data, "intake:" + key, "intake: %s %s %s not queued: %s" % (
                    entry["kind"], entry["pr"], entry["sha"][:12], why)))
            continue
        tid = by_pr.get(number, "")
        queue[key] = {"pr": number, "url": (doc or {}).get("url") or entry["pr"], "sha": str((doc or {}).get("headRefOid") or entry["sha"]),
                      "kind": entry["kind"], "waive": entry["waive"], "gap": entry["gap"], "ticket": tid,
                      "base": (doc or {}).get("baseRefName") or "", "branch": (doc or {}).get("headRefName") or "",
                      "score": scores.get(tid, 0) if tid else scores.get("#" + number, 0), "queuedAt": now_s,
                      "source": entry["source"]}
        seen[key] = "queued"
        out.append("intake: %s #%s %s verified (head, checks%s, Bugbot, mergeable); queued for the lane" % (
            entry["kind"], number, entry["sha"][:12], " with waivers" if entry["waive"] else ""))
    save_lane(beam_path, lane)
    return out


def ordered(beam_path: Path) -> list:
    rows = list((load_lane(beam_path).get("queue") or {}).items())
    rows.sort(key=lambda kv: (-float(kv[1].get("score") or 0), kv[1].get("queuedAt") or ""))
    return rows


def digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]
