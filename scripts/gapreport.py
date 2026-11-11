#!/usr/bin/env python3
"""GAP-REPORT.md: a row-aware three-way merge, a merge guard, and a cleanup that never deletes a real gap (1.7.20).

The bug. Up to 1.7.18 the merge lane listed GAP-REPORT.md as a "union" file and resolved its conflicts by keeping
both sides line by line. A gap report is a table keyed by gap id that pull requests edit in place: they close a gap,
fill in evidence, or fix a cell. A union keeps an edited row as a new line, so:
  - closed gaps reopen: main's `| G-A | … | closed |` and an older branch's `| G-A | … | open |` both survive
  - rows duplicate, and each later merge that touches the region compounds it
  - the status test, /warp-status and the audit read the first row for an id, so a stale open copy above the closed
    one makes a finished gap look open, and the audit files new work for it
One lane merge of #1451 on Forge-QA produced 23 extra rows: 17 identical duplicates and 8 stale open copies.

1.7.19's cleanup made it worse. "One row per id" deleted twins (different gaps that reuse one number, such as G-1034
RUN-002/E16-S03 vs E48-S05). On Forge-QA it removed 89 rows, of which only 23 were real copies.

Now:
  gap_resolve(base, main, pr)   rows are matched by id. When main already has more than one row for an id (twins),
                                the key is (id, occurrence), so twins pair up by position. Per key:
                                  - on one side only: kept
                                  - the same on both sides (normalized): one copy
                                  - changed on one side relative to base: that side
                                  - changed on both: the row whose status is terminal (closed, fixed, done, wontfix,
                                    duplicate, resolved, merged); when both or neither are, main's row, plus any cell
                                    the PR filled that main left empty. Never two rows for one key
                                Main's order is kept; rows new on the PR are added at the end of their table, in PR
                                order. Text outside the tables is a three-way `git merge-file`; a conflict there
                                raises Conflict (the owner gets a REFRESH), and it is never unioned.
  gap_check(main, pr, merged)   findings a merge introduced (never twins already on main):
                                  - an id whose row count is not max(main's count, the PR's count)
                                  - an identical row (status cell normalized) repeated more often than on main or the PR
                                  - an open row next to a closed row of the same id that is on neither main nor the PR
  dedupe(text)                  removes only true copies (identical after normalizing whitespace and the status cell;
                                the first, or the closed one, is kept) and stale open copies of a closed row whose
                                title or evidence cell matches. A twin with different content is never deleted; with
                                rename_twins, later twins become <id>-B, <id>-C, and every reference to the old id
                                (repo, beam, Jira) is listed so the caller can update it.
  report(text)                  copies, stale copies and conflict markers are problems; twins are their own kind
                                ("reused id, different gaps") and are not a failure.

  python3 scripts/lane.py gap-dedupe --report docs/spec/GAP-REPORT.md [--write] [--rename-twins]
  python3 scripts/gapreport.py check  docs/spec/GAP-REPORT.md
  python3 scripts/gapreport.py dedupe docs/spec/GAP-REPORT.md [--write] [--rename-twins]
  python3 scripts/gapreport.py merge  BASE MAIN PR [--out FILE]
  python3 scripts/gapreport.py restore docs/spec/GAP-REPORT.md --from <commit before the 1.7.19 cleanup> [--write]
`--write` refuses to run on main (or master): the cleanup goes in through a normal pull request.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

ROW = re.compile(r"^\| *(G[A-Za-z0-9_-]*[-0-9][A-Za-z0-9_-]*) *\|")
TERMINAL = re.compile(r"^[\W_]*(closed|fixed|done|wont ?fix|won'?t[ -]?fix|won'?t[ -]?do|duplicate|resolved|merged)\b", re.I)
OPEN = re.compile(r"^[\W_]*(open|todo|to do|in progress|wip|pending|blocked|new|reopened)\b", re.I)
_MARKER = re.compile(r"^(<{7}|={7}|>{7}|\|{7})( |$)", re.M)


class Conflict(Exception):
    """The text outside the tables conflicts; the owner merges it."""


def is_gap_report(path: str) -> bool:
    return Path(str(path or "")).name.upper() == "GAP-REPORT.MD"


# ---- rows ---------------------------------------------------------------------------------------------------------


def row_id(line: str) -> str:
    match = ROW.match(str(line or "").strip())
    return match.group(1) if match else ""


def cells(line: str) -> list:
    body = str(line or "").strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _ncell(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def norm(line: str) -> str:
    return " | ".join(_ncell(c) for c in cells(line))


def status_index(line: str) -> int:
    for i, cell in enumerate(cells(line)):
        if i and (TERMINAL.match(cell) or OPEN.match(cell)):
            return i
    return -1


def status_of(line: str) -> str:
    i = status_index(line)
    if i < 0:
        return ""
    return "closed" if TERMINAL.match(cells(line)[i]) else "open"


def norm_no_status(line: str) -> str:
    """The row with whitespace normalized and the status cell blanked: a twin the PR closed compares equal to main's
    open text."""
    row = [_ncell(c) for c in cells(line)]
    i = status_index(line)
    if i >= 0:
        row[i] = ""
    return " | ".join(row)


def _render(row: list, like: str) -> str:
    lead = like[: len(like) - len(like.lstrip())]
    return lead + "| " + " | ".join(row) + " |"


def _keyed(lines: list, twins: Optional[dict] = None) -> list:
    """[(key, line)] for table rows in order. key is (id, n) where n counts the id's occurrences, but only for ids in
    `twins` (main has more than one row); others are (id, 0)."""
    seen: dict = {}
    out = []
    for line in lines:
        gid = row_id(line)
        if not gid:
            continue
        n = seen.get(gid, 0)
        seen[gid] = n + 1
        out.append(((gid, n if twins and gid in twins else 0), line))
    return out


def _counts(lines: list) -> dict:
    out: dict = {}
    for line in lines:
        gid = row_id(line)
        if gid:
            out[gid] = out.get(gid, 0) + 1
    return out


# ---- tables and the text around them ---------------------------------------------------------------------------------


def _layout(lines: list) -> tuple:
    """(plain lines with a marker where each table's rows were, {table key: [row lines]}, {row key: table key}).
    A table is keyed by the heading above it and its header row."""
    plain, tables = [], {}
    heading, header = "", ""
    current = None
    for i, line in enumerate(lines):
        if row_id(line):
            if current is None:
                current = (heading, header)
                plain.append("\x00TABLE\x00%s\x00%s" % current)
                tables.setdefault(current, [])
            tables[current].append(line)
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped
        if stripped.startswith("|") and not set(stripped.replace("|", "").strip()) <= set("-: "):
            header = re.sub(r"\s+", " ", stripped)
        if current is not None and not stripped.startswith("|"):
            current = None
        plain.append(line)
    return plain, tables


def _merge_file(base: list, main: list, pr: list) -> list:
    """Three-way merge of the text outside the tables with git merge-file. Conflict raises."""
    if main == pr or pr == base:
        return main
    if main == base:
        return pr
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, lines in (("main", main), ("base", base), ("pr", pr)):
            path = Path(tmp) / name
            path.write_text("\n".join(lines) + "\n")
            paths.append(str(path))
        done = subprocess.run(["git", "merge-file", "-p", paths[0], paths[1], paths[2]], capture_output=True, text=True)
        if done.returncode != 0:
            raise Conflict("the text outside the gap table conflicts")
        return done.stdout.rstrip("\n").split("\n") if done.stdout.strip("\n") else []


def _pick(key, b: Optional[str], m: Optional[str], p: Optional[str]) -> Optional[str]:
    if m is None:
        return p if (b is None or (p is not None and norm(p) != norm(b))) else None
    if p is None:
        return m if (b is None or norm(m) != norm(b)) else None
    if norm(m) == norm(p):
        return m
    if b is not None and norm(m) == norm(b):
        return p
    if b is not None and norm(p) == norm(b):
        return m
    tm, tp = status_of(m) == "closed", status_of(p) == "closed"
    if tm != tp:
        return m if tm else p
    # Both or neither terminal: main's row, plus cells the PR filled that main left empty.
    mc, pc = cells(m), cells(p)
    if len(mc) == len(pc):
        merged = [mc[i] if mc[i].strip() else pc[i] for i in range(len(mc))]
        return m if merged == mc else _render(merged, m)
    return m


def gap_resolve(base: str, main: str, pr: str) -> str:
    """The merged report (see the module docstring). Raises Conflict only for the text outside the tables."""
    b_lines, m_lines, p_lines = (str(x or "").splitlines() for x in (base, main, pr))
    # Twins: an id with more than one row on main (or on the PR) is keyed (id, occurrence), so twins pair by position.
    twins = {gid for gid, n in list(_counts(m_lines).items()) + list(_counts(p_lines).items()) if n > 1}
    b_rows, m_rows, p_rows = dict(_keyed(b_lines, twins)), _keyed(m_lines, twins), _keyed(p_lines, twins)
    m_map, p_map = dict(m_rows), dict(p_rows)
    b_plain, b_tables = _layout(b_lines)
    m_plain, m_tables = _layout(m_lines)
    p_plain, p_tables = _layout(p_lines)
    table_of: dict = {}
    # Row -> table: main's table for keys main has, else the PR's.
    for side_lines, side_tables in ((p_lines, p_tables), (m_lines, m_tables)):
        seen: dict = {}
        for tkey, rows in side_tables.items():
            for line in rows:
                gid = row_id(line)
                n = seen.get(gid, 0)
                seen[gid] = n + 1
                table_of[(gid, n if gid in twins else 0)] = tkey
    rows_by_table: dict = {}
    order = [k for k, _ in m_rows] + [k for k, _ in p_rows if k not in m_map]
    for key in dict.fromkeys(order):
        line = _pick(key, b_rows.get(key), m_map.get(key), p_map.get(key))
        if line is not None:
            rows_by_table.setdefault(table_of.get(key), []).append(line)
    plain = _merge_file(b_plain, m_plain, p_plain)
    out, placed = [], set()
    for line in plain:
        if line.startswith("\x00TABLE\x00"):
            _x, heading, header = line.split("\x00")[1:4] if line.count("\x00") >= 3 else ("", "", "")
            tkey = (heading, header)
            if tkey not in placed:
                out.extend(rows_by_table.get(tkey, []))
                placed.add(tkey)
            continue
        out.append(line)
    for tkey, rows in rows_by_table.items():
        if tkey not in placed:
            out.extend(rows)  # a table only one side had, or rows whose table moved
    trailing = "\n" if any(str(x or "").endswith("\n") for x in (main, pr)) else ""
    return "\n".join(out) + trailing


merge3 = gap_resolve  # 1.7.19 name


# ---- the guard ------------------------------------------------------------------------------------------------------


def gap_check(main: str, pr: str, merged: str) -> list:
    """Findings the merge introduced relative to main and the PR head (twins already on main are fine)."""
    m_lines, p_lines, g_lines = (str(x or "").splitlines() for x in (main, pr, merged))
    out = []
    if _MARKER.search(str(merged or "")):
        out.append("conflict markers are left in the gap report")
    mc, pc, gc = _counts(m_lines), _counts(p_lines), _counts(g_lines)
    for gid in sorted(set(gc) | (set(mc) & set(pc))):
        want = max(mc.get(gid, 0), pc.get(gid, 0))
        if gc.get(gid, 0) != want:
            out.append("%s: %d rows after the merge (main has %d, the PR has %d)" % (gid, gc.get(gid, 0), mc.get(gid, 0), pc.get(gid, 0)))
    count = lambda lines: _tally([norm_no_status(l) for l in lines if row_id(l)])
    mn, pn, gn = count(m_lines), count(p_lines), count(g_lines)
    for text, n in sorted(gn.items()):
        if n > max(mn.get(text, 0), pn.get(text, 0)):
            out.append("%s: an identical row repeated %d times (main has %d, the PR has %d)" % (
                text.split(" | ")[0], n, mn.get(text, 0), pn.get(text, 0)))
    known = {norm(l) for l in m_lines + p_lines if row_id(l)}
    by_id: dict = {}
    for line in g_lines:
        if row_id(line):
            by_id.setdefault(row_id(line), []).append(line)
    for gid, rows in sorted(by_id.items()):
        if any(status_of(r) == "closed" for r in rows):
            for r in rows:
                if status_of(r) == "open" and norm(r) not in known:
                    out.append("%s: a stale open copy next to its closed row, on neither main nor the PR" % gid)
    return out


def _tally(items: list) -> dict:
    out: dict = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


# ---- the cleanup ------------------------------------------------------------------------------------------------------


def _columns(lines: list) -> tuple:
    """(title column, evidence column) indexes from the header row; (1, -1) when not named."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and not row_id(line) and not set(stripped.replace("|", "").strip()) <= set("-: "):
            names = [c.lower() for c in cells(line)]
            title = next((i for i, c in enumerate(names) if any(w in c for w in ("title", "gap", "summary", "name"))
                          and i), 1)
            evidence = next((i for i, c in enumerate(names) if any(w in c for w in ("evidence", "lands", "landed", "proof",
                                                                                    "tickets"))), -1)
            return title, evidence
    return 1, -1


def classify(text: str) -> dict:
    """{copies: [line index], stale: [line index], twins: {id: [line index]}} for the whole file."""
    lines = str(text or "").splitlines()
    title, evidence = _columns(lines)
    groups: dict = {}
    for i, line in enumerate(lines):
        gid = row_id(line)
        if gid:
            groups.setdefault(gid, []).append(i)
    copies, stale, twins = [], [], {}
    for gid, idx in groups.items():
        if len(idx) < 2:
            continue
        gone = set()
        # True copies: identical after whitespace and the status cell. Keep the closed one, else the first.
        by_text: dict = {}
        for i in idx:
            by_text.setdefault(norm_no_status(lines[i]), []).append(i)
        for group in by_text.values():
            if len(group) > 1:
                keep = next((i for i in group if status_of(lines[i]) == "closed"), group[0])
                for i in group:
                    if i != keep:
                        (stale if status_of(lines[i]) == "open" and status_of(lines[keep]) == "closed" else copies).append(i)
                        gone.add(i)
        left = [i for i in idx if i not in gone]
        closed = [i for i in left if status_of(lines[i]) == "closed"]
        for i in left:
            if status_of(lines[i]) != "open" or not closed:
                continue
            c = cells(lines[i])
            for j in closed:
                d = cells(lines[j])
                same_title = 0 < title < min(len(c), len(d)) and _ncell(c[title]) and _ncell(c[title]) == _ncell(d[title])
                same_ev = 0 <= evidence < min(len(c), len(d)) and _ncell(c[evidence]) and _ncell(c[evidence]) == _ncell(d[evidence])
                if same_title or same_ev:
                    stale.append(i)
                    gone.add(i)
                    break
        rest = [i for i in idx if i not in gone]
        if len(rest) > 1:
            twins[gid] = rest
    return {"copies": sorted(copies), "stale": sorted(stale), "twins": twins}


def dedupe(text: str, rename_twins: bool = False) -> tuple:
    """(clean text, removed lines, renamed [(old id, new id, line)]). Removes only true copies and stale open copies."""
    lines = str(text or "").splitlines()
    found = classify(text)
    drop = set(found["copies"]) | set(found["stale"])
    renamed = []
    if rename_twins:
        for gid, idx in found["twins"].items():
            for n, i in enumerate(idx[1:], 1):
                new = "%s-%s" % (gid, chr(ord("A") + n))
                lines[i] = lines[i].replace(gid, new, 1)
                renamed.append((gid, new, lines[i]))
    removed = [lines[i] for i in sorted(drop)]
    out = [line for i, line in enumerate(lines) if i not in drop]
    trailing = "\n" if str(text or "").endswith("\n") else ""
    return "\n".join(out) + trailing, removed, renamed


def report(text: str) -> tuple:
    """(problems, twins). Problems fail a check; twins are reported as "reused id, different gaps"."""
    found = classify(text)
    lines = str(text or "").splitlines()
    problems = []
    if _MARKER.search(str(text or "")):
        problems.append("conflict markers are left in the file")
    for i in found["copies"]:
        problems.append("%s: a repeated row (line %d)" % (row_id(lines[i]), i + 1))
    for i in found["stale"]:
        problems.append("%s: a stale open copy of a closed gap (line %d)" % (row_id(lines[i]), i + 1))
    twins = ["%s: reused id, different gaps (lines %s)" % (gid, ", ".join(str(i + 1) for i in idx))
             for gid, idx in sorted(found["twins"].items())]
    return problems, twins


def problems(text: str) -> list:
    return report(text)[0]


def references(root: Path, old: str, beam_path: Optional[Path] = None) -> list:
    """Every place an id is referenced: repo files (git grep), beam tickets, and the Jira search to run."""
    out = []
    done = subprocess.run(["git", "grep", "-n", "-w", "-F", old], cwd=str(root), capture_output=True, text=True)
    out.extend("repo: %s" % line for line in done.stdout.splitlines()[:50])
    beam_path = beam_path or Path(root) / ".warp" / "beam.json"
    try:
        import json

        doc = json.loads(Path(beam_path).read_text())
        for tid, t in (doc.get("tickets") or {}).items():
            if old in json.dumps(t):
                out.append("beam: ticket %s mentions %s" % (tid, old))
    except (OSError, ValueError):
        pass
    out.append('jira: search labels = "warp:%s" OR summary ~ "\\"%s\\"" OR text ~ "\\"%s\\""' % (old, old, old))
    return out


def _branch(root: Path) -> str:
    done = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(root), capture_output=True, text=True)
    return done.stdout.strip()


def cleanup(path: Path, write: bool = False, rename_twins: bool = False, base_branches=("main", "master")) -> tuple:
    """(exit code, lines) for `gap-dedupe` / `dedupe`."""
    path = Path(path)
    text = path.read_text()
    clean, removed, renamed = dedupe(text, rename_twins=rename_twins)
    lines = ["gap-report: remove %s" % line.strip() for line in removed]
    _p, twins = report(text)
    if not rename_twins:
        lines.extend("gap-report: kept: %s" % t for t in twins)
    root = path.resolve().parent
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(root), capture_output=True, text=True).stdout.strip()
    for old, new, line in renamed:
        lines.append("gap-report: rename a twin %s -> %s: %s" % (old, new, line.strip()))
        lines.extend("gap-report:   update %s" % ref for ref in references(Path(top or root), old))
    if write and (removed or renamed):
        branch = _branch(root)
        if branch in set(base_branches):
            return 2, lines + ["gap-report: refused: you are on %s. Run the cleanup on a branch and merge it as a normal "
                               "pull request." % branch]
        import safe_write

        safe_write.write_text(path, clean)  # 1.7.24: a docs/spec file keeps its canonical casing
    left, _t = report(clean)
    lines.append("gap-report: %s %d repeated or stale row%s%s%s" % (
        "removed" if write else "would remove", len(removed), "" if len(removed) == 1 else "s",
        (", renamed %d twin%s" % (len(renamed), "" if len(renamed) == 1 else "s")) if renamed else "",
        ("; still: " + "; ".join(left[:5])) if left else ""))
    return (1 if left else 0), lines


def restore(current: str, old: str) -> tuple:
    """(text, restored rows): rows of `old` (the report before the 1.7.19 cleanup) whose content is gone from
    `current`, put back after the last row of their id (or at the end of the table). True copies and stale open copies
    of `old` are not restored. This undoes the twins 1.7.19's `dedupe --write` deleted, and nothing else."""
    old_lines = str(old or "").splitlines()
    shape = classify(old)
    junk = set(shape["copies"]) | set(shape["stale"])
    lines = str(current or "").splitlines()
    have = {norm_no_status(l) for l in lines if row_id(l)}
    restored = []
    for i, line in enumerate(old_lines):
        if i in junk or not row_id(line) or norm_no_status(line) in have:
            continue
        gid = row_id(line)
        at = max((j for j, l in enumerate(lines) if row_id(l) == gid), default=-1)
        if at < 0:
            at = max((j for j, l in enumerate(lines) if row_id(l)), default=len(lines) - 1)
        lines.insert(at + 1, line)
        have.add(norm_no_status(line))
        restored.append(line)
    trailing = "\n" if str(current or "").endswith("\n") else ""
    return "\n".join(lines) + trailing, restored


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="GAP-REPORT.md merge, check and cleanup", epilog=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check")
    pc.add_argument("path")
    pd = sub.add_parser("dedupe")
    pd.add_argument("path")
    pd.add_argument("--write", action="store_true")
    pd.add_argument("--rename-twins", dest="rename_twins", action="store_true")
    pr_ = sub.add_parser("restore", help="put back rows an earlier cleanup deleted (from a git ref)")
    pr_.add_argument("path")
    pr_.add_argument("--from", dest="ref", required=True, help="a commit before the cleanup, e.g. <cleanup sha>^")
    pr_.add_argument("--write", action="store_true")
    pm = sub.add_parser("merge")
    pm.add_argument("base")
    pm.add_argument("main")
    pm.add_argument("pr")
    pm.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if args.cmd == "check":
        found, twins = report(Path(args.path).read_text())
        for line in found:
            print("gap-report: " + line)
        for line in twins:
            print("gap-report: note: " + line)
        if not found:
            print("gap-report: %s has no repeated or stale rows%s" % (args.path, (" (%d reused ids)" % len(twins)) if twins else ""))
        return 1 if found else 0
    if args.cmd == "dedupe":
        code, lines = cleanup(Path(args.path), write=args.write, rename_twins=args.rename_twins)
        print("\n".join(lines))
        return code
    if args.cmd == "restore":
        path = Path(args.path).resolve()
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(path.parent), capture_output=True, text=True).stdout.strip()
        rel = path.relative_to(top) if top else path.name
        shown = subprocess.run(["git", "show", "%s:%s" % (args.ref, rel)], cwd=top or str(path.parent), capture_output=True, text=True)
        if shown.returncode != 0:
            print("gap-report: cannot read %s at %s: %s" % (rel, args.ref, shown.stderr.strip()))
            return 2
        text, restored = restore(path.read_text(), shown.stdout)
        for line in restored:
            print("gap-report: restore %s" % line.strip())
        if args.write and restored:
            branch = _branch(path.parent)
            if branch in {"main", "master"}:
                print("gap-report: refused: you are on %s; restore on a branch and merge a normal pull request" % branch)
                return 2
            import safe_write

            safe_write.write_text(path, text)
        print("gap-report: %s %d row%s" % ("restored" if args.write else "would restore", len(restored), "" if len(restored) == 1 else "s"))
        return 0
    base, main_t, pr = (Path(p).read_text() for p in (args.base, args.main, args.pr))
    try:
        merged = gap_resolve(base, main_t, pr)
    except Conflict as exc:
        print("gap-report: conflict: %s" % exc, file=sys.stderr)
        return 1
    found = gap_check(main_t, pr, merged)
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


# ---- docs/dev/testing/test-verify.md: sections keyed by heading (1.7.20) ----------------------------------------------
#
# test-verify.md has the same shape of problem: a section per test is edited in place (a result, a date, evidence),
# so a line union kept the old and the new text of the same section. Sections are keyed by their heading; each is
# merged three-way on its own, and a real clash inside one section is a conflict for the owner, never a union.

_HEADING = re.compile(r"^#{1,6}\s+\S")


def _sections(text: str) -> tuple:
    """(preamble lines, [(heading, body lines)]) in order. A repeated heading gets an occurrence suffix as its key."""
    pre, out, seen = [], [], {}
    current = None
    for line in str(text or "").splitlines():
        if _HEADING.match(line):
            key = line.strip()
            n = seen.get(key, 0)
            seen[key] = n + 1
            current = (key if not n else "%s\x00%d" % (key, n), line, [])
            out.append(current)
            continue
        (current[2] if current is not None else pre).append(line)
    return pre, out


def section_resolve(base: str, main: str, pr: str) -> str:
    """Three-way, one section at a time. Main's order; sections new on the PR are added at the end."""
    b_pre, b_sec = _sections(base)
    m_pre, m_sec = _sections(main)
    p_pre, p_sec = _sections(pr)
    bmap = {k: [h] + body for k, h, body in b_sec}
    pmap = {k: [h] + body for k, h, body in p_sec}
    out = _merge_file(b_pre, m_pre, p_pre)
    for key, head, body in m_sec:
        m_lines = [head] + body
        p_lines = pmap.get(key)
        b_lines = bmap.get(key)
        if p_lines is None:
            if b_lines is None or b_lines != m_lines:
                out.extend(m_lines)  # the PR deleted a section main changed, or main added it: keep main's
            continue
        out.extend(_merge_file(b_lines or [], m_lines, p_lines) if b_lines is not None or m_lines != p_lines else m_lines)
    mkeys = {k for k, _h, _b in m_sec}
    for key, head, body in p_sec:
        if key not in mkeys and key not in bmap:
            out.extend([head] + body)
    trailing = "\n" if any(str(x or "").endswith("\n") for x in (main, pr)) else ""
    return "\n".join(out) + trailing


def keyed_resolver(path: str):
    """The keyed resolver for a file that is edited in place, or None (1.7.20)."""
    name = Path(str(path or "")).name
    if name.upper() == "GAP-REPORT.MD":
        return gap_resolve
    if str(path or "").replace("\\", "/").endswith("docs/dev/testing/test-verify.md") or name == "test-verify.md":
        return section_resolve
    return None
