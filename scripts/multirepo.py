#!/usr/bin/env python3
"""Several repos, one run (`multiRepo`).

A build-schedule/2 schedule.json whose tickets name more than one `repo` runs as one run in one
agent window. The repo with the most tickets is the parent: Warp runs in its checkout, and the beam,
journal, board, and published state live there. Every other repo is a sibling, worked from its own
clone next to the parent (`siblingRoot`, default `..`, so `../HumanifyOS-UI`).

  parent checkout  HumanifyOS/             .warp/ (all state), .warp/worktrees/<id> (every worktree)
  sibling clone    ../HumanifyOS-UI/       the UI tickets' branches, pull requests, and merges

A sibling ticket's worktree is a worktree of the sibling clone, kept under the parent's gitignored
`.warp/worktrees/`, so the agent window sees it. Its branch, pull request, Bugbot review, merge check,
merge, and branch delete all run against the sibling repo. Nothing of the sibling is committed to the
parent: the parent only gets `.warp/` state. Dependencies stay plain ids (`UI-07`), so a sibling merge
unblocks a parent ticket in the same pass. Locks are per repo: `src/app` in the parent and `src/app` in
a sibling do not collide.

`/warp-scan` decides it and writes it to `.warp/config.yaml`:
  multiRepo: true
  siblingRepos: {HumanifyOS-UI: https://github.com/acme/HumanifyOS-UI.git}

`/warp-start` and `/warp-resume` check it before they start anything (`check`): the parent is this
checkout, every sibling clone is there (cloned when it is not), its origin is the configured repo and
reachable, its base branch exists, its clone has no local changes, gh can see it, every ticket names a
known repo, and tickets run as worktrees on this machine. Any miss refuses with the reason.

  python3 scripts/multirepo.py check --beam .warp/beam.json      the start check, without starting
  python3 scripts/multirepo.py show --beam .warp/beam.json       repos, paths, bases, ticket counts

`splitRepos: true` keeps the older mode: one run per repo (`repoRuns`, `externalRepos`).

?, help, -h, and --help print this text.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

HELP = __doc__
FALSE = {"", "false", "no", "off", "0", "none", "null"}
LOCK_MARK = "@"
SETTLED = ("merged", "done", "skipped")
# A row's own definition comes from the schedule; everything else is the run's progress.
SCHEDULE_FIELDS = {
    "id", "summary", "deps", "after", "unlocks", "locks", "gate", "size", "hours", "critical", "starred", "rank",
    "rankDays", "module", "layer", "group", "owner", "lane", "repo", "wave", "priority", "priorityGate", "checks",
    "planned", "screens", "mock", "acs", "autoMerge", "complexity", "plannedStart", "plannedMerge", "plan", "result",
    "repoRole",
}


def truthy(value) -> bool:
    return str(value if value is not None else "").strip().casefold() not in FALSE


def _git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(root), "-c", "maintenance.auto=false", *args],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, stdout="", stderr=str(exc))


def parse_map(raw) -> dict:
    import buildsched

    return buildsched.parse_map(raw)


def slug(url: str) -> str:
    """owner/name from a git URL or path, lower case. `https://github.com/acme/UI.git` -> `acme/ui`."""
    text = str(url or "").strip().split("#", 1)[0].rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    parts = re.split(r"[/:]", text)
    parts = [p for p in parts if p]
    return "/".join(parts[-2:]).casefold() if len(parts) >= 2 else text.casefold()


def name_of(url: str) -> str:
    return slug(url).rsplit("/", 1)[-1]


def spec(raw: str) -> tuple:
    """(url, branch) from `<url>#<branch>`."""
    text = str(raw or "").strip()
    url, _, branch = text.partition("#")
    return url.strip(), branch.strip()


def same(a: str, b: str) -> bool:
    return str(a or "").casefold() == str(b or "").casefold()


# ---- what the beam carries --------------------------------------------------------------------------------


def enabled(data: Optional[dict]) -> bool:
    data = data or {}
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    return truthy(cfg.get("multiRepo")) and isinstance(data.get("repos"), dict)


def home(data: Optional[dict]) -> str:
    info = (data or {}).get("repos")
    return str(info.get("home") or "") if isinstance(info, dict) else ""


def siblings(data: Optional[dict]) -> dict:
    info = (data or {}).get("repos")
    sib = info.get("siblings") if isinstance(info, dict) else None
    return sib if isinstance(sib, dict) else {}


def ticket_repo(data: Optional[dict], ticket: Optional[dict]) -> str:
    """The ticket's sibling repo name, or "" when it is a parent ticket (or the run is not multi-repo)."""
    if not enabled(data) or not isinstance(ticket, dict):
        return ""
    repo = str(ticket.get("repo") or "").strip()
    if not repo or same(repo, home(data)):
        return ""
    for name in siblings(data):
        if same(name, repo):
            return name
    return ""


def is_sibling(data: Optional[dict], ticket: Optional[dict]) -> bool:
    return bool(ticket_repo(data, ticket))


def mark_roles(beam: dict) -> None:
    """`repoRole: sibling` on each sibling ticket, so code that sees only the ticket (checks_green) knows."""
    for t in (beam.get("tickets") or {}).values():
        if isinstance(t, dict):
            if is_sibling(beam, t):
                t["repoRole"] = "sibling"
            else:
                t.pop("repoRole", None)


def pr_url(data: Optional[dict], ticket: Optional[dict], value: str) -> str:
    """A bare pull request number for a sibling ticket becomes its full URL, so nothing reads it as the parent's #n."""
    text = str(value or "").strip()
    name = ticket_repo(data, ticket)
    if not name or not text.isdigit():
        return text
    import provider

    repo_slug = provider.github_slug(spec(url_for(data, ticket))[0])
    return "https://github.com/%s/pull/%s" % (repo_slug, text) if repo_slug else text


def sibling_path(home_root: Path, data: Optional[dict], name: str) -> Path:
    row = siblings(data).get(name) or {}
    raw = str(row.get("path") or "").strip() or str(Path("..") / name)
    path = Path(raw)
    return (path if path.is_absolute() else Path(home_root) / path).resolve()


def root_for(data: Optional[dict], ticket: Optional[dict], home_root) -> Path:
    """Where the ticket's git work happens: the sibling clone for a sibling ticket, else the parent checkout."""
    name = ticket_repo(data, ticket)
    home_root = Path(home_root).resolve()
    return sibling_path(home_root, data, name) if name else home_root


def base_for(data: Optional[dict], ticket: Optional[dict], parent_base: str) -> str:
    name = ticket_repo(data, ticket)
    if not name:
        return parent_base
    return str((siblings(data).get(name) or {}).get("base") or "main")


def url_for(data: Optional[dict], ticket: Optional[dict]) -> str:
    name = ticket_repo(data, ticket)
    return str((siblings(data).get(name) or {}).get("url") or "") if name else ""


def scoped_locks(data: Optional[dict], ticket: dict, paths: list) -> list:
    """Lock paths keyed by repo, for overlap checks only: a sibling's `src/a` is `@HumanifyOS-UI/src/a`."""
    name = ticket_repo(data, ticket)
    if not name:
        return list(paths)
    return ["%s%s/%s" % (LOCK_MARK, name, p) for p in paths]


def repo_tag(data: Optional[dict], ticket: Optional[dict]) -> str:
    """` [HumanifyOS-UI]` for a sibling ticket in board and herald lines; empty otherwise."""
    name = ticket_repo(data, ticket)
    return " [%s]" % name if name else ""


def prompt_lines(data: Optional[dict], ticket: dict, home_root) -> list:
    """What a Shuttle on a sibling ticket must know: which repo its worktree is, and where its pull request goes."""
    name = ticket_repo(data, ticket)
    if not name:
        return []
    url = url_for(data, ticket)
    base = base_for(data, ticket, "main")
    return [
        "repo: %s (sibling of %s; clone %s; origin %s; base %s)" % (name, home(data), root_for(data, ticket, home_root), slug(url) or url, base),
        "This ticket's code is in %s, not in %s. Your worktree is a checkout of %s: commit and push there, and open the "
        "pull request in %s against %s. Never copy %s files into the parent checkout." % (name, home(data), name, slug(url) or name, base, name),
    ]


# ---- scan -------------------------------------------------------------------------------------------------


def counts(tickets: list, fallback: str = "") -> dict:
    out = {}
    for t in tickets:
        repo = str(t.get("repo") or fallback or "").strip()
        if repo:
            out[repo] = out.get(repo, 0) + 1
    return out


def choose_parent(by_repo: dict, order: list) -> str:
    """The repo with the most tickets. A tie goes to the one the schedule lists first."""
    rank = {r.casefold(): i for i, r in enumerate(order or [])}
    return sorted(by_repo, key=lambda r: (-by_repo[r], rank.get(r.casefold(), len(rank)), r.casefold()))[0]


def derive_url(home_url: str, name: str) -> str:
    """The sibling's URL next to the parent's: same host and owner, the sibling's name."""
    text = str(home_url or "").strip()
    if not text:
        return ""
    git = text.endswith(".git")
    base = text[:-4] if git else text
    cut = max(base.rfind("/"), base.rfind(":"))
    if cut < 0:
        return ""
    return base[: cut + 1] + name + (".git" if git else "")


def describe(home_root: Path, cfg: dict, home_name: str, by_repo: dict) -> dict:
    """{home, siblings: {name: {path, url, base, tickets}}} from the config, the clones on disk, and the parent's origin."""
    import herald_fmt

    configured = parse_map(cfg.get("siblingRepos"))
    root_dir = str(cfg.get("siblingRoot") or "..").strip() or ".."
    home_url = herald_fmt.git(home_root, "remote", "get-url", "origin")
    out = {"home": home_name, "homeTickets": by_repo.get(home_name, 0), "siblings": {}}
    for name in sorted(by_repo, key=str.casefold):
        if same(name, home_name):
            continue
        raw = next((v for k, v in configured.items() if same(k, name)), "")
        url, branch = spec(raw)
        rel = str(Path(root_dir) / name)
        where = (Path(rel) if Path(rel).is_absolute() else home_root / rel).resolve()
        if not url and (where / ".git").exists():
            url = herald_fmt.git(where, "remote", "get-url", "origin")
        if not url:
            url = derive_url(home_url, name)
        if not branch and (where / ".git").exists():
            head = herald_fmt.git(where, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
            branch = head.split("/", 1)[-1] if head else ""
        out["siblings"][name] = {"path": rel, "url": url, "base": branch or "main", "tickets": by_repo[name]}
    return out


def config_lines(info: dict) -> dict:
    """The keys scan writes to .warp/config.yaml for a multi-repo run."""
    pairs = []
    for name, row in info["siblings"].items():
        url = row.get("url") or ""
        if row.get("base") and row["base"] != "main":
            url = "%s#%s" % (url, row["base"])
        pairs.append("%s: %s" % (name, url))
    return {"multiRepo": "true", "siblingRepos": "{%s}" % ", ".join(pairs)}


def write_config(home_root: Path, keys: dict) -> list:
    """Set each key in .warp/config.yaml; lines for what changed."""
    import provider

    path = Path(home_root) / ".warp" / "config.yaml"
    text = path.read_text() if path.is_file() else ""
    out = []
    for key, raw in keys.items():
        now = provider.set_key(text, key, raw)
        if now != text:
            out.append("config: %s: %s" % (key, raw))
            text = now
    if out:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return out


# ---- carrying progress into a multi-repo beam --------------------------------------------------------------


def _beams_on_base(clone: Path, base: str) -> list:
    """Every published beam on the clone's origin base: `.warp/beam.json` and `.warp/runs/*/beam.json`."""
    if not (clone / ".git").exists():
        return []
    _git(clone, "fetch", "-q", "origin", base)
    ref = "origin/%s" % base
    if _git(clone, "rev-parse", "--verify", "-q", ref).returncode != 0:
        ref = base
    listed = _git(clone, "ls-tree", "-r", "--name-only", ref, "--", ".warp")
    names = [n for n in listed.stdout.splitlines() if n == ".warp/beam.json" or re.fullmatch(r"\.warp/runs/[^/]+/beam\.json", n)]
    found = []
    for name in names:
        shown = _git(clone, "show", "%s:%s" % (ref, name))
        try:
            doc = json.loads(shown.stdout or "{}")
        except ValueError:
            continue
        if isinstance(doc.get("tickets"), dict):
            found.append((name, doc))
    return found


def _better(old: Optional[dict], new: dict) -> bool:
    rank = {"merged": 3, "done": 2, "skipped": 1}
    return old is None or rank.get(new.get("status"), 0) > rank.get(old.get("status"), 0)


def carry(beam: dict, previous: dict, home_root: Path) -> list:
    """Keep what the earlier runs finished. A merged, done, or skipped ticket stays that way.

    Sources: this checkout's own beam before the scan, and every published beam on each sibling's
    origin base (the old one-run-per-repo mode published the sibling's run there). Only a settled row
    is carried, with its pull request, branch, Jira link, and times; its definition (deps, locks,
    gate, summary) comes from the schedule. Rows in flight start queued and pick up their branch and
    pull request when they launch.
    """
    rows = {}
    for tid, row in (previous or {}).items():
        if isinstance(row, dict) and row.get("status") in SETTLED and _better(rows.get(tid), row):
            rows[tid] = row
    sources = []
    tickets = beam.get("tickets") or {}
    for name, sib in siblings(beam).items():
        clone = sibling_path(home_root, beam, name)
        mine = {tid for tid, t in tickets.items() if same(ticket_repo(beam, t), name)}
        for where, doc in _beams_on_base(clone, sib.get("base") or "main"):
            sources.append("%s:%s" % (name, where))
            for tid, row in doc["tickets"].items():
                # Only this sibling's own tickets: another program's run, or a row for a parent ticket, says nothing here.
                if tid not in mine or not isinstance(row, dict) or (row.get("repo") and not same(row.get("repo"), name)):
                    continue
                if row.get("status") in SETTLED and _better(rows.get(tid), row):
                    rows[tid] = row
    carried = []
    in_flight = []
    for tid, ticket in (beam.get("tickets") or {}).items():
        old = rows.get(tid)
        if old is None:
            prev = (previous or {}).get(tid)
            if isinstance(prev, dict) and prev.get("status") not in (None, "queued", "blocked") and prev.get("status") not in SETTLED:
                in_flight.append("%s (%s%s)" % (tid, prev.get("status"), ", " + str(prev.get("branch")) if prev.get("branch") else ""))
            continue
        for key, value in old.items():
            if key in SCHEDULE_FIELDS:
                continue
            if key.startswith("jiraKey") and ticket.get("jiraKey"):
                continue
            ticket[key] = value
        carried.append(tid)
    lines = []
    if carried:
        lines.append("repos: kept %d finished tickets from earlier runs (%s)" % (len(carried), ", ".join(["this checkout"] + sources)))
    if in_flight:
        lines.append("repos: in flight before the scan, queued again (their branch and pull request are reused at launch): " + ", ".join(in_flight[:20])
                     + (" (+%d)" % (len(in_flight) - 20) if len(in_flight) > 20 else ""))
    return lines


# ---- the start check --------------------------------------------------------------------------------------


def _clean(clone: Path) -> list:
    status = _git(clone, "status", "--porcelain", "--untracked-files=normal")
    return [line[3:].strip() for line in status.stdout.splitlines() if len(line) > 3]


def check(home_root: Path, data: dict, cfg: dict, clone=True, gh_view=None) -> tuple:
    """(problems, lines). Every problem refuses the start; the lines say what was checked."""
    import herald_fmt
    import orchestrator

    home_root = Path(home_root).resolve()
    problems = []
    lines = []
    info = data.get("repos") if isinstance(data.get("repos"), dict) else None
    if not info or not info.get("home"):
        return ["multiRepo is true but this beam has no repos. Run /warp-scan again in this checkout."], lines
    here = str(cfg.get("repo") or "").strip() or herald_fmt.repo_name(home_root)
    if not same(here, info["home"]):
        problems.append("this checkout is %s, but the parent repo is %s (the repo with the most tickets holds the state). "
                        "Start the run in a %s checkout." % (here, info["home"], info["home"]))
    if orchestrator.launch_mode(cfg) == "agent":
        problems.append("launch: agent starts each ticket in its own cloud clone of the parent repo. A multi-repo run needs launch: worktree.")
    elif orchestrator.subagent_vm(cfg):
        problems.append("subagentVm: true gives each ticket its own VM clone of the parent repo. A multi-repo run needs subagentVm: false.")
    known = {info["home"].casefold()} | {n.casefold() for n in siblings(data)}
    unknown = sorted({str(t.get("repo")) for t in (data.get("tickets") or {}).values()
                      if isinstance(t, dict) and t.get("repo") and str(t.get("repo")).casefold() not in known})
    if unknown:
        problems.append("tickets name a repo that is not the parent or a sibling: %s. Run /warp-scan again." % ", ".join(unknown))
    configured = parse_map(cfg.get("siblingRepos"))
    connected = False
    try:
        import provider

        connected = provider.resolve(home_root, cfg).get("mode") == "connected"
    except Exception:
        connected = False
    for name, row in siblings(data).items():
        clone_path = sibling_path(home_root, data, name)
        raw = next((v for k, v in configured.items() if same(k, name)), "")
        url, branch = spec(raw)
        url = url or str(row.get("url") or "")
        base = branch or str(row.get("base") or "main")
        tag = "%s (%d tickets)" % (name, int(row.get("tickets") or 0))
        if not url:
            problems.append("%s: no URL. Set siblingRepos: {%s: <git url>} in .warp/config.yaml." % (name, name))
            continue
        if not (clone_path / ".git").exists():
            if clone_path.exists() and any(clone_path.iterdir()):
                problems.append("%s: %s exists and is not a git clone. Move it, or set siblingRoot to where the clones are." % (name, clone_path))
                continue
            if not clone:
                problems.append("%s: no clone at %s. /warp-start clones %s there." % (name, clone_path, url))
                continue
            made = subprocess.run(["git", "clone", "-q", url, str(clone_path)], capture_output=True, text=True)
            if made.returncode != 0:
                problems.append("%s: git clone %s %s failed: %s" % (name, url, clone_path, (made.stderr or made.stdout).strip().splitlines()[-1:] or ["?"]))
                continue
            lines.append("repos: cloned %s into %s" % (name, clone_path))
        origin = herald_fmt.git(clone_path, "remote", "get-url", "origin")
        if slug(origin) != slug(url):
            problems.append("%s: the clone at %s has origin %s, not %s." % (name, clone_path, origin or "(none)", url))
            continue
        fetched = _git(clone_path, "fetch", "-q", "origin", base)
        if fetched.returncode != 0:
            problems.append("%s: cannot fetch %s from %s: %s" % (name, base, origin, ((fetched.stderr or "").strip().splitlines() or ["?"])[-1]))
            continue
        dirty = [p for p in _clean(clone_path) if not p.startswith(".warp/")]
        if dirty:
            problems.append("%s: the clone at %s has local changes (%s%s). Commit or stash them; ticket work happens in worktrees."
                            % (name, clone_path, ", ".join(dirty[:5]), " +%d" % (len(dirty) - 5) if len(dirty) > 5 else ""))
            continue
        if connected and "github.com" in url:
            viewer = gh_view or _gh_view
            ok, why = viewer(clone_path, slug(url))
            if ok is False:
                problems.append("%s: gh cannot see %s (%s). Warp opens and merges its pull requests there." % (name, slug(url), why))
                continue
        row.update(url=url, base=base)
        lines.append("repos: %s at %s, origin %s, base %s: ok" % (tag, clone_path, slug(url), base))
    if not problems:
        lines.insert(0, "repos: multi-repo run. %s is the parent (%d tickets, holds the state)" % (info["home"], int(info.get("homeTickets") or 0)))
    return problems, lines


def _gh_view(clone: Path, repo_slug: str) -> tuple:
    import shutil

    if not shutil.which("gh"):
        return None, "gh is not installed"
    try:
        viewed = subprocess.run(["gh", "repo", "view", repo_slug, "--json", "name"], cwd=str(clone), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if viewed.returncode != 0:
        return False, ((viewed.stderr or viewed.stdout).strip().splitlines() or ["no access"])[-1]
    return True, ""


def start_refusal(beam_path: Path, cfg: dict, clone=True) -> list:
    """For /warp-start and /warp-resume: the lines to print, refusal last. Empty when the run is not multi-repo."""
    import beam as beam_mod

    beam_path = Path(beam_path)
    data = beam_mod.load_json(beam_path)
    if not truthy(cfg.get("multiRepo")):
        if enabled(data):
            left = sorted(tid for tid, t in (data.get("tickets") or {}).items() if is_sibling(data, t))
            if left:
                # Dropping the routing here would start sibling tickets in this repo and merge their pull requests by
                # number in the wrong repo. The beam stays multi-repo until a scan rebuilds it.
                return ["refuse: multi-repo run not started: multiRepo is false in .warp/config.yaml, but this run has %d tickets in "
                        "sibling repos (%s%s). Set multiRepo: true again, or /warp-scan to rebuild the run."
                        % (len(left), ", ".join(left[:8]), " +%d" % (len(left) - 8) if len(left) > 8 else "")]
        return []
    data.setdefault("config", {})["multiRepo"] = True
    home_root = beam_path.resolve().parent.parent
    problems, lines = check(home_root, data, cfg, clone=clone)
    if problems:
        return lines + ["refuse: multi-repo run not started: %s" % p for p in problems]
    beam_mod.atomic_write(beam_path, json.dumps(data, indent=2) + "\n", data=data)
    return lines


def show(data: dict, home_root: Path) -> list:
    if not enabled(data):
        return ["repos: not a multi-repo run (multiRepo is false)"]
    lines = ["repos: %s (parent, %d tickets, state)" % (home(data), int((data.get("repos") or {}).get("homeTickets") or 0))]
    for name, row in siblings(data).items():
        lines.append("repos: %s (sibling, %d tickets) clone %s origin %s base %s"
                     % (name, int(row.get("tickets") or 0), sibling_path(home_root, data, name), slug(row.get("url") or ""), row.get("base") or "main"))
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Several repos, one run", epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "show"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--beam", default=".warp/beam.json")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    beam_path = Path(args.beam).resolve()
    data = json.loads(beam_path.read_text())
    home_root = beam_path.parent.parent
    if args.cmd == "show":
        for line in show(data, home_root):
            print(line)
        return 0
    import prompt_gate

    cfg = prompt_gate.effective_config(data, home_root)
    if not truthy(cfg.get("multiRepo")):
        print("repos: not a multi-repo run (multiRepo is false)")
        return 0
    data.setdefault("config", {})["multiRepo"] = True
    problems, lines = check(home_root, data, cfg, clone=False)
    for line in lines:
        print(line)
    for problem in problems:
        print("refuse: %s" % problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
