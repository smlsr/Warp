#!/usr/bin/env python3
"""The fast lane (1.7.25): clean pull requests merge as soon as CI is green and Bugbot has no open threads; every other
pull request rebases, passes local CI, and pushes once, all at the same time.

What made a busy run slow: every merge left the other open pull requests BEHIND main, and Warp treated BEHIND like a
conflict. Each one got a rebase Shuttle, which pushed, which reran full CI and Bugbot, by which time the next merge had
made it BEHIND again. Ready pull requests left the merge queue the same way. A conflicting pull request also waited for
Bugbot and for every check on its old head before its rebase started, and each merge ran the full test command locally
first, one pull request at a time.

With `fastMerge` (default on):
  - BEHIND is not a conflict. A pull request whose checks are green merges while it is behind. Only when GitHub refuses
    the merge because the branch must be up to date does Warp ask GitHub to update the branch itself
    (`PUT /pulls/N/update-branch`, guarded by the head): no agent, one server-side commit, and it merges as soon as CI
    is green on the new head. An update GitHub cannot make (a real conflict) goes to a rebase Shuttle.
  - Bugbot gates on open threads: once Bugbot has reviewed the pull request, a later head whose Bugbot threads are all
    resolved, outdated or answered "Fixed in <sha>" (and has no findings of its own) does not wait for Bugbot to run
    again. A pull request Bugbot never reviewed still climbs the 1.7.21 ladder.
  - Every ready pull request is merged in the same pass (`maxMergesPerPass: 0`, no cap), and the local merge check is
    skipped when CI already ran green on the head (it still runs for a repo with no CI on the pull request).
  - A real conflict starts its rebase at once, before Bugbot or old checks are waited for. The rebase round also carries
    every red check and Bugbot finding on the head, runs local CI to green, and pushes once. Rebases of different pull
    requests run in parallel (up to maxAgents and the locks).
`fastMerge: false` brings back 1.7.24 behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# GitHub's answers when protection requires an up-to-date branch. "Base branch was modified" is not one: that is an
# earlier merge in the same pass, and the pull request is asked again next pass.
BEHIND_REFUSAL = re.compile(r"not up to date|up-to-date|out of date|is behind|must be updated|update the branch|"
                            r"head branch is not up", re.I)


def on(config: Optional[dict]) -> bool:
    raw = (config or {}).get("fastMerge", True)
    return str(raw).strip().casefold() not in {"false", "no", "off", "0"}


def behind_refusal(detail: str) -> bool:
    return bool(BEHIND_REFUSAL.search(str(detail or "")))


def _head(pr: dict) -> str:
    return str((pr or {}).get("headSha") or "")


def remember_review(pr: dict, state: str, sha: str) -> None:
    """Bugbot reported (pass or fail) on `sha`: the pull request has been reviewed by Bugbot."""
    if str(state or "").strip().casefold() in {"pass", "fail"} and sha:
        pr["bugbotEverSha"] = str(sha)


def reviewed_before(pr: dict) -> bool:
    """Bugbot reported pass or fail on an earlier head of this pull request (not just "pending" on this one)."""
    ever = str(pr.get("bugbotEverSha") or "")
    return bool(ever) and ever != _head(pr)


def note_bugbot_clear(data: dict, ticket: dict, config: Optional[dict], lines: Optional[list] = None) -> bool:
    """Record `bugbotClearSha` for the head when Bugbot has reviewed this pull request before, every Bugbot thread on it
    is addressed, and the head carries no findings. Live passes only (it reads the threads). True when it is clear."""
    import bugbot
    import pipeline

    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    head = _head(pr)
    if not on(config) or not head or not pr.get("url"):
        return False
    if str(pr.get("bugbotClearSha") or "") == head:
        return True
    if not reviewed_before(pr) or pipeline.findings_of(ticket) or pipeline.bugbot_state(ticket) in pipeline._FAIL_BUG:
        return False
    if bugbot.THREADS is None and not data.get("_live"):
        return False
    reader = bugbot.THREADS or (lambda r, u: __import__("provider").bugbot_threads(r, u))
    try:
        threads = reader(bugbot._root(data) or Path("."), pr["url"])
    except Exception:
        threads = None
    if threads is None:
        return False
    open_ = [t for t in threads if not bugbot._addressed(t)]
    pr["bugbotOpenThreads"] = len(open_)
    pr["bugbotThreadsSha"] = head
    if open_:
        return False
    pr["bugbotClearSha"] = head
    if lines is not None:
        lines.append("bugbot: clear %s on %s: all %d Bugbot thread%s addressed; it merges when CI is green (fastMerge)"
                     % (ticket.get("id"), head[:12], len(threads), "" if len(threads) == 1 else "s"))
    return True


def bugbot_clear(ticket: dict) -> bool:
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    head = _head(pr)
    return bool(head) and str(pr.get("bugbotClearSha") or "") == head


def ci_ran_green(ticket: dict, config: Optional[dict]) -> bool:
    """CI ran on this head and is green: the local merge check would only repeat it."""
    import orchestrator

    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    if pr.get("ciAbsent") or not _head(pr):
        return False
    rollup = str(pr.get("rollup") or "").strip().casefold()
    return rollup in {"green", "success", "passed", "pass"} and orchestrator.checks_green(ticket, config or {})


def update_branch(data: dict, ticket: dict, repo: Path, runner=None) -> tuple:
    """Ask GitHub to bring the branch up to date. (ok, why)."""
    import ghrest

    pr = ticket.setdefault("pr", {})
    if str(pr.get("updateBranchSha") or "") == _head(pr) and _head(pr):
        return True, "asked already for %s" % _head(pr)[:12]
    ok, why = ghrest.update_branch(repo, str(pr.get("url") or ""), head=_head(pr), runner=runner)
    if ok:
        import beam as beam_mod

        pr["updateBranchSha"] = _head(pr)
        pr["updateBranchAt"] = beam_mod.utcnow()
    return ok, why


def rebase_round(ticket: dict, config: Optional[dict]) -> tuple:
    """(detail, reasons) for a rebase round that also clears every red check and Bugbot finding on the head."""
    import pipeline

    reasons = [r for r in pipeline.red_reasons(ticket, config or {}) if r]
    detail = pipeline.rebase_detail(ticket)
    if reasons:
        detail += "; in the same round also fix: " + " | ".join(pipeline.batched(reasons))
    detail += ("; then run the local CI command (checkCommand, else ./scripts/ci/local.sh) until it exits 0, and push "
               "once: one push for the merge, every conflict and every fix together")
    return detail, ["rebase: the branch conflicts with the base"] + reasons
