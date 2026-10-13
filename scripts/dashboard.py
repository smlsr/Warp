#!/usr/bin/env python3
"""The run dashboard: one HTML page that shows every ticket on a slot swimlane.

`assets/warp-dashboard.html` is a single file with no install and no server. Open
it from disk and use File > Open to read `beam.json`, `status.json`, ticket
`state.json` files, or a whole `.warp` folder. It also reads a build-schedule/2
`schedule.json` (drawn as planned) and a /warp-export `WARP_PLAN.json`. Dropping
them on the page works too. Nothing leaves the browser.

`/warp-dashboard` writes a copy with the run's files already inside, so it
opens with data:

  .warp/dashboard.html   the page, with .warp/beam.json, every
                         .warp/tickets/<id>/state.json, and the other runs'
                         .warp/runs/<name>/beam.json embedded

Once that file exists, every status write (`scan.py status`, each parent pass)
rewrites it, and the page's Reload picks up the new copy. Delete the file to
stop that. It is local: state commits do not include it.

  python3 scripts/dashboard.py ?
  python3 scripts/dashboard.py write
  python3 scripts/dashboard.py write --beam .warp/beam.json --out .warp/dashboard.html --open
  python3 scripts/dashboard.py write --empty --out ~/Desktop/warp-dashboard.html
  python3 scripts/dashboard.py path

`write` prints `dashboard: <path>` and how many tickets and files went in.
`--empty` writes the page with no data, to open files by hand. `--open` opens
it in the default browser when this machine has one. `path` prints where the
page is in the plugin.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

HELP = __doc__
PAGE = Path(__file__).resolve().parent.parent / "assets" / "warp-dashboard.html"
SLOT = '<script id="warp-data" type="application/json">null</script>'
SLOT_RE = re.compile(r'<script id="warp-data" type="application/json">.*?</script>', re.S)
OUT_NAME = "dashboard.html"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def gather(beam_path: Path) -> list:
    """The beam, its ticket states, and the other runs' published beams."""
    warp = beam_path.parent
    root = warp.parent if warp.name == ".warp" else warp
    files = []
    beam = _read(beam_path)
    if isinstance(beam, dict):
        files.append({"path": _rel(beam_path, root), "data": beam})
    tickets = warp / "tickets"
    if tickets.is_dir():
        for state in sorted(tickets.glob("*/state.json")):
            data = _read(state)
            if isinstance(data, dict):
                files.append({"path": _rel(state, root), "data": data})
    mine = str((beam or {}).get("run") or "")
    runs = warp / "runs"
    if runs.is_dir():
        for other in sorted(runs.glob("*/beam.json")):
            data = _read(other)
            if isinstance(data, dict) and other.parent.name != mine:
                files.append({"path": _rel(other, root), "data": data})
    if not isinstance(beam, dict):
        status = _read(warp / "status.json")
        if isinstance(status, dict):
            files.append({"path": _rel(warp / "status.json", root), "data": status})
    return files


def repo_label(root: Path) -> Optional[str]:
    """owner/repo from origin. None without a remote: the page reads it from pull request links."""
    try:
        import herald_fmt

        web, _, _ = herald_fmt.repo_web(root)
    except Exception:
        return None
    if not web:
        return None
    parts = web.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]


def payload(beam_path: Path) -> dict:
    files = gather(beam_path)
    warp = beam_path.parent
    root = warp.parent if warp.name == ".warp" else warp
    beam = next((f["data"] for f in files if f["path"].endswith("beam.json")), {}) or {}
    return {
        "generatedAt": utcnow(),
        "repo": repo_label(root),
        "jiraSite": str((beam.get("config") or {}).get("jiraSite") or ""),
        "files": files,
    }


def embed(page: str, data: Optional[dict]) -> str:
    """Put `data` into the page. `</` is escaped so the JSON cannot close the script tag."""
    body = "null" if data is None else json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    body = body.replace("</", "<\\/").replace("<!--", "<\\!--")
    tag = '<script id="warp-data" type="application/json">' + body + "</script>"
    if not SLOT_RE.search(page):
        raise ValueError("the page has no warp-data slot")
    return SLOT_RE.sub(lambda _m: tag, page, count=1)


def write(beam_path: Path, out: Path, empty: bool = False) -> dict:
    page = PAGE.read_text()
    data = None if empty else payload(beam_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(embed(page, data))
    tmp.replace(out)
    tickets = 0
    for f in (data or {}).get("files") or []:
        d = f.get("data") or {}
        if isinstance(d.get("tickets"), dict):
            tickets += len(d["tickets"])
        elif isinstance(d.get("tickets"), list):
            tickets += len(d["tickets"])
    return {"out": out, "files": len((data or {}).get("files") or []), "tickets": tickets}


def refresh(beam_path: Path) -> Optional[Path]:
    """Rewrite `.warp/dashboard.html` when it exists. Status writes call this."""
    out = Path(beam_path).parent / OUT_NAME
    if not out.is_file():
        return None
    try:
        write(Path(beam_path), out)
    except Exception:
        return None
    return out


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Warp run dashboard", epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="write the page with this run's files inside")
    w.add_argument("--beam", default=".warp/beam.json")
    w.add_argument("--out", default=None, help="default: next to the beam, dashboard.html")
    w.add_argument("--empty", action="store_true", help="write the page without data")
    w.add_argument("--open", action="store_true", help="open it in the default browser")
    sub.add_parser("path", help="print where the page is in the plugin")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    if args.cmd == "path":
        print("dashboard: %s" % PAGE)
        return 0
    beam_path = Path(args.beam).expanduser()
    if not args.empty and not beam_path.is_file() and not (beam_path.parent / "status.json").is_file():
        print("refuse: no beam at %s. Run /warp-scan first, or pass --empty to write the page without data." % beam_path)
        return 2
    out = Path(args.out).expanduser() if args.out else beam_path.parent / OUT_NAME
    info = write(beam_path, out, empty=args.empty)
    if args.empty:
        print("dashboard: %s (no data; use File > Open)" % info["out"])
    else:
        print("dashboard: %s tickets=%d files=%d" % (info["out"], info["tickets"], info["files"]))
        if out.name == OUT_NAME and out.parent == beam_path.parent:
            print("dashboard: status writes keep this file current. Reload the page to see them. Delete the file to stop.")
    print("open: %s" % out.resolve().as_uri())
    if args.open:
        import webbrowser

        try:
            opened = webbrowser.open(out.resolve().as_uri())
        except Exception:
            opened = False
        if not opened:
            print("open: no browser on this machine. Open the file above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
