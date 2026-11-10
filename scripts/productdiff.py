#!/usr/bin/env python3
"""Did this Shuttle change the product? (1.7.21)

Seen on 2026-11-10: replacement Shuttles on an open pull request (G-INSTALL-GCP-BUILD-MANIFEST, #1537) wrote
started, coding, pr-opened and done in the same second, committed only `.warp/runs/core/tickets/<id>/state.json` and
`log.jsonl`, and returned `result: <id> ok`. Warp recorded success, the pull request stayed OPEN and UNSTABLE, and
nothing drove it toward merge.

A step that is meant to change code (implement, fix, restart, repair, rebase) is done only when:
  - the branch changes at least one file outside `.warp/` against its base (`origin/<base>`), and
  - this turn changed at least one file outside `.warp/` since the turn's own start (the head recorded with its
    `started` state), when that start is known
Review and verify steps change no code and are exempt; so is a turn that recorded a review or acceptance results.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

CODE_STEPS = {"implement", "fix", "restart", "repair", "rebase", ""}
NO_CODE_STEPS = {"review", "verify", "aireview"}  # aireview records a review; a clean head needs no commit


def _git(root: Path, *args: str, run: Optional[Callable] = None):
    if run is not None:
        return run(["git", *args], Path(root))
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def product_paths(names: list) -> list:
    return [n for n in names if n and not n.startswith(".warp/") and n != ".warp"]


def changed(root: Path, old: str, new: str = "HEAD", run: Optional[Callable] = None) -> Optional[list]:
    """Files outside .warp/ that differ between old and new (three-dot against a branch base). None when git cannot
    tell (no such ref)."""
    done = _git(root, "diff", "--name-only", "%s...%s" % (old, new) if not _is_sha(old) else "%s..%s" % (old, new), run=run)
    if getattr(done, "returncode", 1) != 0:
        return None
    return product_paths([line.strip() for line in (getattr(done, "stdout", "") or "").splitlines()])


def _is_sha(ref: str) -> bool:
    text = str(ref or "")
    return 7 <= len(text) <= 40 and all(c in "0123456789abcdef" for c in text)


def head(root: Path, run: Optional[Callable] = None) -> str:
    done = _git(root, "rev-parse", "HEAD", run=run)
    return (getattr(done, "stdout", "") or "").strip() if getattr(done, "returncode", 1) == 0 else ""


def verdict(root: Path, base: str, turn_base: str = "", step: str = "", reviewed: bool = False,
            run: Optional[Callable] = None) -> str:
    """"" when done may stand; else why not ("no product commit ...")."""
    if step in NO_CODE_STEPS or reviewed:
        return ""
    on_branch = changed(root, "origin/%s" % base, run=run)
    if on_branch is None:
        _git(root, "fetch", "-q", "origin", base, run=run)
        on_branch = changed(root, "origin/%s" % base, run=run)
    if on_branch is not None and not on_branch:
        return "no product commit: the branch changes nothing outside .warp/ against origin/%s" % base
    if turn_base:
        this_turn = changed(root, turn_base, run=run)
        if this_turn is not None and not this_turn:
            return "no product commit this turn: nothing outside .warp/ changed since %s" % turn_base[:12]
    return ""


SPRINT_SECONDS = 60


def turn_of(rows: list) -> list:
    """The log rows of the last turn: from its last `started` on."""
    out = []
    for row in rows or []:
        if isinstance(row, dict) and row.get("state") == "started":
            out = []
        if isinstance(row, dict):
            out.append(row)
    return out


def turn_seconds(rows: list) -> Optional[float]:
    import beam as beam_mod

    turn = turn_of(rows)
    stamps = [beam_mod.parse_ts(str(r.get("at") or "")) for r in turn]
    stamps = [s for s in stamps if s is not None]
    if len(stamps) < 2:
        return None
    return (max(stamps) - min(stamps)).total_seconds()


def branch_verdict(root: Path, branch: str, base: str, state: Optional[dict] = None, rows: Optional[list] = None,
                   step: str = "", run: Optional[Callable] = None) -> str:
    """For the parent, after a Shuttle says it is done: "" when its branch shows product work, else why not.
      - the branch changes nothing outside .warp/ against origin/<base>
      - or the turn lasted under a minute (started to done) and changed nothing outside .warp/ since its turnBase
    Review and verify turns, and turns that recorded a review or acceptance results, are exempt."""
    step = step or str((state or {}).get("turnStep") or "")
    turn = turn_of(rows or [])
    if step in NO_CODE_STEPS or any(r.get("state") in {"review-pass", "review-fail", "acceptance"} or r.get("acs") for r in turn):
        return ""
    _git(root, "fetch", "-q", "origin", base, branch, run=run)
    on_branch = changed(root, "origin/%s" % base, "origin/%s" % branch, run=run)
    if on_branch is not None and not on_branch:
        return "its branch %s changes nothing outside .warp/ against %s" % (branch, base)
    seconds = turn_seconds(rows or [])
    turn_base = str((state or {}).get("turnBase") or "")
    if seconds is not None and seconds < SPRINT_SECONDS and turn_base:
        this_turn = changed(root, turn_base, "origin/%s" % branch, run=run)
        if this_turn is not None and not this_turn:
            return "its %ds turn changed nothing outside .warp/ (started, then done, with no product commit)" % seconds
    if seconds is not None and seconds < SPRINT_SECONDS and not turn_base and \
            [r.get("state") for r in turn][-4:] == ["started", "coding", "pr-opened", "done"]:
        return "its %ds turn wrote started, coding, pr-opened and done with no work between them" % seconds
    return ""
