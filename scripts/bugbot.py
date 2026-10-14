#!/usr/bin/env python3
"""How Warp gets Bugbot to review a pull request, and what it does when Bugbot never starts.

`bugbotTrigger` in .warp/config.yaml:

  auto     (default) Bugbot reviews each new pull request and push on its own. Warp waits
           for its `Cursor Bugbot` check. If none shows up on the head within
           `bugbotStartMinutes` (default 10) of the request, Warp sets
           `bugbotTrigger: comment` (published with the run state) and posts `bugbot run`.
  comment  The Cursor Bugbot app is on the repo and hears pull request comments. Warp posts
           `bugbot run` each time it asks for a review, once per head commit. No answer
           after `bugbotStartMinutes`: one more comment. No answer to two: Bugbot is not
           listening, so Warp sets `bugbotTrigger: manual` and asks by hand.
  manual   Bugbot cannot be started from here: the provider is not connected to Cursor, or
           nothing hears comments. Warp asks once per head commit (a MUST DO line and one
           Herald line) for a Bugbot review done by hand, and the ticket waits in review
           until someone records the result with `bugbot.py record`.
  shuttle  The Shuttle runs the review. Once its pull request is open and its check is
           green, the Shuttle reviews its own diff the way Bugbot does, fixes what it finds,
           and records the result in its ticket folder (`ticket_state.py append --state
           review-pass|review-fail --sha <head>`). When the work is done and no review is
           recorded for the head, with no Shuttle out (it returned without one, stalled, or
           is stuck), the parent starts a review-only Shuttle (`step=review`), up to
           2 per head commit; after that it asks for a review by hand.

`bugbotManual` is a different key: whether manual-merge tickets (autoMerge false) need
Bugbot at all. It does not change how Bugbot is started.

`orchestrator.py supervise` posts comments itself with `gh pr comment` when gh is logged
in, and writes each posted comment to the beam on disk at once. Without gh it prints a
MUST DO line for the GitHub MCP, then:

  python3 scripts/bugbot.py run --beam .warp/beam.json --id WV-01            post now (gh) and record it
  python3 scripts/bugbot.py run --beam .warp/beam.json --id WV-01 --posted   record a comment posted another way
  python3 scripts/bugbot.py record --beam .warp/beam.json --id WV-01 --result pass
  python3 scripts/bugbot.py record --beam .warp/beam.json --id WV-01 --result fail --finding "null check in util.mjs"
  python3 scripts/bugbot.py status --beam .warp/beam.json                    trigger, and each ticket waiting on Bugbot
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beam as beam_mod  # noqa: E402

TRIGGER_KEY = "bugbotTrigger"
START_KEY = "bugbotStartMinutes"
COMMENT_BODY = "bugbot run"
DEFAULT_START_MINUTES = 10
MAX_COMMENTS = 2
TRIGGERS = ("auto", "comment", "manual", "shuttle")
MAX_REVIEW_SHUTTLES = 2
_LINE = re.compile(r"^bugbot: comment (\S+) (\S+)")


def _root(data: dict, root=None) -> Optional[Path]:
    if root:
        return Path(root)
    raw = data.get("_repoRoot") if isinstance(data, dict) else None
    return Path(raw) if raw else None


def _yaml_value(root: Optional[Path], key: str) -> Optional[str]:
    if root is None:
        return None
    path = Path(root) / ".warp" / "config.yaml"
    if not path.is_file():
        return None
    import runs

    try:
        return runs.read_key(path.read_text(), key)
    except OSError:
        return None


def trigger(data: dict, root=None) -> str:
    """`auto` or `comment`. The yaml wins over the beam's copy."""
    raw = _yaml_value(_root(data, root), TRIGGER_KEY)
    if raw in (None, ""):
        raw = ((data or {}).get("config") or {}).get(TRIGGER_KEY)
    value = str(raw or "auto").strip().casefold()
    if value in {"manual", "hand", "person"}:
        return "manual"
    if value in {"shuttle", "agent", "review", "self"}:
        return "shuttle"
    if value in {"comment", "true", "yes", "on"}:
        return "comment"
    return "auto"


def start_minutes(data: dict, root=None) -> int:
    raw = _yaml_value(_root(data, root), START_KEY)
    if raw in (None, ""):
        raw = ((data or {}).get("config") or {}).get(START_KEY)
    try:
        value = int(str(raw).strip()) if raw not in (None, "") else DEFAULT_START_MINUTES
    except ValueError:
        value = DEFAULT_START_MINUTES
    return value if value > 0 else DEFAULT_START_MINUTES


def _pr(ticket: dict) -> dict:
    pr = ticket.get("pr")
    if not isinstance(pr, dict):
        pr = {}
        ticket["pr"] = pr
    return pr


def answered(ticket: dict) -> bool:
    """Bugbot has a check on this head: pending, pass, or fail."""
    import pipeline

    return pipeline.bugbot_state(ticket) not in {"", "none", "null"}


def _head(pr: dict) -> str:
    return str(pr.get("headSha") or pr.get("bugbotRequestedSha") or "").strip()


def comments_on_head(pr: dict) -> int:
    if str(pr.get("bugbotCommentSha") or "") != _head(pr):
        return 0
    try:
        return int(pr.get("bugbotComments") or 0)
    except (TypeError, ValueError):
        return 0


def ask(ticket: dict, lines: list) -> None:
    """Queue one `bugbot run` comment for this head. Supervise posts it."""
    pr = _pr(ticket)
    pr["bugbotCommentPending"] = _head(pr)
    lines.append("bugbot: comment %s %s" % (ticket.get("id"), pr.get("url") or ""))


def ask_manual(data: dict, ticket: dict, lines: list) -> None:
    """Bugbot cannot be started from here. Ask once per head for a review recorded by hand."""
    pr = _pr(ticket)
    head = _head(pr)
    if head and str(pr.get("bugbotManualAsked") or "") == head:
        return
    pr["bugbotManualAsked"] = head
    tid = ticket.get("id")
    url = pr.get("url") or ""
    branch = ticket.get("branch") or ""
    beam_file = data.get("_agentBeam") or (str(Path(data["_repoRoot"]) / ".warp" / "beam.json") if data.get("_repoRoot") else ".warp/beam.json")
    scripts = Path(__file__).resolve().parent
    lines.append("bugbot: manual %s %s branch=%s" % (tid, url, branch))
    lines.append(
        "bugbot: MUST DO %s: Bugbot cannot be started from here (%s: manual). Get a Bugbot review of %s "
        "(branch %s, head %s) the way this setup allows, then record it: python3 %s/bugbot.py record --beam %s "
        "--id %s --result pass|fail [--finding \"<what is wrong>\" ...]. The ticket waits in review until it is recorded."
        % (tid, TRIGGER_KEY, url or "the pull request", branch, head[:12] or "?", scripts, beam_file, tid)
    )
    lines.append("herald: %s needs a Bugbot review by hand: %s. It waits in review until the result is recorded." % (tid, url or branch))


def on_request(data: dict, ticket: dict, lines: list) -> None:
    """Called when Warp asks Bugbot for a new head. comment posts `bugbot run`; manual asks for a review by hand."""
    mode = trigger(data)
    if mode == "comment" and comments_on_head(_pr(ticket)) == 0:
        ask(ticket, lines)
    elif mode == "manual":
        ask_manual(data, ticket, lines)
    # shuttle: the Shuttle reviews its own work; the parent starts a review Shuttle if it did not.


def set_trigger(data: dict, value: str, root=None) -> bool:
    """Write `bugbotTrigger: <value>` to the beam copy and .warp/config.yaml. True when the yaml changed."""
    import runs

    cfg = data.setdefault("config", {})
    cfg[TRIGGER_KEY] = value
    where = _root(data, root)
    if where is None:
        return False
    path = Path(where) / ".warp" / "config.yaml"
    if not path.is_file():
        return False
    text = path.read_text()
    updated = runs.set_key(text, TRIGGER_KEY, value)
    if updated == text:
        return False
    path.write_text(updated)
    return True


_PUBLISHED = " .warp/config.yaml was updated and is published with the run state."


def switch_to_comment(data: dict, ticket: dict, minutes: int, root=None) -> list:
    """Bugbot did not start on its own: write `bugbotTrigger: comment` and say so once."""
    wrote = set_trigger(data, "comment", root)
    url = _pr(ticket).get("url") or ""
    tid = ticket.get("id")
    return [
        "config: %s comment. Bugbot did not start on its own for %s (%s) within %d minutes. "
        "Warp now posts `%s` on each pull request when it asks for a review.%s"
        % (TRIGGER_KEY, tid, url, minutes, COMMENT_BODY, _PUBLISHED if wrote else ""),
        "herald: Bugbot did not start on its own on %s. Warp now asks it with a `%s` comment on each pull request (%s: comment)."
        % (tid, COMMENT_BODY, TRIGGER_KEY),
    ]


def switch_to_manual(data: dict, ticket: dict, count: int, root=None) -> list:
    """`bugbot run` comments went unanswered: Bugbot is not listening on this repo."""
    wrote = set_trigger(data, "manual", root)
    pr = _pr(ticket)
    url = pr.get("url") or ""
    repo = ""
    match = re.search(r"(?:github\.com|bitbucket\.org)/([^/]+/[^/]+)/", str(url))
    if match:
        repo = " on %s" % match.group(1)
    return [
        "config: %s manual. Bugbot did not answer %d `%s` comments on %s, so it is not listening%s "
        "(the Cursor Bugbot app is not on the repo, or the author's \"Run only when mentioned\" setting). "
        "Each review is now asked for and recorded by hand.%s"
        % (TRIGGER_KEY, count, COMMENT_BODY, url, repo, _PUBLISHED if wrote else ""),
        "herald: Bugbot is not answering `%s`%s. Warp now asks for each Bugbot review by hand (%s: manual). "
        "Set it back to auto once Bugbot is on for the repo." % (COMMENT_BODY, repo, TRIGGER_KEY),
        "bugbot: silent %s" % ticket.get("id"),
    ]


def _age_minutes(stamp, now_dt) -> Optional[float]:
    seen = beam_mod.parse_ts(str(stamp or "")) if stamp else None
    if seen is None or now_dt is None:
        return None
    return max(0.0, (now_dt - seen).total_seconds() / 60.0)


def check_silence(data: dict, ticket: dict, lines: list, now=None) -> bool:
    """A requested Bugbot that never started. Returns True when it queued a comment or warned."""
    pr = _pr(ticket)
    head = _head(pr)
    if not pr.get("bugbotRequested") or answered(ticket):
        return False
    if head and str(pr.get("bugbotRequestedSha") or head) != head:
        return False
    mode = trigger(data)
    if mode == "shuttle":
        if not shuttle_reviewed(pr):
            return False  # pipeline starts a review Shuttle; nothing to post
        # The Shuttle reviewed this head and Cursor Bugbot has not started on it: ask Bugbot with a comment.
        mode = "comment"
    if mode == "manual":
        # Nothing to wait for: a person records the result. Ask once per head.
        before = len(lines)
        ask_manual(data, ticket, lines)
        return len(lines) > before
    now_dt, now_s = beam_mod.coerce_now(now)
    count = comments_on_head(pr)
    since = pr.get("bugbotCommentAt") if count else pr.get("bugbotRequestedAt")
    age = _age_minutes(since, now_dt)
    minutes = start_minutes(data)
    if age is None or age < minutes:
        return False
    if str(pr.get("bugbotCommentPending") or "") == head and head:
        # Asked already this head and not posted yet: say it again so it gets posted.
        lines.append("bugbot: comment %s %s" % (ticket.get("id"), pr.get("url") or ""))
        return True
    if mode == "auto":
        lines.extend(switch_to_comment(data, ticket, minutes))
        ask(ticket, lines)
        return True
    if count < MAX_COMMENTS:
        ask(ticket, lines)
        return True
    pr["bugbotSilentSha"] = head
    pr["bugbotSilentAt"] = now_s
    lines.extend(switch_to_manual(data, ticket, count))
    ask_manual(data, ticket, lines)
    return True


def record(ticket: dict, now=None) -> dict:
    """Count one posted comment on this head. Returns the fields it set."""
    pr = _pr(ticket)
    _now_dt, now_s = beam_mod.coerce_now(now)
    head = _head(pr)
    count = comments_on_head(pr)
    fields = {"bugbotCommentSha": head, "bugbotComments": count + 1, "bugbotCommentAt": now_s}
    pr.update(fields)
    pr.pop("bugbotCommentPending", None)
    return fields


def persist(beam_path: Path, tid: str, fields: dict, drop: tuple = ()) -> None:
    """Write pull request fields to the beam on disk now, under the beam lock.

    A comment is on GitHub the moment gh returns. If the pass is then aborted (a pause or a
    stop landed meanwhile), the in-memory beam is not what gets saved; this record must be.
    """
    import fcntl

    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return
    lock_path = beam_path.parent / ".bugbot.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = beam_mod.load_json(beam_path)
            ticket = (data.get("tickets") or {}).get(tid)
            if not isinstance(ticket, dict):
                return
            pr = _pr(ticket)
            pr.update(fields)
            for key in drop:
                pr.pop(key, None)
            beam_mod.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def record_result(ticket: dict, result: str, findings: Optional[list] = None, evidence: str = "", now=None,
                  source: str = "manual", sha: str = "") -> dict:
    """A Bugbot review recorded by hand or by a Shuttle for a head commit. Returns the fields it set."""
    pr = _pr(ticket)
    _now_dt, now_s = beam_mod.coerce_now(now)
    head = str(sha or "").strip() or _head(pr)
    result = "pass" if str(result).strip().casefold() == "pass" else "fail"
    found = [str(item).strip() for item in (findings or []) if str(item).strip()]
    if result == "fail" and not found:
        found = ["Bugbot finding (recorded by hand, no detail given)"]
    fields = {
        "bugbot": result,
        "bugbotSha": head,
        "bugbotFindings": found if result == "fail" else [],
        "bugbotFindingsSha": head,
        "bugbotEvidence": evidence or ("reviewed by the Shuttle" if source == "shuttle" else "recorded by hand"),
        "bugbotSource": source,
        "bugbotRecordedSha": head,
        "bugbotRecordedAt": now_s,
    }
    if source == "shuttle":
        # The Shuttle's own review comes first. It does not stand in for Cursor Bugbot when Bugbot runs
        # on this repo: the merge still waits for Bugbot's result on the head (see pipeline._hold_for_bugbot).
        fields["shuttleReview"] = result
        fields["shuttleReviewSha"] = head
    pr.update(fields)
    return fields


def shuttle_reviewed(pr: dict) -> bool:
    """A review Shuttle recorded a result for the current head."""
    head = _head(pr)
    return bool(head) and str(pr.get("shuttleReviewSha") or "") == head


def recorded_for_head(pr: dict, head: str) -> bool:
    """A result recorded by hand or by a Shuttle for this head. A provider read with no Bugbot check keeps it."""
    return bool(head) and str(pr.get("bugbotRecordedSha") or "") == head


def apply_review(beam_path, ticket: dict, event: dict, stamp: str, lines: list, heralded: set) -> bool:
    """A review-pass or review-fail line from the ticket folder. True when the beam changed."""
    pr = _pr(ticket)
    head = str(pr.get("headSha") or "").strip()
    sha = str(event.get("sha") or "").strip()
    tid = ticket.get("id")
    if sha and head and not (head.startswith(sha) or sha.startswith(head)):
        lines.append("bugbot: review for %s ignored: it reviewed %s, the head is %s" % (tid, sha[:12], head[:12]))
        return False
    sha = head or sha
    result = "pass" if event.get("state") == "review-pass" else "fail"
    findings = [str(item) for item in (event.get("findings") or []) if str(item).strip()]
    if recorded_for_head(pr, sha) and str(pr.get("bugbot") or "") == result and list(pr.get("bugbotFindings") or []) == (findings if result == "fail" else []):
        return False
    source = str(event.get("source") or "shuttle")
    fields = record_result(ticket, result, findings, now=stamp, source=source, sha=sha)
    ticket["updatedAt"] = stamp
    key = (tid, "review-" + result)
    if key not in heralded:
        heralded.add(key)
        who = "the Shuttle" if source == "shuttle" else "hand"
        if result == "pass":
            lines.append("herald: %s Bugbot review by %s: pass." % (tid, who))
        else:
            lines.append("herald: %s Bugbot review by %s: %d finding(s). Back to fix." % (tid, who, len(fields["bugbotFindings"])))
    try:
        beam_mod.append_ticket_event(Path(beam_path), ticket, {"at": stamp, "type": "bugbot", "value": result, "source": source, "sha": sha})
    except Exception:
        pass
    return True


def review_shuttles_on_head(pr: dict) -> int:
    if str(pr.get("bugbotReviewSha") or "") != _head(pr):
        return 0
    try:
        return int(pr.get("bugbotReviews") or 0)
    except (TypeError, ValueError):
        return 0


def note_review_shuttle(ticket: dict) -> int:
    """Count one review Shuttle on this head. Returns the count."""
    pr = _pr(ticket)
    count = review_shuttles_on_head(pr) + 1
    pr["bugbotReviewSha"] = _head(pr)
    pr["bugbotReviews"] = count
    return count


def review_detail(ticket: dict, data: dict) -> str:
    """What the review Shuttle does. It goes on the start line as the fix detail."""
    pr = _pr(ticket)
    scripts = Path(__file__).resolve().parent
    root = data.get("_repoRoot") or "<parent checkout>"
    return (
        "review only (bugbotTrigger: shuttle): do not change code. Review the diff of %s against the base branch "
        "the way Bugbot does: real bugs, crashes, security holes, data loss, broken acceptance criteria; not style. "
        "Then record it for head %s: python3 %s/ticket_state.py append --root %s --id %s --state review-pass|review-fail "
        "--sha <head sha> --source shuttle [--finding \"<file:line what is wrong>\" ...]"
        % (pr.get("url") or ticket.get("branch") or "the branch", (_head(pr) or "?")[:12], scripts, root, ticket.get("id"))
    )


def post(root: Path, url: str, runner: Optional[Callable] = None) -> tuple:
    """Post `bugbot run` on the pull request with gh. (ok, detail)."""
    if not url:
        return False, "no pull request url"
    if runner is None:
        if not shutil.which("gh"):
            return False, "gh is not installed"

        def runner(cmd):
            return subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=30)

    proc = runner(["gh", "pr", "comment", str(url), "--body", COMMENT_BODY])
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "gh pr comment failed").strip().splitlines()[0]
    return True, (proc.stdout or "").strip()


def must_do(tid: str, url: str, beam_path: Path, detail: str = "") -> str:
    scripts = Path(__file__).resolve().parent
    why = " (%s)" % detail if detail else ""
    return (
        "bugbot: MUST DO post a pull request comment with the body `%s` on %s%s: gh pr comment, or the GitHub MCP "
        "add_issue_comment. Then run python3 %s/bugbot.py run --beam %s --id %s --posted"
        % (COMMENT_BODY, url, why, scripts, Path(beam_path).resolve(), tid)
    )


def post_pending(data: dict, beam_path: Path, lines: list, runner: Optional[Callable] = None, now=None) -> list:
    """Post each `bugbot: comment` this pass printed. Returns the result lines."""
    out = []
    seen = set()
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    root = Path(beam_path).resolve().parent.parent
    for line in list(lines):
        match = _LINE.match(line)
        if not match:
            continue
        tid, url = match.group(1), match.group(2)
        if tid in seen or tid not in tickets:
            continue
        seen.add(tid)
        ok, detail = post(root, url, runner=runner)
        if ok:
            fields = record(tickets[tid], now=now)
            # On disk at once: an aborted pass must not drop a comment that is already posted.
            persist(beam_path, tid, fields, drop=("bugbotCommentPending",))
            out.append("bugbot: commented %s %s" % (tid, url))
        else:
            out.append(must_do(tid, url, beam_path, detail))
    return out


def _cli_run(beam_path: Path, tid: str, posted: bool) -> int:
    data = beam_mod.load_json(beam_path)
    ticket = (data.get("tickets") or {}).get(tid)
    if not isinstance(ticket, dict):
        print("bugbot: unknown %s" % tid)
        return 1
    url = _pr(ticket).get("url") or ""
    if not posted:
        ok, detail = post(beam_path.resolve().parent.parent, url)
        if not ok:
            print(must_do(tid, url, beam_path, detail))
            return 1
    fields = record(ticket)
    persist(beam_path, tid, fields, drop=("bugbotCommentPending",))
    print("bugbot: commented %s %s" % (tid, url))
    return 0


def _cli_record(beam_path: Path, tid: str, result: str, findings: list, evidence: str) -> int:
    data = beam_mod.load_json(beam_path)
    ticket = (data.get("tickets") or {}).get(tid)
    if not isinstance(ticket, dict):
        print("bugbot: unknown %s" % tid)
        return 1
    if not _pr(ticket).get("url") and not _pr(ticket).get("headSha"):
        print("bugbot: %s has no pull request yet. Nothing recorded." % tid)
        return 1
    fields = record_result(ticket, result, findings, evidence, source="hand")
    persist(beam_path, tid, fields)
    try:
        # Also in the ticket folder: a parent pass already holding the beam re-applies it from there.
        import contextlib
        import io

        import ticket_state

        with contextlib.redirect_stdout(io.StringIO()):
            ticket_state.append_status(beam_path.resolve().parent.parent, tid,
                                       "review-pass" if fields["bugbot"] == "pass" else "review-fail",
                                       findings=fields["bugbotFindings"], sha=fields["bugbotSha"], source="hand")
    except Exception:
        pass
    head = fields["bugbotSha"][:12] or "?"
    if fields["bugbot"] == "pass":
        print("bugbot: recorded %s pass on %s. The next pass checks the rest and merges when it can." % (tid, head))
    else:
        print("bugbot: recorded %s fail on %s with %d finding(s). The next pass sends it back to fix." % (tid, head, len(fields["bugbotFindings"])))
    return 0


def _cli_status(beam_path: Path) -> int:
    data = beam_mod.load_json(beam_path)
    data["_repoRoot"] = str(beam_path.resolve().parent.parent)
    print("bugbot: trigger %s, start %d minutes" % (trigger(data), start_minutes(data)))
    for tid, ticket in (data.get("tickets") or {}).items():
        if not isinstance(ticket, dict):
            continue
        pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
        if not pr.get("bugbotRequested") or answered(ticket) or ticket.get("status") in {"merged", "done", "parked"}:
            continue
        print("bugbot: waiting %s %s requested %s comments %d" % (
            tid, pr.get("url") or "", pr.get("bugbotRequestedAt") or "?", comments_on_head(pr)))
    return 0


HELP = __doc__.split("\n\n", 1)[1] + "\n?, help, -h, and --help print this text. Quote ? if the shell expands it.\n"


def main() -> None:
    p = argparse.ArgumentParser(description="Bugbot trigger", epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run", help="post `bugbot run` on a ticket's pull request and record it")
    pr.add_argument("--beam", default=".warp/beam.json")
    pr.add_argument("--id", required=True)
    pr.add_argument("--posted", action="store_true", help="the comment was posted another way: record it only")
    prc = sub.add_parser("record", help="record a Bugbot review done by hand for the ticket's current head")
    prc.add_argument("--beam", default=".warp/beam.json")
    prc.add_argument("--id", required=True)
    prc.add_argument("--result", required=True, choices=["pass", "fail"])
    prc.add_argument("--finding", action="append", default=[], help="one finding; repeat for each")
    prc.add_argument("--evidence", default="", help="where the review is: a link or a note")
    ps = sub.add_parser("status", help="the trigger and each ticket waiting on Bugbot")
    ps.add_argument("--beam", default=".warp/beam.json")
    import usage

    args = p.parse_args(usage.normalize_argv(None))
    beam_path = Path(args.beam)
    if not beam_path.is_file():
        print("bugbot: no beam at %s" % beam_path)
        sys.exit(1)
    if args.cmd == "run":
        sys.exit(_cli_run(beam_path, args.id, bool(args.posted)))
    if args.cmd == "record":
        sys.exit(_cli_record(beam_path, args.id, args.result, args.finding, args.evidence))
    sys.exit(_cli_status(beam_path))


if __name__ == "__main__":
    main()
