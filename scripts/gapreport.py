#!/usr/bin/env python3
"""GAP-REPORT.md merges by gap id, a check before any push, and a one-time cleanup (1.7.19).

1.7.15 listed GAP-REPORT.md as a "union" file and resolved its conflicts by keeping both sides line by line. A gap
report is a table whose rows pull requests edit in place, for example to close a gap. So a union merge kept the old
open row next to the new closed one, and two rows that differed only slightly both survived. On Forge-QA one merge of
#1451 added 23 bad rows. Main was left with 5 duplicated gaps, and 11 closed gaps that still had a stale open copy.

Now:
  merge3(base, ours, theirs)  rows are matched by gap id (the first cell) and merged cell by cell against the base.
                              One side changed a cell: that change wins. Both sides changed it the same way: once.
                              Both changed it differently: a closing status wins over an open one; anything else is a
                              real conflict, and the owner gets a REFRESH. A row one side deleted and the other left
                              alone is deleted. New rows from either side are kept once, in place. Text outside the
                              tables merges the same way, line by line.
  problems(text)              duplicate gap ids, an id that is both open and closed, near-duplicate rows (same gap
                              under two spellings of the id), and leftover conflict markers. The lane runs this on the
                              merged file and refuses to push, or to merge a pull request, when it finds any.
  dedupe(text)                the one-time cleanup: one row per gap id. A closed copy wins over an open one, and
                              among copies of the same status the last (newest) one wins.

  python3 scripts/gapreport.py check  docs/spec/GAP-REPORT.md        exit 1 when it has problems
  python3 scripts/gapreport.py dedupe docs/spec/GAP-REPORT.md        print what it would remove
  python3 scripts/gapreport.py dedupe docs/spec/GAP-REPORT.md --write
  python3 scripts/gapreport.py merge  BASE OURS THEIRS               the merged file on stdout (git merge driver form)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*$")
_CLOSED = re.compile(r"^[\W_]*(closed|done|fixed|resolved|merged|landed|won'?t[ -]?do|duplicate)\b", re.I)
_OPEN = re.compile(r"^[\W_]*(open|todo|to do|in progress|wip|pending|blocked|new)\b", re.I)
_MARKER = re.compile(r"^(<{7}|={7}|>{7}|\|{7})( |$)", re.M)


def is_gap_report(path: str) -> bool:
    return Path(str(path or "")).name.upper() == "GAP-REPORT.MD"


class Conflict(Exception):
    """Both sides changed the same cell of the same gap in different ways."""


def _cells(line: str) -> list:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def row_id(line: str) -> str:
    """The gap id of a table row, or "" for a header, separator or non-table line."""
    if not line.lstrip().startswith("|"):
        return ""
    cells = _cells(line)
    if not cells or set("".join(cells)) <= set("-: "):
        return ""
    first = cells[0].strip("*` ")
    if not _ID.match(first) or first in {"ID", "GAP"} or not re.search(r"\d", first):
        return ""
    return first


def status_of(line: str) -> str:
    """closed, open, or "" from the row's cells."""
    cells = _cells(line)[1:]
    if any(_CLOSED.match(c) for c in cells):
        return "closed"
    if any(_OPEN.match(c) for c in cells):
        return "open"
    return ""


def _render(cells: list, like: str) -> str:
    lead = like[: len(like) - len(like.lstrip())]
    return lead + "| " + " | ".join(cells) + " |"


def _merge_row(base: Optional[str], ours: Optional[str], theirs: Optional[str], gid: str) -> Optional[str]:
    """One gap's row from the three sides; None means deleted."""
    if ours == theirs:
        return ours
    if base is not None and ours == base:
        return theirs
    if base is not None and theirs == base:
        return ours
    if ours is None or theirs is None:
        # Deleted on one side and changed on the other: keep the change.
        return ours if ours is not None else theirs
    so, st = status_of(ours), status_of(theirs)
    b, o, t = (_cells(base) if base is not None else None), _cells(ours), _cells(theirs)
    if len(o) != len(t) or (b is not None and len(b) != len(o)):
        if so != st and "closed" in {so, st}:
            return ours if so == "closed" else theirs
        raise Conflict("%s: both sides changed the row" % gid)
    out = []
    for i in range(len(o)):
        bc = b[i] if b is not None else None
        if o[i] == t[i]:
            out.append(o[i])
        elif bc is not None and o[i] == bc:
            out.append(t[i])
        elif bc is not None and t[i] == bc:
            out.append(o[i])
        elif _CLOSED.match(o[i]) and not _CLOSED.match(t[i]):
            out.append(o[i])
        elif _CLOSED.match(t[i]) and not _CLOSED.match(o[i]):
            out.append(t[i])
        else:
            raise Conflict("%s: both sides changed column %d (%r vs %r)" % (gid, i + 1, o[i], t[i]))
    return _render(out, ours)


def _index(lines: list) -> dict:
    out: dict = {}
    for line in lines:
        gid = row_id(line)
        if gid and gid not in out:
            out[gid] = line
    return out


def _merge_plain(base: list, ours: list, theirs: list) -> list:
    """Non-row lines: one side's change wins; both changed: ours, plus lines only theirs added."""
    if ours == theirs or theirs == base:
        return ours
    if ours == base:
        return theirs
    have = set(ours)
    return ours + [line for line in theirs if line not in have and line not in set(base)]


def merge3(base: str, ours: str, theirs: str) -> str:
    """The merged report. Raises Conflict for a real conflict. Rows keep `ours` order; rows only `theirs` has are
    placed after the row they follow there."""
    b_lines, o_lines, t_lines = (str(base or "").splitlines(), str(ours or "").splitlines(), str(theirs or "").splitlines())
    b_rows, o_rows, t_rows = _index(b_lines), _index(o_lines), _index(t_lines)
    merged_rows = {}
    for gid in list(dict.fromkeys(list(o_rows) + list(t_rows) + list(b_rows))):
        merged_rows[gid] = _merge_row(b_rows.get(gid), o_rows.get(gid), t_rows.get(gid), gid)
    # Placement: walk ours; after each row, insert theirs' new rows that follow it in theirs.
    t_after: dict = {}
    prev = ""
    for line in t_lines:
        gid = row_id(line)
        if gid:
            if gid not in o_rows and gid not in b_rows:
                t_after.setdefault(prev, []).append(gid)
            prev = gid
    out: list = []
    placed = set()
    plain_o = [l for l in o_lines if not row_id(l)]
    plain_merged = _merge_plain([l for l in b_lines if not row_id(l)], plain_o, [l for l in t_lines if not row_id(l)])
    extra_plain = [l for l in plain_merged if l not in set(plain_o)]
    removed_plain = set(plain_o) - set(plain_merged)

    def put_new(after: str) -> None:
        for gid in t_after.get(after, []):
            if gid not in placed and merged_rows.get(gid) is not None:
                out.append(merged_rows[gid])
                placed.add(gid)
                put_new(gid)

    put_new("")
    for line in o_lines:
        gid = row_id(line)
        if not gid:
            if line not in removed_plain:
                out.append(line)
            continue
        if gid in placed:
            continue
        if merged_rows.get(gid) is not None:
            out.append(merged_rows[gid])
        placed.add(gid)
        put_new(gid)
    for gid, row in merged_rows.items():
        if gid not in placed and row is not None:
            out.append(row)  # a row only theirs had, whose neighbour is gone
            placed.add(gid)
    out.extend(extra_plain)
    trailing = "\n" if any(str(x or "").endswith("\n") for x in (ours, theirs, base)) else ""
    return "\n".join(out) + trailing


def _norm_summary(line: str) -> str:
    cells = _cells(line)[1:]
    text = next((c for c in cells if len(c) > 12), " ".join(cells))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def problems(text: str) -> list:
    """Duplicate ids, open+closed copies, near-duplicates under two spellings, conflict markers."""
    out = []
    if _MARKER.search(str(text or "")):
        out.append("conflict markers are left in the file")
    seen: dict = {}
    for number, line in enumerate(str(text or "").splitlines(), 1):
        gid = row_id(line)
        if gid:
            seen.setdefault(gid, []).append((number, line))
    for gid, rows in seen.items():
        if len(rows) > 1:
            states = {status_of(line) for _n, line in rows}
            kind = "a stale open copy of a closed gap" if {"open", "closed"} <= states else "a duplicated gap"
            out.append("%s: %s (lines %s)" % (gid, kind, ", ".join(str(n) for n, _l in rows)))
    by_canon: dict = {}
    for gid in seen:
        canon = re.sub(r"[^A-Z0-9]", "", gid).lstrip("G")
        by_canon.setdefault(canon, []).append(gid)
    for canon, ids in by_canon.items():
        if len(ids) > 1:
            out.append("%s: the same gap under %d ids" % (" / ".join(sorted(ids)), len(ids)))
    return out


def dedupe(text: str) -> tuple:
    """(clean text, removed lines). One row per gap id: a closed copy wins over an open one; among the same status
    the last copy wins."""
    lines = str(text or "").splitlines()
    keep_at: dict = {}
    for index, line in enumerate(lines):
        gid = row_id(line)
        if not gid:
            continue
        best = keep_at.get(gid)
        if best is None:
            keep_at[gid] = index
            continue
        rank_new = 1 if status_of(line) == "closed" else 0
        rank_old = 1 if status_of(lines[best]) == "closed" else 0
        if rank_new >= rank_old:
            keep_at[gid] = index
    kept_index = {index: gid for gid, index in keep_at.items()}
    out, removed = [], []
    first_slot: dict = {}
    for index, line in enumerate(lines):
        gid = row_id(line)
        if gid and gid not in first_slot:
            first_slot[gid] = index
    for index, line in enumerate(lines):
        gid = row_id(line)
        if not gid:
            out.append(line)
        elif first_slot[gid] == index:
            out.append(lines[keep_at[gid]])  # the kept copy, at the gap's first position
            if keep_at[gid] != index:
                removed.append(line)
        else:
            if index not in kept_index:
                removed.append(line)
    trailing = "\n" if str(text or "").endswith("\n") else ""
    return "\n".join(out) + trailing, removed


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="GAP-REPORT.md merge, check and cleanup", epilog=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check")
    pc.add_argument("path")
    pd = sub.add_parser("dedupe")
    pd.add_argument("path")
    pd.add_argument("--write", action="store_true")
    pm = sub.add_parser("merge")
    pm.add_argument("base")
    pm.add_argument("ours")
    pm.add_argument("theirs")
    pm.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if args.cmd == "check":
        found = problems(Path(args.path).read_text())
        for line in found or ["gap-report: %s is clean (one row per gap)" % args.path]:
            print(("gap-report: " + line) if found else line)
        return 1 if found else 0
    if args.cmd == "dedupe":
        text = Path(args.path).read_text()
        clean, removed = dedupe(text)
        for line in removed:
            print("gap-report: remove %s" % line.strip())
        if args.write and removed:
            Path(args.path).write_text(clean)
        left = problems(clean)
        print("gap-report: %s %d duplicate or stale row%s%s" % ("removed" if args.write else "would remove", len(removed),
                                                                "" if len(removed) == 1 else "s",
                                                                ("; still: " + "; ".join(left)) if left else "; one row per gap"))
        return 1 if left else 0
    try:
        merged = merge3(Path(args.base).read_text(), Path(args.ours).read_text(), Path(args.theirs).read_text())
    except Conflict as exc:
        print("gap-report: conflict: %s" % exc, file=sys.stderr)
        return 1
    found = problems(merged)
    if found:
        print("gap-report: refused: %s" % "; ".join(found), file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(merged)
    else:
        sys.stdout.write(merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
