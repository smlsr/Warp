#!/usr/bin/env python3
"""Main red, waivers, force-push exceptions, hung CI and Bugbot, on every tick (1.7.15).

Main red. Each tick reads main's CI (provider.base_checks). When a test fails on main:
  1. the breaking commit: bisect main's last merges by their CI result (first_bad), else the test file's history
  2. a waiver in `.warp/waivers.json` (and `waivers` on the beam): name, test ids, scope heads-containing <sha> (a
     CI merge ref that contains it counts), owner (that commit's author), fix gap id, expiry = when the fix merges
  3. ONE gap ticket for the break (gaps.create), staffed at the top. Every test that the same commit broke is an
     acceptance criterion of that one gap, and a failure that shows up while the fix is open is added to it
     (gaps.extend): one fix pull request, one push, one CI run, instead of a PR per test that each fail CI
     until all are fixed.
  4. the merge gate applies waivers strictly: the waived test must be the only failure in its package, and a head
     that does not contain the breaking commit is not waived
  5. the waiver retires when its fix merges, and that is posted once

Force-push exceptions: `.warp/exceptions.json` rows {pr, reason, allowed (e.g. "reword commit X, tree identical"),
approver}. Warp allows only that operation and checks the tree is identical after it.

Hung CI: a job with no step progress for `ci.hungMinutes` (60) is cancelled once and posted. It is never rerun: the
pull request's next real push makes a fresh run.

Bugbot: a pull request Bugbot skipped as too large gets one `bugbot run` comment. A new finding on a head that was
already sent as GAPPR goes back to its owner with every open finding at once: "fix them in one push, reply Fixed in
<sha>, resolve".
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

FILE = "waivers.json"
EXCEPTIONS = "exceptions.json"


# ---- the registry ----------------------------------------------------------------------------------------------


def load(beam_path: Path) -> dict:
    try:
        doc = json.loads((Path(beam_path).parent / FILE).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save(beam_path: Path, doc: dict, data: Optional[dict] = None) -> None:
    (Path(beam_path).parent / FILE).write_text(json.dumps(doc, indent=2) + "\n")
    if data is not None:
        data["waivers"] = {k: v for k, v in doc.items() if isinstance(v, dict) and not v.get("retiredAt")}
        # The beam carries the active waivers too, so every merge gate (supervise, the lane) reads the same set.
        try:
            import beam as beam_mod
            import beamlock

            with beamlock.run_lock(Path(beam_path)):
                disk = beam_mod.load_json(Path(beam_path))
                disk["waivers"] = data["waivers"]
                beam_mod.atomic_write(Path(beam_path), json.dumps(disk, indent=2) + "\n", data=disk)
        except Exception:
            pass


def active(data: dict) -> list:
    rows = data.get("waivers") if isinstance(data.get("waivers"), dict) else {}
    return [dict(v, id=k) for k, v in rows.items() if isinstance(v, dict) and not v.get("retiredAt")]


def package_of(test: str) -> str:
    """The package a test id belongs to: `pkg/a::test_x` -> pkg/a, `web/app › renders` -> web/app, `ci / lint` -> ci."""
    text = str(test or "")
    for sep in ("::", " › ", " > ", " / ", ":", "#"):
        if sep in text:
            return text.split(sep, 1)[0].strip()
    return text.rsplit("/", 1)[0] if "/" in text else text


def gate(data: dict, head: str, failed: list, contains: Optional[Callable] = None, merge_ref: str = "") -> tuple:
    """(ok, why): every red check is waived for this head. Strict: an active waiver names the test, the head contains
    the waiver's breaking commit (contains(sha, head)), or the CI merge ref built from it does (merge_ref), and the
    test is the only failure in its package."""
    rows = active(data)
    if not failed:
        return True, ""
    by_package: dict = {}
    for name in failed:
        by_package.setdefault(package_of(name), []).append(name)
    for name in failed:
        hit = [w for w in rows if name in (w.get("tests") or [])]
        if not hit:
            return False, "%s is red and no waiver covers it" % name
        if len(by_package.get(package_of(name), [])) > 1:
            return False, "%s is waived, but it is not the only failure in %s (%s)" % (
                name, package_of(name), ", ".join(by_package[package_of(name)]))
        covered = False
        for waiver in hit:
            sha = str(waiver.get("sha") or "")
            if sha and contains is not None:
                for ref in [head] + ([merge_ref] if merge_ref else []):
                    try:
                        covered = bool(contains(sha, ref))
                    except Exception:
                        covered = False
                    if covered:
                        break
            if covered:
                break
        if not covered:
            return False, "%s is waived only for heads that contain %s; %s does not" % (
                name, ", ".join(str(w.get("sha") or "?")[:12] for w in hit), str(head)[:12])
    return True, ""


def git_contains(root: Path, runner: Optional[Callable] = None) -> Callable:
    """contains(sha, head): head (or the CI merge ref built from it) has sha as an ancestor."""
    import provider

    run = runner or provider._run

    def check(sha: str, head: str) -> bool:
        if not sha or not head:
            return False
        done = run(["git", "merge-base", "--is-ancestor", sha, head], Path(root))
        return getattr(done, "returncode", 1) == 0
    return check


# ---- main red ---------------------------------------------------------------------------------------------------


def first_bad(commits: list, result_of: Callable) -> str:
    """The first commit (oldest first) whose CI result is red after the last green one. Binary search over the
    commits with a known result; `result_of(sha)` returns "green", "red" or "" (unknown)."""
    known = [(sha, result_of(sha)) for sha in commits]
    known = [(sha, r) for sha, r in known if r in {"green", "red"}]
    if not known:
        return ""
    lo, hi = 0, len(known) - 1
    if known[hi][1] != "red":
        return ""
    if known[0][1] == "red":
        return known[0][0]
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if known[mid][1] == "red":
            hi = mid
        else:
            lo = mid
    return known[hi][0]


def main_red(beam_path: Path, data: dict, base: Optional[dict], history: Callable, author_of: Callable,
             now=None, top_n: int = 10) -> list:
    """Register a waiver and one gap per break; extend the open gap with new failures. `base` is
    provider.base_checks(); `history()` -> [(sha, result)] of main's last merges, oldest first; `author_of(sha)`."""
    import beam as beam_mod
    import gaps
    import selfdrive

    if not base or base.get("state") not in {"red", "fail", "failure"} or not base.get("failed"):
        return []
    beam_path = Path(beam_path)
    failed = list(base.get("failed") or [])
    doc = load(beam_path)
    now_s = beam_mod.coerce_now(now)[1]
    lines: list = []
    open_rows = [dict(v, id=k) for k, v in doc.items() if isinstance(v, dict) and not v.get("retiredAt")]
    covered = {t for row in open_rows for t in row.get("tests") or []}
    new = [name for name in failed if name not in covered]
    if not new:
        save(beam_path, doc, data)
        return []
    commits = history()
    results = dict(commits)
    bad = first_bad([sha for sha, _r in commits], lambda sha: results.get(sha, "")) or str(base.get("sha") or "")
    same = [row for row in open_rows if row.get("sha") == bad]
    if same:
        # The same break: the open gap takes the new tests too (one fix round).
        row = same[0]
        doc[row["id"]]["tests"] = sorted(set(row.get("tests") or []) | set(new))
        save(beam_path, doc, data)
        lines.extend(gaps.extend(beam_path, row.get("gap") or "", ["%s passes on main" % t for t in new],
                                 note="broken by the same commit %s" % bad[:12]))
        lines.extend(selfdrive.say_once(data, "waiver:%s" % row["id"], "waiver: %s now covers %s" % (
            row["id"], ", ".join(doc[row["id"]]["tests"]))))
        return lines
    if open_rows:
        # A different break while a main fix is open: still one fix pull request. Add it to the open gap.
        row = open_rows[0]
        wid = "W-%s" % bad[:8]
        doc[wid] = {"name": "main red since %s" % bad[:12], "tests": sorted(new), "sha": bad, "scope": "heads-containing",
                    "owner": author_of(bad), "gap": row.get("gap"), "createdAt": now_s, "expires": "fix-merge"}
        save(beam_path, doc, data)
        lines.extend(gaps.extend(beam_path, row.get("gap") or "", ["%s passes on main (broken by %s)" % (t, bad[:12]) for t in new],
                                 note="batched into the open main fix"))
        lines.append("waiver: %s registered (%s; heads containing %s; fix %s)" % (wid, ", ".join(new), bad[:12], row.get("gap")))
        return lines
    wid = "W-%s" % bad[:8]
    owner = author_of(bad)
    gid, made = gaps.create(beam_path, lambda: beam_mod.load_json(beam_path), "MAIN", "RED-%s" % bad[:7],
                            "Main is red since %s: %s" % (bad[:12], ", ".join(new[:4]) + (" …" if len(new) > 4 else "")),
                            acs=["%s passes on main" % t for t in new], source="main-red", score_rank=0, top_n=top_n)
    lines.extend(made)
    doc[wid] = {"name": "main red since %s" % bad[:12], "tests": sorted(new), "sha": bad, "scope": "heads-containing",
                "owner": owner, "gap": gid, "createdAt": now_s, "expires": "fix-merge"}
    save(beam_path, doc, data)
    lines.append("waiver: %s registered: %s waived on heads containing %s (owner %s, fix %s, expires when the fix merges)"
                 % (wid, ", ".join(new), bad[:12], owner or "?", gid))
    lines.append("herald: Main is red since %s (%s). Waiver %s lets unrelated pull requests merge; %s fixes it, at the "
                 "top of the queue." % (bad[:12], ", ".join(new[:3]), wid, gid))
    return lines


def retire(beam_path: Path, data: dict, now=None) -> list:
    """Waivers whose fix gap merged are retired, once."""
    import beam as beam_mod
    import terminal

    doc = load(beam_path)
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    out = []
    for wid, row in doc.items():
        if not isinstance(row, dict) or row.get("retiredAt"):
            continue
        gap = tickets.get(str(row.get("gap") or ""))
        if isinstance(gap, dict) and terminal.is_terminal(gap):
            row["retiredAt"] = beam_mod.coerce_now(now)[1]
            out.append("waiver: %s retired: its fix %s merged" % (wid, row.get("gap")))
            out.append("herald: Main fix %s merged; waiver %s (%s) is retired." % (row.get("gap"), wid, ", ".join(row.get("tests") or [])))
    save(beam_path, doc, data)
    return out


# ---- force-push exceptions ---------------------------------------------------------------------------------------


def exceptions(beam_path: Path) -> list:
    try:
        doc = json.loads((Path(beam_path).parent / EXCEPTIONS).read_text())
    except (OSError, ValueError):
        return []
    rows = doc.get("exceptions") if isinstance(doc, dict) else doc
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def force_push_allowed(beam_path: Path, pr: str, operation: str) -> Optional[dict]:
    """The approved one-off for this pull request and operation, or None. Never a blanket allowance."""
    for row in exceptions(beam_path):
        if str(row.get("pr")).lstrip("#") == str(pr).lstrip("#") and row.get("approver") and not row.get("usedAt"):
            allowed = str(row.get("allowed") or "").lower()
            if operation.lower() in allowed or allowed.startswith(operation.lower().split()[0]):
                return row
    return None


def tree_identical(root: Path, before: str, after: str, runner: Optional[Callable] = None) -> bool:
    import provider

    run = runner or provider._run
    a = run(["git", "rev-parse", "%s^{tree}" % before], Path(root))
    b = run(["git", "rev-parse", "%s^{tree}" % after], Path(root))
    return getattr(a, "returncode", 1) == 0 and (a.stdout or "").strip() == (b.stdout or "").strip() != ""


def use_exception(beam_path: Path, pr: str, operation: str, root: Path, before: str, after: str,
                  runner: Optional[Callable] = None) -> list:
    import beam as beam_mod

    row = force_push_allowed(beam_path, pr, operation)
    if row is None:
        return ["exception: no approved one-off for #%s %s; not force-pushed" % (str(pr).lstrip("#"), operation)]
    if "tree identical" in str(row.get("allowed") or "").lower() and not tree_identical(root, before, after, runner):
        return ["exception: #%s %s changes the tree; refused (approved only as tree identical)" % (str(pr).lstrip("#"), operation)]
    path = Path(beam_path).parent / EXCEPTIONS
    rows = exceptions(beam_path)
    for item in rows:
        if item is row or item == row:
            item["usedAt"] = beam_mod.utcnow()
            item["before"], item["after"] = before, after
    path.write_text(json.dumps({"exceptions": rows}, indent=2) + "\n")
    beam_mod.journal(Path(beam_path), {"type": "force-push", "pr": pr, "operation": operation, "approver": row.get("approver"),
                                       "before": before, "after": after})
    return ["exception: #%s %s allowed once (approved by %s; tree identical); logged" % (str(pr).lstrip("#"), operation, row.get("approver"))]


# ---- hung CI ------------------------------------------------------------------------------------------------------


def _last_progress(job: dict) -> str:
    stamps = [str(job.get("started_at") or "")]
    for step in job.get("steps") or []:
        if isinstance(step, dict):
            stamps.extend(str(step.get(k) or "") for k in ("started_at", "completed_at"))
    return max(stamps) if stamps else ""


def hung(beam_path: Path, data: dict, root: Path, slug: str, heads: list, runner: Optional[Callable] = None,
         now=None, minutes: float = 60) -> list:
    """Cancel, once, every in-progress run on these heads whose jobs show no step progress for `minutes`."""
    import beam as beam_mod
    import ghrest

    now_dt = beam_mod.coerce_now(now)[0]
    done = data.setdefault("ciCancelled", {})
    out = []
    for sha, label in heads:
        for run in ghrest.runs_for(root, slug, sha=sha, status="in_progress", runner=runner):
            rid = str(run.get("id"))
            if rid in done:
                continue
            jobs = ghrest.jobs_for(root, slug, rid, runner=runner)
            stamps = [_last_progress(j) for j in jobs if isinstance(j, dict) and j.get("status") != "completed"]
            last = beam_mod.parse_ts(max(stamps)) if stamps and max(stamps) else beam_mod.parse_ts(str(run.get("run_started_at") or run.get("created_at") or ""))
            if last is None or (now_dt - last).total_seconds() < minutes * 60:
                continue
            ok = ghrest.cancel_run(root, slug, rid, runner=runner)
            done[rid] = {"sha": sha, "at": beam_mod.coerce_now(now)[1], "ok": ok}
            silent = int((now_dt - last).total_seconds() // 60)
            out.append("ci: cancelled run %s on %s (%s): no step progress for %dm. Not rerun; the next real push makes a "
                       "fresh run." % (rid, label, sha[:12], silent))
            out.append("herald: CI run %s on %s hung %dm with no progress; cancelled once, not rerun." % (rid, label, silent))
    return out


# ---- Bugbot -------------------------------------------------------------------------------------------------------


_TOO_LARGE = re.compile(r"(too large|exceeds the (diff|size) limit|skipped.*(large|size))", re.I)


def bugbot_actions(beam_path: Path, data: dict, root: Path, prs: list, comment: Callable, send: Callable) -> list:
    """prs: [{number, url, head, checks, findings, gappr, owner, agent}]. Too large -> one `bugbot run` comment per
    head. New findings on a GAPPR'd head -> the owner gets all of them in one message, once per head."""
    out = []
    asked = data.setdefault("bugbotRunAsked", {})
    sent = data.setdefault("bugbotSentBack", {})
    for pr in prs:
        head = str(pr.get("head") or "")
        text = " ".join(str(c.get("summary") or c.get("title") or c.get("log") or c.get("conclusion") or "")
                        for c in pr.get("checks") or [] if isinstance(c, dict) and "bugbot" in str(c.get("name") or "").lower())
        if _TOO_LARGE.search(text) and asked.get(str(pr["number"])) != head:
            if comment(pr["url"], "bugbot run"):
                asked[str(pr["number"])] = head
                out.append("bugbot: #%s was skipped as too large; posted `bugbot run` once for %s" % (pr["number"], head[:12]))
        findings = pr.get("findings") or []
        if findings and pr.get("gappr") and sent.get(str(pr["number"])) != head:
            sent[str(pr["number"])] = head
            body = ("Bugbot found %d issue%s on %s after GAPPR. Fix all of them in one push (no partial pushes: each push "
                    "reruns CI), reply `Fixed in <sha>` on each thread, resolve them, then re-send GAPPR: %s" % (
                        len(findings), "" if len(findings) == 1 else "s", head[:12], " | ".join(str(f) for f in findings[:10])))
            out.extend(send(pr.get("owner") or "", body, pr.get("agent") or ""))
    return out
