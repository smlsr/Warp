#!/usr/bin/env python3
"""Warp's own AI review, when Cursor Bugbot will not run (1.7.21).

When Warp is the one asking Bugbot for a review (bugbotTrigger auto, comment or manual), every pull request climbs one
ladder per head, with a `bugbotStartMinutes` (default 20) wait on each rung:

  1. `bugbot run`, posted as a pull request comment; nothing after the wait: post it once more (2 tries)
  2. `/review-bugbot <pr>` (`bugbotManualCommand`), run by the parent; nothing after the wait:
  3. Warp's own AI review takes the pull request over (`pr.aiReview`), for this head and every later one:
       - one AI review Shuttle (`step=aireview`) reviews the diff the way Bugbot does, fixes every finding in one push,
         reviews the new head again, and repeats until a pass finds nothing (at most MAX_PASSES passes); it records
         `review-pass` with each finding it fixed, or `review-fail` with what is left (that goes to a fix round, and
         the next head gets a new AI review)
       - when the head is clean (the AI review passed it, checks green, acceptance met), Warp posts the findings and
         their fixes on the pull request and on the Jira issue, APPROVES the pull request, and merges it

A Bugbot check that shows up on the head later still counts: its findings go to fix like any other.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

STEP = "aireview"
MAX_SHUTTLES = 2  # AI review Shuttles on one head that came back with nothing recorded, before a person is asked
MAX_PASSES = 3  # review -> fix -> review passes inside one AI review Shuttle
SOURCE = "ai"
APPROVER = "warp-ai-review"
RUN: Optional[Callable] = None  # tests replace it: (cmd list) -> CompletedProcess-like


def on(pr: dict) -> bool:
    return bool((pr or {}).get("aiReview"))


def _head(pr: dict) -> str:
    return str((pr or {}).get("headSha") or "").strip()


def shuttles_on_head(pr: dict) -> int:
    if str(pr.get("aiReviewShuttleSha") or "") != _head(pr):
        return 0
    try:
        return int(pr.get("aiReviewShuttles") or 0)
    except (TypeError, ValueError):
        return 0


def note_shuttle(ticket: dict) -> int:
    pr = ticket.setdefault("pr", {})
    count = shuttles_on_head(pr) + 1
    pr["aiReviewShuttleSha"] = _head(pr)
    pr["aiReviewShuttles"] = count
    return count


def detail(ticket: dict, data: dict) -> str:
    pr = ticket.get("pr") or {}
    return ("AI review of %s (Cursor Bugbot did not run: %s). Review, fix every finding in one push, review again, until "
            "clean; then record the result." % (pr.get("url") or ticket.get("branch") or "the branch",
                                                pr.get("aiReviewWhy") or "no answer"))


def prompt_lines(ticket: dict, parent: str, scripts: str, agent: str, focus: str) -> list:
    """What the AI review Shuttle does. It replaces the Bugbot review on this pull request."""
    tid = str(ticket.get("id") or "")
    record = ("python3 %s/ticket_state.py append --root %s --id %s --state review-pass|review-fail --sha <head sha> "
              "--agent %s --source %s [--finding \"<file:line what was wrong> (fixed in <sha>)\" ...]"
              % (scripts, parent, tid, agent, SOURCE))
    return [
        "AI REVIEW %s. Cursor Bugbot did not run on this pull request, so you are its review. You may change code inside "
        "your locks." % tid,
        "1. Check out the pull request branch and diff it against the base branch. %s" % focus,
        "2. Write down every finding (file:line and what is wrong). Fix all of them in ONE commit and push it once, with a "
        "test that fails without the fix wherever one can be written. Run the check command to green before the push.",
        "3. Review the new head again, the same way, and repeat steps 2-3 until a pass finds nothing (at most %d passes)."
        % MAX_PASSES,
        "4. Record the result for the final head (`git rev-parse HEAD`). Clean: review-pass, with one --finding per "
        "finding you fixed and the commit that fixed it. Not clean after %d passes: review-fail, with one --finding per "
        "finding still open:" % MAX_PASSES,
        record,
        "Never record review-pass on a head you did not review, and never leave a finding out of the record: Warp posts "
        "them on the pull request and the Jira issue, then approves and merges a clean head.",
        "Return one line: result: %s ok review=pass|fail findings=<n> passes=<n>" % tid,
    ]


def start(data: dict, ticket: dict, lines: list) -> None:
    """The pull request is on AI review and its head has no review recorded: one AI review Shuttle."""
    import agents
    import bugbot
    import pipeline

    tid = ticket.get("id")
    if agents.shuttle_busy(data, ticket, beam_path=pipeline._beam_path(data)):
        lines.append("shuttle: hold %s" % tid)
        return
    pr = ticket.setdefault("pr", {})
    out = pipeline._step_out(data, ticket, STEP, str(pr.get("aiReviewShuttleSha") or "") == _head(pr))
    if out == "hold":
        lines.append("shuttle: hold %s" % tid)
        return
    if out == "relaunch":
        lines.extend(pipeline._start_lines(data, ticket, STEP, detail(ticket, data)))
        lines.append("bugbot: ai-review-shuttle %s %d/%d" % (tid, shuttles_on_head(pr), MAX_SHUTTLES))
        return
    if shuttles_on_head(pr) >= MAX_SHUTTLES:
        bugbot.ask_manual(data, ticket, lines)
        lines.append("review: wait %s" % tid)
        return
    count = note_shuttle(ticket)
    ticket["status"] = "review"
    pipeline._phase(ticket, "reviewing", lines)
    pipeline.mark_shuttle(ticket, STEP, data)
    lines.extend(pipeline._start_lines(data, ticket, STEP, detail(ticket, data)))
    lines.append("bugbot: ai-review-shuttle %s %d/%d" % (tid, count, MAX_SHUTTLES))


def note_result(pr: dict, sha: str, result: str, findings: list, at: str) -> None:
    """Keep every AI review's findings (fixed or open) for the comments posted when the head is clean."""
    log = pr.get("aiReviewLog") if isinstance(pr.get("aiReviewLog"), list) else []
    log.append({"sha": sha, "result": result, "findings": list(findings or []), "at": at})
    pr["aiReviewLog"] = log[-20:]


def clean_on_head(ticket: dict) -> bool:
    pr = ticket.get("pr") or {}
    head = _head(pr)
    return on(pr) and bool(head) and str(pr.get("bugbotSource") or "") == SOURCE and \
        str(pr.get("bugbotRecordedSha") or "") == head and str(pr.get("bugbot") or "").casefold() == "pass"


def report(ticket: dict) -> str:
    pr = ticket.get("pr") or {}
    head = _head(pr)
    found = []
    for row in pr.get("aiReviewLog") or []:
        for item in row.get("findings") or []:
            if item not in found:
                found.append(item)
    body = ["Warp AI review: clean on %s. Cursor Bugbot did not run (%s), so Warp reviewed it." % (
        head[:12], pr.get("aiReviewWhy") or "no answer")]
    if found:
        body.append("Findings, each fixed before this head:")
        body.extend("- %s" % item for item in found)
    else:
        body.append("No findings.")
    body.append("Approved by Warp's AI review; merging.")
    return "\n".join(body)


def _run(cmd: list, root: Path):
    if RUN is not None:
        return RUN(cmd)
    return subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=60)


def _gh_ok(data: dict) -> bool:
    return RUN is not None or (bool(data.get("_live")) and bool(shutil.which("gh")))


def on_clean(data: dict, ticket: dict, lines: list) -> None:
    """The head passed the AI review and is otherwise clean: post the findings, approve, and let it merge. Once a head."""
    pr = ticket.setdefault("pr", {})
    head = _head(pr)
    if not head or str(pr.get("aiCleanSha") or "") == head:
        return
    import beam as beam_mod

    pr["aiCleanSha"] = head
    tid = ticket.get("id")
    url = str(pr.get("url") or "")
    body = report(ticket)
    root = Path(data.get("_repoRoot") or ".")
    lines.append("bugbot: ai-review clean %s on %s; approving and merging" % (tid, head[:12]))
    # 1. The findings on the pull request.
    posted = False
    if url and _gh_ok(data):
        try:
            done = _run(["gh", "pr", "comment", url, "--body", body], root)
            posted = getattr(done, "returncode", 1) == 0
        except Exception:
            posted = False
    if posted:
        lines.append("bugbot: ai-review posted %s %s" % (tid, url))
    elif url:
        lines.append("bugbot: MUST DO %s: post this comment on %s (gh pr comment, or the GitHub MCP add_issue_comment):" % (tid, url))
        lines.append(body)
    # 2. The findings on the Jira issue.
    lines.extend(_jira_lines(data, ticket, body, head))
    # 3. Approve.
    approved = False
    if url and _gh_ok(data):
        try:
            done = _run(["gh", "pr", "review", url, "--approve", "--body",
                         "Warp AI review: clean on %s." % head[:12]], root)
            approved = getattr(done, "returncode", 1) == 0
        except Exception:
            approved = False
    if approved:
        lines.append("bugbot: ai-review approved %s %s" % (tid, url))
    elif url:
        lines.append(
            "bugbot: MUST DO %s: approve %s with the GitHub MCP (pull request review, event APPROVE, body \"Warp AI review: "
            "clean on %s.\"). If GitHub refuses because the pull request is your own, nothing more to do: Warp's approval "
            "is recorded and the merge goes on." % (tid, url, head[:12]))
    # 4. Merge: Warp's AI approval is the approval (aiReviewMerge, default on).
    if str((data.get("config") or {}).get("aiReviewMerge", True)).strip().casefold() not in {"false", "no", "off", "0"}:
        pr["approvedAt"] = beam_mod.utcnow()
        pr["approvedBy"] = APPROVER
        pr["approvedSha"] = head
    lines.append("herald: %s Warp AI review: clean on %s (%d finding(s) fixed). Approved; merging. %s" % (
        tid, head[:12], len([1 for row in pr.get("aiReviewLog") or [] for _ in row.get("findings") or []]), url))


def _jira_lines(data: dict, ticket: dict, body: str, head: str) -> list:
    try:
        import jira_sync

        beam_path = Path(data.get("_agentBeam") or (Path(data.get("_repoRoot") or ".") / ".warp" / "beam.json"))
        cfg = jira_sync.settings(beam_path, data)
        action = jira_sync._comment_action(ticket, "jira", "ai-review-%s" % head[:7], body)
        if not action:
            return []
        text = jira_sync.render(ticket, [action], cfg)
        return [line for line in text.splitlines() if line.strip()] if text else []
    except Exception as exc:  # Jira is best effort here; the pull request comment carries the same text
        return ["jira: skipped AI review comment for %s (%s)" % (ticket.get("id"), exc)]
