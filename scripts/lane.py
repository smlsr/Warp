#!/usr/bin/env python3
"""The merge lane (1.7.15): verified READY/GAPPR pull requests merge in blocker-score order, then what the merge freed
moves in the same tick.

Merge: REST squash (mergeMethod) with sha=<verified head> as the guard (ghrest.merge). A moved head is not merged;
it is verified again from the next READY.

After each merge:
  children    every open pull request whose base was the merged branch is retargeted to main (PATCH base=main).
              A child that now conflicts only because it carries the parent's pre-squash commits is rebuilt: merge
              the parent's pre-squash head, then main; check that its diff against main holds only its own files;
              push once (the lane is the sole pusher for that one push, recorded on the beam)
  holds       every hold that named the merged pull request, ticket or gap is lifted, and live runs are re-dispatched
  union files changelog.d fragments and append-only registries and ledgers are resolved by keeping both sides,
              without asking the owner to push. Files edited in place are never a line union (1.7.20):
              GAP-REPORT.md merges row by row on gap id (gapreport.gap_resolve) and docs/dev/testing/test-verify.md
              section by section on its headings (gapreport.section_resolve). Before any lane push, gap_check compares
              the merged gap report with main and with the owner's own head, and a merge that adds rows, repeats a
              row or leaves a stale open copy is not pushed (conflict: the owner gets a REFRESH)

Stale pull requests: a pull request dirty against main (or its base, for a stacked one) gets one REFRESH to its
owner: "merge <base> once, run the gate, push once, re-send READY". A conflict only in union files is refreshed by
the lane itself. Each pull request has a refresh budget (`refresh.budget`); an owner who has not pushed within
`refresh.timeout` minutes is restaffed (the sweep's ladder).
"""

from __future__ import annotations

import fnmatch
import re

import gapreport
import tempfile
from pathlib import Path
from typing import Callable, Optional

# Append-only files only (1.7.20: GAP-REPORT.md and test-verify.md are edited in place and have keyed resolvers).
UNION = ("changelog.d/*", "**/changelog.d/*", "**/*.registry.md", "**/registry.jsonl", "**/*.ledger.md")


def resolvable(path: str, extra=()) -> bool:
    """A conflict here is resolved by the lane: a keyed resolver, or an append-only union."""
    return gapreport.keyed_resolver(path) is not None or is_union(path, extra)


# ---- union files ------------------------------------------------------------------------------------------------


def is_union(path: str, extra=()) -> bool:
    path = str(path or "").lstrip("./")
    return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch("x/" + path, pattern) for pattern in tuple(UNION) + tuple(extra))


_CONFLICT = re.compile(r"<<<<<<< [^\n]*\n(.*?)(?:\|\|\|\|\|\|\| [^\n]*\n.*?)?=======\n(.*?)>>>>>>> [^\n]*\n", re.S)


def union_resolve(text: str) -> str:
    """Both sides of every conflict, ours first; a line already on our side is not repeated."""
    def keep(match):
        ours, theirs = match.group(1), match.group(2)
        have = set(ours.splitlines())
        extra = "".join(line + "\n" for line in theirs.splitlines() if line not in have)
        return ours + extra
    return _CONFLICT.sub(keep, text)


# ---- git helpers ------------------------------------------------------------------------------------------------


def _git(run: Callable, cwd: Path, *args: str):
    return run(["git", *args], Path(cwd))


def _ok(done) -> bool:
    return getattr(done, "returncode", 1) == 0


def _out(done) -> str:
    return (getattr(done, "stdout", "") or "").strip()


def merge_into(root: Path, branch: str, onto: str, run: Optional[Callable] = None, push: bool = True,
               extra_union=(), first: str = "") -> tuple:
    """Merge `onto` (and `first` before it) into origin/<branch> in a temporary worktree. Union-file conflicts are
    resolved keeping both sides; any other conflict aborts. Pushes once (not forced) when `push`.
    Returns (outcome, detail): merged | conflict | unchanged | failed."""
    import provider

    run = run or provider._run
    root = Path(root)
    _git(run, root, "fetch", "-q", "origin", branch, onto.replace("origin/", ""))
    tmp = Path(tempfile.mkdtemp(prefix="warp-lane-"))
    added = _git(run, root, "worktree", "add", "-q", "--detach", str(tmp), "origin/%s" % branch)
    if not _ok(added):
        return "failed", "worktree: %s" % ((getattr(added, "stderr", "") or "").strip()[:200])
    try:
        before = _out(_git(run, tmp, "rev-parse", "HEAD"))
        for ref in [first, onto]:
            if not ref:
                continue
            if ref == first and not _ok(_git(run, tmp, "cat-file", "-e", "%s^{commit}" % ref)):
                continue  # the parent's pre-squash head is not here (its branch was deleted); main alone decides
            done = _git(run, tmp, "-c", "user.name=Warp", "-c", "user.email=warp@localhost", "merge", "--no-edit", "-q", ref)
            if _ok(done):
                continue
            conflicted = [p for p in _out(_git(run, tmp, "diff", "--name-only", "--diff-filter=U")).splitlines() if p]
            other = [p for p in conflicted if not resolvable(p, extra_union)]
            if other or not conflicted:
                _git(run, tmp, "merge", "--abort")
                return "conflict", ", ".join(other or ["(merge failed)"])
            for rel in conflicted:
                target = tmp / rel
                resolver = gapreport.keyed_resolver(rel)
                if resolver is not None:
                    # 1.7.20: keyed three-way merge. Stage 2 is the branch being refreshed (the PR), stage 3 is what
                    # is merged into it (main).
                    stage = lambda n: _stage(run, tmp, n, rel)
                    try:
                        target.write_text(resolver(stage(1), stage(3), stage(2)))
                    except gapreport.Conflict as exc:
                        _git(run, tmp, "merge", "--abort")
                        return "conflict", "%s (%s)" % (rel, exc)
                else:
                    target.write_text(union_resolve(target.read_text()))
                _git(run, tmp, "add", rel)
            done = _git(run, tmp, "-c", "user.name=Warp", "-c", "user.email=warp@localhost", "commit", "--no-edit", "-q")
            if not _ok(done):
                return "failed", "commit after union resolve failed"
        after = _out(_git(run, tmp, "rev-parse", "HEAD"))
        if after == before:
            return "unchanged", after
        # 1.7.20: never push a gap report the merge made worse, however the merge produced it. The PR side is the
        # owner's own head, not a head that already carries an earlier lane merge.
        bad = gap_report_findings(tmp, onto, owner_head(run, tmp, before), "HEAD", run)
        if bad:
            _git(run, tmp, "reset", "-q", "--hard", before)
            return "conflict", "the gap report merge would add %s" % "; ".join(bad[:6])
        if push:
            import pusher

            ok, _how, why = pusher.push(root, after, branch, run=run)
            if not ok:
                return "push-failed", "%s %s" % (after, why)
        return "merged", after
    finally:
        _git(run, root, "worktree", "remove", "--force", str(tmp))


LANE_AUTHOR = "Warp"


def _stage(run: Callable, cwd: Path, n: int, rel: str) -> str:
    done = _git(run, cwd, "show", ":%d:%s" % (n, rel))
    return (getattr(done, "stdout", "") or "") if _ok(done) else ""


def owner_head(run: Callable, cwd: Path, head: str) -> str:
    """The newest commit on the branch the owner made: commits the lane itself pushed (author Warp) are skipped, so
    extras an earlier lane merge added never count as the PR's own rows."""
    done = _git(run, cwd, "log", "--first-parent", "--format=%H %an", "-n", "50", head)
    for line in _out(done).splitlines():
        sha, _, author = line.partition(" ")
        if author.strip() != LANE_AUTHOR:
            return sha
    return head


def _show(run: Callable, cwd: Path, ref: str, rel: str) -> Optional[str]:
    done = _git(run, cwd, "show", "%s:%s" % (ref, rel))
    return (getattr(done, "stdout", "") or "") if _ok(done) else None


def gap_report_findings(cwd: Path, main: str, pr: str, merged: str, run: Callable) -> list:
    """gap_check for every GAP-REPORT.md the merge changed relative to the PR side: main, the PR side, the result."""
    names = set()
    for old in (main, pr):
        names |= {p for p in _out(_git(run, cwd, "diff", "--name-only", old, merged)).splitlines() if gapreport.is_gap_report(p)}
    found = []
    for rel in sorted(names):
        result = _show(run, cwd, merged, rel)
        if result is None:
            continue
        found.extend("%s: %s" % (rel, item) for item in gapreport.gap_check(_show(run, cwd, main, rel) or "",
                                                                           _show(run, cwd, pr, rel) or "", result))
    return found


def check_merge_ref(root: Path, number: str, base: str = "main", run: Optional[Callable] = None) -> list:
    """What GitHub's own merge of pull request `number` (refs/pull/N/merge) would do to the gap report, compared with
    main and with the PR's head (refs/pull/N/head), before the lane or supervise merges it. [] when clean or when the
    refs cannot be read. Twins already on main are not findings."""
    import provider

    run = run or provider._run
    merge_ref, head_ref = "refs/warp/check/%s-merge" % number, "refs/warp/check/%s-head" % number
    got = _git(run, root, "fetch", "-q", "origin", "+refs/pull/%s/merge:%s" % (number, merge_ref),
               "+refs/pull/%s/head:%s" % (number, head_ref))
    if not _ok(got):
        return []
    try:
        return gap_report_findings(root, "origin/%s" % base, owner_head(run, root, head_ref), merge_ref, run)
    finally:
        _git(run, root, "update-ref", "-d", merge_ref)
        _git(run, root, "update-ref", "-d", head_ref)


def own_files(root: Path, child: str, parent_head: str, run: Optional[Callable] = None) -> set:
    import provider

    run = run or provider._run
    done = _git(run, root, "diff", "--name-only", "%s...origin/%s" % (parent_head, child))
    return {p for p in _out(done).splitlines() if p}


def rebuild_child(root: Path, child: str, parent_head: str, base: str = "main", run: Optional[Callable] = None) -> tuple:
    """Rebuild a stacked child after its parent squash-merged: merge the parent's pre-squash head, then main; the
    diff against main must hold only the child's own files; push once. (outcome, detail)."""
    import provider

    run = run or provider._run
    root = Path(root)
    _git(run, root, "fetch", "-q", "origin", base, child)
    mine = own_files(root, child, parent_head, run)
    outcome, detail = merge_into(root, child, "origin/%s" % base, run=run, push=False, first=parent_head)
    if outcome not in {"merged", "unchanged"}:
        return outcome, detail
    # Check before pushing: the rebuilt head, diffed against main, has only the child's own files.
    tmp_head = detail
    diff = {p for p in _out(_git(run, root, "diff", "--name-only", "origin/%s" % base, tmp_head)).splitlines() if p}
    foreign = sorted(diff - mine - {p for p in diff if resolvable(p)})
    if foreign:
        return "foreign", ", ".join(foreign[:8])
    if outcome == "unchanged":
        return "unchanged", tmp_head
    import pusher

    ok, _how, why = pusher.push(root, tmp_head, child, run=run)
    if not ok:
        return "push-failed", "%s %s" % (tmp_head, why)
    return "rebuilt", tmp_head


# ---- the lane ---------------------------------------------------------------------------------------------------


def run_lane(beam_path: Path, data: dict, merge: Callable, list_children: Callable, retarget: Callable,
             rebuild: Callable, send: Callable, max_merges: int = 3, now=None, push_failed: Optional[Callable] = None,
             precheck: Optional[Callable] = None) -> list:
    """Merge the queue in score order. merge(entry) -> (outcome, detail); list_children(branch) -> [{number, url,
    branch, owner, agent, mergeable}]; retarget(url, base) -> (ok, why); rebuild(child_branch, parent_head) ->
    (outcome, detail); send(owner, text, agent) -> lines."""
    import beam as beam_mod
    import gaps
    import intake
    import relay
    import selfdrive

    lane = intake.load_lane(beam_path)
    queue = lane.setdefault("queue", {})
    seen = lane.setdefault("seen", {})
    out: list = []
    merged = 0
    now_s = beam_mod.coerce_now(now)[1]
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for key, entry in intake.ordered(beam_path):
        if merged >= max_merges:
            out.append("lane: %d more verified pull request%s wait for the next tick (maxMergesPerPass %d)" % (
                len(queue) - merged, "" if len(queue) - merged == 1 else "s", max_merges))
            break
        bad = precheck(entry) if precheck is not None else []
        if bad:
            queue.pop(key, None)
            seen[key] = "refused: gap report"
            out.append("lane: #%s not merged: its merge would leave the gap report with %s" % (entry["pr"], "; ".join(bad[:4])))
            out.extend(send(relay_owner(data, entry), "#%s is not merged: GitHub's merge of it would leave the gap report "
                            "with %s. Merge main, keep one row per gap id (the closed copy when a gap is closed), push "
                            "once, re-send READY." % (entry["pr"], "; ".join(bad[:6])), relay_agent(data, entry)))
            continue
        outcome, detail = merge(entry)
        if outcome != "merged":
            queue.pop(key, None)
            seen[key] = "refused: %s" % detail
            out.append("lane: #%s not merged: %s; it waits for a new READY on its next head" % (entry["pr"], detail))
            continue
        merged += 1
        queue.pop(key, None)
        seen[key] = "merged"
        lane.setdefault("merged", []).append({"pr": entry["pr"], "sha": detail, "at": now_s, "readyAt": entry.get("queuedAt")})
        lane["merged"] = lane["merged"][-500:]
        out.append("lane: #%s merged (%s, squash with the sha guard %s)" % (entry["pr"], detail[:12], entry["sha"][:12]))
        tid = entry.get("ticket") or ""
        if tid and isinstance(tickets.get(tid), dict):
            row = tickets[tid]
            row["status"] = "merged"
            row.setdefault("pr", {}).update(sha=detail, mergedAt=now_s, mergedBy="lane")
        data.setdefault("mergedPrs", []).append(str(entry["pr"]))
        # Holds that named it lift now.
        for name in filter(None, [tid, entry.get("gap"), "#%s" % entry["pr"]]):
            out.extend(gaps.lift_holds(data, name))
        # Children: retarget, rebuild if they carry the parent's pre-squash commits.
        for child in list_children(entry.get("branch") or ""):
            ok, why = retarget(child["url"], "main")
            if not ok:
                out.append("lane: child #%s not retargeted to main: %s" % (child["number"], why))
                continue
            out.append("lane: child #%s retargeted to main" % child["number"])
            if child.get("ownedBy"):
                # 1.7.17: someone else owns the child; it rebuilds it, Warp does not push to it.
                out.extend(send(child["ownedBy"], "#%s merged and #%s is retargeted to main. Rebuild it once: merge the "
                                "pre-squash head %s, then origin/main; keep only your own files; push once; re-send READY."
                                % (entry["pr"], child["number"], entry["sha"][:12]), child.get("agent") or ""))
                continue
            if str(child.get("mergeable") or "").upper() in {"CONFLICTING", "DIRTY"} or child.get("stacked", True):
                result, info = rebuild(child["branch"], entry["sha"])
                if result == "rebuilt":
                    relay.set_owner(data, child["number"], "lane", coordinator="warp", now_s=now_s)
                    out.append("lane: child #%s rebuilt on main after #%s (only its own files; pushed once, %s)" % (
                        child["number"], entry["pr"], info[:12]))
                    out.extend(send(child.get("owner") or "", "#%s merged; Warp rebuilt #%s on main (%s, one push). Pull "
                                    "it before you push again; CI runs once on it." % (entry["pr"], child["number"], info[:12]),
                                    child.get("agent") or ""))
                elif result == "push-failed" and push_failed is not None:
                    sha, _, why = info.partition(" ")
                    out.extend(push_failed(child["branch"], sha, why, child["number"], child.get("owner") or "",
                                           child.get("agent") or "", "after #%s merged, merge its pre-squash head %s and "
                                           "then origin/main into %s (only your own files stay in the diff), and push "
                                           "once" % (entry["pr"], entry["sha"][:12], child["branch"])))
                elif result == "foreign":
                    out.extend(send(child.get("owner") or "", "REFRESH #%s: after #%s merged, a rebuild would carry files "
                                    "that are not yours (%s). Merge main once, run the gate, push once, re-send READY." % (
                                        child["number"], entry["pr"], info), child.get("agent") or ""))
                elif result == "conflict":
                    out.extend(send(child.get("owner") or "", "REFRESH #%s: it conflicts with main after #%s merged (%s). "
                                    "Merge main once, run the gate, push once, re-send READY." % (
                                        child["number"], entry["pr"], info), child.get("agent") or ""))
    intake.save_lane(beam_path, lane)
    return out


def relay_owner(data: dict, entry: dict) -> str:
    import relay

    row = relay.owner_of(data, str(entry.get("pr")))
    return str(row.get("coordinator") if row.get("coordinator") not in {"", "warp", None} else row.get("owner")
               or entry.get("ticket") or "warp")


def relay_agent(data: dict, entry: dict) -> str:
    import relay

    return str(relay.owner_of(data, str(entry.get("pr"))).get("agent") or "")


def refresh_stale(beam_path: Path, data: dict, prs: list, union_merge: Callable, send: Callable, cfg: dict,
                  now=None, push_failed: Optional[Callable] = None) -> list:
    """prs: [{number, branch, base, dirty, conflicts, owner, agent, head}]. Union-only conflicts: the lane merges the
    base into the branch itself (one push). Otherwise one REFRESH per head, within the budget; past refresh.timeout
    with no new head the pull request is due for restaffing (returned as `restaff:` lines the sweep acts on)."""
    import beam as beam_mod
    import selfdrive

    now_dt, now_s = beam_mod.coerce_now(now)
    book = data.setdefault("refresh", {})
    budget = int(selfdrive.num(cfg, "refresh", "budget"))
    timeout = selfdrive.num(cfg, "refresh", "timeout")
    out = []
    for pr in prs:
        if not pr.get("dirty"):
            book.pop(str(pr["number"]), None)
            continue
        row = book.get(str(pr["number"])) if isinstance(book.get(str(pr["number"])), dict) else {"sent": 0}
        conflicts = list(pr.get("conflicts") or [])
        if conflicts and all(resolvable(p) for p in conflicts) and not pr.get("ownedBy"):
            outcome, detail = union_merge(pr["branch"], "origin/%s" % (pr.get("base") or "main"))
            if outcome == "merged":
                out.append("lane: #%s conflicted only in union files (%s); merged %s into it keeping both sides, one "
                           "push (%s), no owner push" % (pr["number"], ", ".join(conflicts), pr.get("base") or "main", detail[:12]))
                book.pop(str(pr["number"]), None)
                continue
            if outcome in {"refused", "conflict"}:
                conflicts = conflicts + [detail]
            if outcome == "push-failed" and push_failed is not None:
                sha, _, why = detail.partition(" ")
                out.extend(push_failed(pr["branch"], sha, why, pr["number"], pr.get("owner") or "", pr.get("agent") or "",
                                       "merge origin/%s into %s keeping both sides of %s, and push once" % (
                                           pr.get("base") or "main", pr["branch"], ", ".join(conflicts))))
                book.pop(str(pr["number"]), None)
                continue
        if row.get("head") == pr.get("head") and row.get("sentAt"):
            sent = beam_mod.parse_ts(row["sentAt"])
            if sent is not None and (now_dt - sent).total_seconds() >= timeout * 60:
                if not row.get("restaffed"):
                    row["restaffed"] = now_s
                    out.append("restaff: #%s owner %s did not push within %dm of REFRESH; restaff it (the new agent "
                               "starts from the remote head %s)" % (pr["number"], pr.get("owner") or "?", timeout, str(pr.get("head") or "")[:12]))
            book[str(pr["number"])] = row
            continue
        if int(row.get("sent") or 0) >= budget:
            if not row.get("restaffed"):
                row["restaffed"] = now_s
                out.append("restaff: #%s used its refresh budget (%d); restaff it" % (pr["number"], budget))
            book[str(pr["number"])] = row
            continue
        text = ("REFRESH #%s: it is dirty against %s%s. Merge %s once, run the gate, push once (one push for every "
                "conflict and every red check together), re-send READY." % (
                    pr["number"], pr.get("base") or "main", (" (%s)" % ", ".join(conflicts[:6])) if conflicts else "",
                    pr.get("base") or "main"))
        out.extend(send(pr.get("owner") or "", text, pr.get("agent") or ""))
        row.update(sent=int(row.get("sent") or 0) + 1, sentAt=now_s, head=pr.get("head"))
        book[str(pr["number"])] = row
    return out


def main(argv: Optional[list] = None) -> int:
    """`lane.py gap-dedupe --report docs/spec/GAP-REPORT.md [--write] [--rename-twins]` (1.7.20): the one-time cleanup.
    It removes only identical repeated rows and stale open copies of closed rows; twins (one id, different gaps) are
    reported, or renamed with --rename-twins. --write refuses on main: run it from a branch and merge a normal PR."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="The merge lane's tools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    gd = sub.add_parser("gap-dedupe")
    gd.add_argument("--report", required=True)
    gd.add_argument("--write", action="store_true")
    gd.add_argument("--rename-twins", dest="rename_twins", action="store_true")
    args = parser.parse_args(argv)
    code, lines = gapreport.cleanup(Path(args.report), write=args.write, rename_twins=args.rename_twins)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
