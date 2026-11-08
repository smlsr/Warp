#!/usr/bin/env python3
"""How Warp pushes (1.7.16). Each step is tried in order; the first that works wins:

  1. GITHUB_TOKEN (or GH_TOKEN), when the environment has that secret:
     git push https://x-access-token:<token>@github.com/<owner>/<repo>.git <sha>:refs/heads/<branch>
  2. the checkout's own credentials: git push origin <sha>:refs/heads/<branch> (gh's credential helper, an SSH key, …)
  3. the parent's Cursor GitHub tools: `push: MUST DO <branch> …` with the exact command and the GitHub MCP fallback.
     The commit is kept under refs/warp/pending/<branch> so it is not lost meanwhile
  4. the next tick reads the branch over REST. Not at <sha> yet: the pull request's coordinator (or owner) is told to
     redo the change itself and push it once (relay.py, a follow-up run on its agent)

A token is never printed: every line and journal entry has it redacted.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

PENDING = "pushes.json"


def token(env: Optional[dict] = None) -> str:
    env = env if env is not None else os.environ
    return str(env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "")


def redact(text: str, secret: str = "") -> str:
    out = str(text or "")
    if secret:
        out = out.replace(secret, "***")
    return re.sub(r"(x-access-token:)[^@\s]+@", r"\1***@", out)


def push(root: Path, src: str, branch: str, run: Optional[Callable] = None, env: Optional[dict] = None) -> tuple:
    """(ok, how, why). Steps 1 and 2. `src` is a sha or ref; the push is never forced."""
    import ghrest
    import provider

    run = run or provider._run
    root = Path(root)
    refspec = "%s:refs/heads/%s" % (src, branch)
    secret = token(env)
    why = []
    slug = ghrest.slug_of(root)
    if secret and slug:
        done = run(["git", "push", "-q", "https://x-access-token:%s@github.com/%s.git" % (secret, slug), refspec], root)
        if getattr(done, "returncode", 1) == 0:
            return True, "GITHUB_TOKEN", ""
        last = ((getattr(done, "stderr", "") or "").strip().splitlines() or ["refused"])[-1]
        why.append("GITHUB_TOKEN: %s" % redact(last, secret))
    done = run(["git", "push", "-q", "origin", refspec], root)
    if getattr(done, "returncode", 1) == 0:
        return True, "origin credentials", ""
    last = ((getattr(done, "stderr", "") or getattr(done, "stdout", "") or "").strip().splitlines() or ["refused"])[-1]
    why.append("origin: %s" % redact(last, secret))
    return False, "", "; ".join(why)


def _load(beam_path: Path) -> list:
    try:
        rows = json.loads((Path(beam_path).parent / PENDING).read_text())
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _save(beam_path: Path, rows: list) -> None:
    (Path(beam_path).parent / PENDING).write_text(json.dumps(rows[-200:], indent=2) + "\n")


def fallback(beam_path: Path, root: Path, sha: str, branch: str, why: str, pr: str = "", owner: str = "",
             coordinator: str = "", agent: str = "", redo: str = "", files: Optional[list] = None,
             run: Optional[Callable] = None, now_s: str = "") -> list:
    """Step 3: keep the commit, hand the push to the parent's Cursor GitHub tools, and remember it for step 4."""
    import provider

    run = run or provider._run
    run(["git", "update-ref", "refs/warp/pending/%s" % branch, sha], Path(root))
    rows = [r for r in _load(beam_path) if r.get("branch") != branch]
    rows.append({"branch": branch, "sha": sha, "pr": pr, "owner": owner, "coordinator": coordinator, "agent": agent,
                 "redo": redo, "files": list(files or [])[:30], "at": now_s, "stage": "parent"})
    _save(beam_path, rows)
    shown = ", ".join((files or [])[:10]) or "the changed files"
    return ["push: MUST DO %s%s: Warp could not push %s (%s). Push it with your Cursor GitHub tools: from %s run "
            "`git push origin refs/warp/pending/%s:refs/heads/%s`, or with the GitHub MCP put %s on %s in one commit. "
            "The next tick checks the branch; if it is not at %s then, %s is told to redo it and push once." % (
                branch, (" (#%s)" % pr) if pr else "", sha[:12], why, root, branch, branch, shown, branch, sha[:12],
                coordinator or owner or "its owner")]


def check_pending(beam_path: Path, root: Path, send: Callable, runner: Optional[Callable] = None, now=None) -> list:
    """Step 4. Done when the branch is at the sha (or moved on past it); else the coordinator redoes it, once."""
    import beam as beam_mod
    import ghrest
    import provider

    rows = _load(beam_path)
    if not rows:
        return []
    slug = ghrest.slug_of(Path(root))
    now_dt, now_s = beam_mod.coerce_now(now)
    keep, out = [], []
    for row in rows:
        ok, doc, _ = ghrest.call(Path(root), "GET", "repos/%s/git/ref/heads/%s" % (slug, row["branch"]), runner=runner) if slug else (False, None, "")
        head = str(((doc or {}).get("object") or {}).get("sha") or "") if ok and isinstance(doc, dict) else ""
        if head and head == row["sha"]:
            out.append("push: %s is at %s; pushed by the parent" % (row["branch"], row["sha"][:12]))
            (runner or provider._run)(["git", "update-ref", "-d", "refs/warp/pending/%s" % row["branch"]], Path(root))
            continue
        if row.get("stage") == "parent":
            target = row.get("coordinator") or row.get("owner") or "warp"
            text = ("Push needed on %s%s: Warp could not push it (GITHUB_TOKEN, the checkout's credentials and the parent's "
                    "GitHub tools all failed). Redo it yourself and push once: %s" % (
                        row["branch"], (" (#%s)" % row["pr"]) if row.get("pr") else "",
                        row.get("redo") or "fetch the branch and origin/main, merge origin/main into it, and push"))
            out.extend(send(target, text, row.get("agent") or ""))
            row["stage"] = "coordinator"
            row["toldAt"] = now_s
            keep.append(row)
            continue
        told = beam_mod.parse_ts(str(row.get("toldAt") or ""))
        if head and head != row.get("headAtTell", head) or (told is not None and (now_dt - told).total_seconds() >= 7200):
            out.append("push: %s moved on (or two hours passed); the pending push of %s is dropped" % (row["branch"], row["sha"][:12]))
            continue
        row.setdefault("headAtTell", head)
        keep.append(row)
    _save(beam_path, keep)
    return out
