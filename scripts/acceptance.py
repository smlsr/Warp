#!/usr/bin/env python3
"""A ticket's acceptance criteria: where they come from, how a Shuttle reports them, how they gate the merge.

Where they come from. A plan stores them on the ticket as `acs` or `acceptance` (a list of
strings), or, in a pack's Jira import file (`tickets/tickets.json`), as `acceptanceCriteria`
(`[{id, kind, text}]`). A build-schedule/2 schedule.json has none of its own, so `/warp-scan`
and `/warp-start`/`/warp-resume` fill each ticket's `acs` from the pack's ticket file, matched
by `tempId`, `externalId`, or `id`. A string criterion with no id is `AC-<n>` by position, or
the id in an `AC-3: ...` prefix.

How a Shuttle reports them. The prompt lists them (`acceptance: AC-1: ... | AC-2: ...`) and
asks for a result for each, on the Shuttle's last line:

  python3 scripts/ticket_state.py append --id WV-01 --state done --agent <agent> --root <parent> \\
      --ac AC-1=pass --ac AC-2=pass --ac AC-3=fail

A person or the parent records them the same way with `--state acceptance`. The parent reads
the ticket folder each pass and stores them on `pr.acResults` for the head commit.

How they gate. A ticket merges when every criterion has a `pass` for the head. A `fail` sends
it to fix with that criterion. When the work is done (Bugbot finished, checks green) and some
criteria have no result and no Shuttle is out, the parent starts a verify Shuttle (`step=verify`)
that checks each one against the branch and records it, up to 2 per head commit; after that it
asks for the results by hand.

  python3 scripts/acceptance.py show --beam .warp/beam.json [--id WV-01]
  python3 scripts/acceptance.py backfill --beam .warp/beam.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASS = {"pass", "passed", "ok", "green", "yes", "true", "done"}
FAIL = {"fail", "failed", "red", "no", "false"}
MAX_VERIFY_SHUTTLES = 2
_PREFIX = re.compile(r"^\s*(AC[-_ ]?\d+)\s*[:.)\-]\s*(.+)$", re.I)
_FIELDS = ("acs", "acceptance", "acceptanceCriteria", "acceptance_criteria", "criteria")


def raw_of(row: dict):
    """The first acceptance field a plan row has, as stored."""
    if not isinstance(row, dict):
        return None
    for key in _FIELDS:
        value = row.get(key)
        if value not in (None, "", []):
            return value
    return None


def items(ticket: dict) -> list:
    """[(id, text)] in order. Accepts strings, `AC-1: text` strings, and {id, text} objects."""
    raw = raw_of(ticket)
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [part for part in re.split(r"\s*\|\s*|\n", raw) if part.strip()]
    if not isinstance(raw, list):
        return []
    out = []
    for n, item in enumerate(raw, 1):
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("title") or item.get("description") or "").strip()
            ident = str(item.get("id") or item.get("key") or "").strip() or "AC-%d" % n
        else:
            text = str(item).strip()
            match = _PREFIX.match(text)
            if match:
                ident, text = match.group(1).upper().replace("_", "-").replace(" ", "-"), match.group(2).strip()
            else:
                ident = "AC-%d" % n
        if text:
            out.append((ident, text))
    return out


def text(ticket: dict) -> str:
    return " | ".join("%s: %s" % (ident, body) for ident, body in items(ticket))


def ids(ticket: dict) -> list:
    return [ident for ident, _body in items(ticket)]


def results(ticket: dict) -> dict:
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    raw = pr.get("acResults")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value).strip().casefold() for key, value in raw.items()}


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).casefold())


def result_for(got: dict, ident: str, body: str) -> Optional[str]:
    """A recorded result for one criterion, keyed by its id (AC-1, ac1) or by its text."""
    if ident in got:
        return got[ident]
    norm = {_norm(key): value for key, value in got.items()}
    for key in (ident, body):
        if _norm(key) in norm:
            return norm[_norm(key)]
    return None


def all_pass(ticket: dict) -> bool:
    got = results(ticket)
    return all((result_for(got, ident, body) or "") in PASS for ident, body in items(ticket))


def missing(ticket: dict) -> list:
    got = results(ticket)
    return [ident for ident, body in items(ticket) if result_for(got, ident, body) is None]


def parse_args(values) -> dict:
    """`AC-1=pass` strings from --ac. Unknown words are kept as given (a fail reason)."""
    out = {}
    for raw in values or []:
        if "=" not in str(raw):
            continue
        key, value = str(raw).split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            continue
        folded = value.casefold()
        out[key] = "pass" if folded in PASS else ("fail" if folded in FAIL else value)
    return out


# --- the pack's ticket file -------------------------------------------------------


def _rows(doc) -> list:
    if isinstance(doc, list):
        return [row for row in doc if isinstance(row, dict)]
    if isinstance(doc, dict):
        for key in ("tickets", "issues", "items"):
            if isinstance(doc.get(key), list):
                return [row for row in doc[key] if isinstance(row, dict)]
    return []


def _row_ids(row: dict) -> list:
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    out = []
    for value in (row.get("tempId"), row.get("externalId"), row.get("id"), fields.get("tempId"), row.get("key")):
        if value not in (None, "") and str(value) not in out:
            out.append(str(value))
    return out


def pack_criteria(root: Path, rels) -> tuple:
    """({plan id: criteria as stored}, [files read]) from the pack's ticket files."""
    found, used = {}, []
    for rel in rels or []:
        path = Path(root) / rel
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        hit = False
        for row in _rows(doc):
            raw = raw_of(row)
            if raw is None:
                fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
                raw = raw_of(fields)
            if raw is None:
                continue
            for ident in _row_ids(row):
                found.setdefault(ident, raw)
            hit = True
        if hit:
            used.append(str(rel))
    return found, used


def fill(tickets, criteria: dict) -> list:
    """Set `acs` on tickets that have none. Returns the ids filled."""
    filled = []
    rows = tickets.values() if isinstance(tickets, dict) else tickets
    for ticket in rows:
        if not isinstance(ticket, dict) or raw_of(ticket) is not None:
            continue
        for key in (ticket.get("id"), ticket.get("externalId")):
            if key not in (None, "") and str(key) in criteria:
                ticket["acs"] = criteria[str(key)]
                filled.append(str(ticket.get("id")))
                break
    return filled


def ticket_files(root: Path, found: Optional[dict]) -> list:
    """The ticket files the scan found, else `tickets/*.json` beside the plan folder."""
    rels = list((found or {}).get("jira") or [])
    if rels:
        return rels
    folder = (found or {}).get("folder")
    base = Path(root) / folder if folder else Path(root)
    return [str(p.relative_to(root)) for p in sorted(base.glob("**/tickets/*.json")) if p.is_file()][:10]


def backfill(beam_path: Path) -> list:
    """Fill missing criteria on the live beam from the pack (start and resume run this)."""
    import beam as beam_mod

    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return []
    data = beam_mod.load_json(beam_path)
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    if all(raw_of(t) is not None for t in tickets.values() if isinstance(t, dict)):
        return []
    root = beam_path.resolve().parent.parent
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    found = source.get("scan") if isinstance(source.get("scan"), dict) else None
    criteria, used = pack_criteria(root, ticket_files(root, found))
    filled = fill(tickets, criteria)
    if not filled:
        return []
    beam_mod.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
    return ["acceptance: criteria added to %d tickets from %s (%s)" % (len(filled), ", ".join(used), ", ".join(filled[:12]) + (" ..." if len(filled) > 12 else ""))]


def summary_lines(tickets) -> list:
    rows = [t for t in (tickets.values() if isinstance(tickets, dict) else tickets) if isinstance(t, dict)]
    none = [str(t.get("id")) for t in rows if not items(t)]
    lines = ["acceptance: %d of %d tickets have criteria" % (len(rows) - len(none), len(rows))]
    if none:
        lines.append("acceptance: no criteria for %s. Those tickets cannot merge until a result is recorded for them; "
                     "add acceptanceCriteria to the pack's tickets.json (or acs to the plan) and rescan." % ", ".join(none[:12]))
    return lines


# --- prompt -------------------------------------------------------------------------


def prompt_lines(ticket: dict, record: str) -> list:
    """What the Shuttle does with the criteria. `record` is its done command without --ac."""
    listed = items(ticket)
    if not listed:
        return []
    acs = " ".join("--ac %s=pass|fail" % ident for ident, _body in listed)
    return [
        "Acceptance criteria. Check each one against your branch before you finish (run what it says to run):",
        *["  %s: %s" % (ident, body) for ident, body in listed],
        "Record a result for every one on your last line, pass only when you saw it pass:",
        "%s %s" % (record, acs),
    ]


def verify_detail(ticket: dict, scripts: str, parent: str) -> str:
    listed = items(ticket)
    want = " ".join("--ac %s=pass|fail" % ident for ident, _body in listed)
    return (
        "verify only: do not change code. Check out %s and check each acceptance criterion against it, running "
        "what it says to run: %s. Then record: python3 %s/ticket_state.py append --root %s --id %s --state done "
        "--agent <agent> %s"
        % (ticket.get("branch") or "the branch", " | ".join("%s: %s" % pair for pair in listed), scripts, parent,
           ticket.get("id"), want)
    )


def verify_shuttles_on_head(pr: dict) -> int:
    head = str(pr.get("headSha") or "")
    if str(pr.get("acVerifySha") or "") != head:
        return 0
    try:
        return int(pr.get("acVerifies") or 0)
    except (TypeError, ValueError):
        return 0


def note_verify_shuttle(ticket: dict) -> int:
    pr = ticket.setdefault("pr", {})
    count = verify_shuttles_on_head(pr) + 1
    pr["acVerifySha"] = str(pr.get("headSha") or "")
    pr["acVerifies"] = count
    return count


def ask_by_hand(ticket: dict, lines: list, beam_file: str) -> None:
    pr = ticket.setdefault("pr", {})
    head = str(pr.get("headSha") or "")
    if head and str(pr.get("acAskedSha") or "") == head:
        return
    pr["acAskedSha"] = head
    tid = ticket.get("id")
    scripts = Path(__file__).resolve().parent
    want = " ".join("--ac %s=pass|fail" % ident for ident in missing(ticket))
    lines.append(
        "acceptance: MUST DO %s: no result recorded for %s after %d verify Shuttles. Check them on %s and record: "
        "python3 %s/ticket_state.py append --root %s --id %s --state acceptance %s"
        % (tid, ", ".join(missing(ticket)), MAX_VERIFY_SHUTTLES, pr.get("url") or ticket.get("branch") or "the branch",
           scripts, Path(beam_file).resolve().parent.parent, tid, want)
    )
    lines.append("herald: %s needs its acceptance criteria checked by hand: %s." % (tid, ", ".join(missing(ticket))))


# --- command line -------------------------------------------------------------------


HELP = __doc__.split("\n\n", 1)[1] + "\n?, help, -h, and --help print this text. Quote ? if the shell expands it.\n"


def main() -> None:
    p = argparse.ArgumentParser(description="Acceptance criteria", epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("show", help="each ticket's criteria and the results recorded")
    ps.add_argument("--beam", default=".warp/beam.json")
    ps.add_argument("--id")
    pb = sub.add_parser("backfill", help="fill missing criteria from the pack's ticket file")
    pb.add_argument("--beam", default=".warp/beam.json")
    import usage

    args = p.parse_args(usage.normalize_argv(None))
    beam_path = Path(args.beam)
    if not beam_path.is_file():
        print("acceptance: no beam at %s" % beam_path)
        sys.exit(1)
    if args.cmd == "backfill":
        for line in backfill(beam_path) or ["acceptance: nothing to add"]:
            print(line)
        return
    data = json.loads(beam_path.read_text())
    for tid, ticket in (data.get("tickets") or {}).items():
        if args.id and tid != args.id:
            continue
        got = results(ticket)
        listed = items(ticket)
        if not listed:
            print("%s: no criteria" % tid)
            continue
        for ident, body in listed:
            print("%s %s %s: %s" % (tid, got.get(ident, "-"), ident, body))


if __name__ == "__main__":
    main()
