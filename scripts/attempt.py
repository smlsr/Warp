#!/usr/bin/env python3
"""Never fail without trying the other ways (1.7.17).

Every GitHub write Warp makes goes down the same ladder. The first rung that works wins:

  1. REST with the environment's credentials (GITHUB_TOKEN when set, else gh's login)
  2. the gh CLI command for the same thing
  3. the parent's Cursor GitHub tools: a `<kind>: MUST DO …` line with the GitHub MCP tool and arguments
  4. the next tick checks whether it happened. If it has not, the pull request's coordinator (or owner) is told to do
     it (relay.py, a follow-up run on its agent), once

  merge      PUT /pulls/N/merge sha=<head>   gh pr merge N --squash --match-head-commit <head>   merge_pull_request
  retarget   PATCH /pulls/N base=main        gh pr edit N --base main                             update_pull_request
  comment    POST /issues/N/comments         gh pr comment N --body …                             add_issue_comment
  cancel     POST /actions/runs/R/cancel     gh run cancel R                                      (no MCP tool: coordinator)

A definite refusal (the head moved past the sha guard, not mergeable) is an answer, not a failure: it is not retried.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

FILE = "handoffs.json"


def chain(steps: list) -> tuple:
    """steps: [(name, fn)] where fn() -> (ok, detail, final). Returns (ok, how, detail, tried). `final` stops the
    ladder (a definite answer such as a moved head)."""
    tried = []
    for name, fn in steps:
        try:
            ok, detail, final = fn()
        except Exception as exc:
            ok, detail, final = False, str(exc), False
        if ok:
            return True, name, detail, tried
        tried.append("%s: %s" % (name, str(detail or "refused").splitlines()[-1][:160] if detail else "refused"))
        if final:
            return False, name, detail, tried
    return False, "", "; ".join(tried), tried


def _load(beam_path: Path) -> list:
    try:
        rows = json.loads((Path(beam_path).parent / FILE).read_text())
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _save(beam_path: Path, rows: list) -> None:
    (Path(beam_path).parent / FILE).write_text(json.dumps(rows[-300:], indent=2) + "\n")


MCP = {
    "merge": "the GitHub MCP merge_pull_request (pull_number {pr}, merge_method {method}, sha {sha})",
    "retarget": "the GitHub MCP update_pull_request (pull_number {pr}, base {base})",
    "comment": "the GitHub MCP add_issue_comment (issue_number {pr}, body {body!r})",
    "cancel": "`gh run cancel {run}` from your terminal (no MCP tool cancels a run)",
}
ASK = {
    "merge": "Merge #{pr} at {sha12} (squash): Warp could not ({why}).",
    "retarget": "Retarget #{pr} to {base}: Warp could not ({why}).",
    "comment": "Post `{body}` on #{pr}: Warp could not ({why}).",
    "cancel": "Cancel the hung CI run {run} on #{pr} once (do not rerun it): Warp could not ({why}).",
}


def handoff(beam_path: Path, kind: str, pr: str, why: str, owner: str = "", coordinator: str = "", agent: str = "",
            now_s: str = "", **fields) -> list:
    """Rung 3: the parent's Cursor GitHub tools, and remember it for rung 4."""
    rows = [r for r in _load(beam_path) if not (r.get("kind") == kind and r.get("pr") == str(pr))]
    row = dict(fields, kind=kind, pr=str(pr), why=why, owner=owner, coordinator=coordinator, agent=agent, at=now_s,
               stage="parent")
    rows.append(row)
    _save(beam_path, rows)
    sha = str(fields.get("sha") or "")
    shown = MCP[kind].format(pr=pr, method=fields.get("method") or "squash", sha=sha, base=fields.get("base") or "main",
                             body=fields.get("body") or "", run=fields.get("run") or "")
    return ["%s: MUST DO #%s with %s. Warp tried REST and gh (%s). The next tick checks it; if it did not happen, %s is "
            "told to do it." % (kind, pr, shown, why, coordinator or owner or "its owner")]


def check(beam_path: Path, done: Callable, send: Callable, now=None) -> list:
    """Rung 4. done(row) -> True when it happened. Not done after the parent's turn: the coordinator is told, once;
    after that it waits up to two hours, then the entry is dropped with one line."""
    import beam as beam_mod

    rows = _load(beam_path)
    if not rows:
        return []
    now_dt, now_s = beam_mod.coerce_now(now)
    keep, out = [], []
    for row in rows:
        try:
            happened = bool(done(row))
        except Exception:
            happened = False
        if happened:
            out.append("%s: #%s done%s" % (row["kind"], row["pr"], " by its coordinator" if row.get("stage") == "coordinator"
                                           else " by the parent"))
            continue
        if row.get("stage") == "parent":
            text = ASK[row["kind"]].format(pr=row["pr"], sha12=str(row.get("sha") or "")[:12], base=row.get("base") or "main",
                                           body=row.get("body") or "", run=row.get("run") or "", why=row.get("why") or "")
            out.extend(send(row.get("coordinator") or row.get("owner") or "warp",
                            text + " You have the permissions for it; do it now.", row.get("agent") or ""))
            row["stage"] = "coordinator"
            row["toldAt"] = now_s
            keep.append(row)
            continue
        told = beam_mod.parse_ts(str(row.get("toldAt") or ""))
        if told is not None and (now_dt - told).total_seconds() >= 7200:
            out.append("%s: #%s still not done two hours after its coordinator was told; dropped. %s" % (
                row["kind"], row["pr"], "The lane verifies it again from a new READY." if row["kind"] == "merge" else ""))
            continue
        keep.append(row)
    _save(beam_path, keep)
    return out
