#!/usr/bin/env python3
"""How Warp gets Bugbot to review a pull request, and what it does when Bugbot never starts.

`bugbotTrigger` in .warp/config.yaml:

  auto     (default) Bugbot reviews each pull request on its own. Warp waits for its
           check. If no Bugbot check shows up on the head within `bugbotStartMinutes`
           (default 10) of the request, Warp sets `bugbotTrigger: comment` in
           .warp/config.yaml (published with the run state), posts `bugbot run` on that
           pull request, and asks the same way on every pull request after it.
  comment  Warp posts `bugbot run` on the pull request each time it asks for a review
           (once per head commit). If Bugbot still has not started `bugbotStartMinutes`
           after a comment, Warp comments once more. After that it posts one Herald line
           saying Bugbot is not answering, and keeps waiting.

`orchestrator.py supervise` posts the comment itself with `gh pr comment` when gh is
logged in. Without gh it prints a MUST DO line for the GitHub MCP, then:

  python3 scripts/bugbot.py run --beam .warp/beam.json --id WV-01            post now (gh) and record it
  python3 scripts/bugbot.py run --beam .warp/beam.json --id WV-01 --posted   record a comment posted another way
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
TRIGGERS = ("auto", "comment")
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
    if value in {"manual", "comment", "true", "yes", "on"}:
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


def on_request(data: dict, ticket: dict, lines: list) -> None:
    """Called when Warp asks Bugbot for a new head. Comment mode posts `bugbot run` now."""
    if trigger(data) == "comment" and comments_on_head(_pr(ticket)) == 0:
        ask(ticket, lines)


def switch_to_comment(data: dict, ticket: dict, minutes: int, root=None) -> list:
    """Bugbot did not start on its own: write `bugbotTrigger: comment` and say so once."""
    import runs

    cfg = data.setdefault("config", {})
    cfg[TRIGGER_KEY] = "comment"
    where = _root(data, root)
    wrote = False
    if where is not None:
        path = Path(where) / ".warp" / "config.yaml"
        if path.is_file():
            text = path.read_text()
            updated = runs.set_key(text, TRIGGER_KEY, "comment")
            if updated != text:
                path.write_text(updated)
                wrote = True
    url = _pr(ticket).get("url") or ""
    tid = ticket.get("id")
    lines = [
        "config: %s comment. Bugbot did not start on its own for %s (%s) within %d minutes. "
        "Warp now posts `%s` on each pull request when it asks for a review.%s"
        % (TRIGGER_KEY, tid, url, minutes, COMMENT_BODY,
           " .warp/config.yaml was updated and is published with the run state." if wrote else ""),
        "herald: Bugbot did not start on its own on %s. Warp now asks it with a `%s` comment on each pull request (%s: comment)."
        % (tid, COMMENT_BODY, TRIGGER_KEY),
    ]
    return lines


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
    if trigger(data) == "auto":
        lines.extend(switch_to_comment(data, ticket, minutes))
        ask(ticket, lines)
        return True
    if count < MAX_COMMENTS:
        ask(ticket, lines)
        return True
    if str(pr.get("bugbotSilentSha") or "") != head:
        pr["bugbotSilentSha"] = head
        pr["bugbotSilentAt"] = now_s
        repo = ""
        match = re.search(r"github\.com/([^/]+/[^/]+)/pull/", str(pr.get("url") or ""))
        if match:
            repo = " for %s" % match.group(1)
        lines.append(
            "herald: %s Bugbot has not answered `%s` on %s after %d comments. Check that Bugbot is on%s in the Cursor "
            "dashboard and that the pull request author's \"Run only when mentioned\" setting is not the cause, "
            "or set bugbotRequired: false." % (ticket.get("id"), COMMENT_BODY, pr.get("url") or "", count, repo)
        )
        lines.append("bugbot: silent %s" % ticket.get("id"))
        return True
    return False


def record(ticket: dict, now=None) -> None:
    pr = _pr(ticket)
    _now_dt, now_s = beam_mod.coerce_now(now)
    head = _head(pr)
    count = comments_on_head(pr)
    pr["bugbotCommentSha"] = head
    pr["bugbotComments"] = count + 1
    pr["bugbotCommentAt"] = now_s
    pr.pop("bugbotCommentPending", None)


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
            record(tickets[tid], now=now)
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
    record(ticket)
    beam_mod.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
    print("bugbot: commented %s %s" % (tid, url))
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
    sys.exit(_cli_status(beam_path))


if __name__ == "__main__":
    main()
