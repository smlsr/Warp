"""1.6.26: one run, several repos. The repo with the most tickets is the parent; the rest are sibling clones."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))

import beam as beam_mod  # noqa: E402
import buildsched  # noqa: E402
import checkout  # noqa: E402
import multirepo  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402
import scan  # noqa: E402

FIX = HERE / "fixtures" / "build_schedule_v2.json"


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=check)


def make_repo(origins: Path, where: Path, name: str, files: dict) -> Path:
    """A bare origin with one commit on main, and a clone of it at `where`."""
    bare = origins / ("%s.git" % name)
    git(origins, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = origins / ("seed-%s" % name)
    git(origins, "clone", "-q", str(bare), str(seed))
    for rel, text in files.items():
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text(text)
    git(seed, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    git(seed, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    git(seed, "push", "-q", "origin", "main")
    git(origins, "clone", "-q", str(bare), str(where))
    git(where, "config", "user.email", "t@example.com")
    git(where, "config", "user.name", "t")
    return where


class TwoRepos(unittest.TestCase):
    """Core (5 tickets) and Core-UI (1 ticket, AU-01) side by side, each with a bare origin."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origins = self.tmp / "origins"
        origins.mkdir()
        work = self.tmp / "work"
        work.mkdir()
        self.core = make_repo(origins, work / "Core", "Core", {"README.md": "core\n", ".gitignore": ".warp/worktrees/\n.warp/config.yaml\n"})
        self.ui = make_repo(origins, work / "Core-UI", "Core-UI", {"README.md": "ui\n", "apps/au-01/index.ts": "export {}\n"})
        self.core_origin = origins / "Core.git"
        self.ui_origin = origins / "Core-UI.git"
        (self.core / "PLAN" / "plan").mkdir(parents=True)
        shutil.copy(FIX, self.core / "PLAN" / "plan" / "schedule.json")
        git(self.core, "add", "PLAN")
        git(self.core, "commit", "-qm", "plan")
        git(self.core, "push", "-q", "origin", "main")
        (self.core / ".warp").mkdir()
        (self.core / ".warp" / "config.yaml").write_text("messenger: teams\n")

    def scan(self, cwd=None, env=None):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "scan.py"), "scan", "--folder", "PLAN", "--out", ".warp/beam.json"],
                              cwd=cwd or self.core, capture_output=True, text=True, env=dict(os.environ, WARP_PUBLISH="0", **(env or {})))

    def scanned(self) -> dict:
        out = self.scan()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.out = out.stdout
        return json.loads((self.core / ".warp" / "beam.json").read_text())


class ScanTest(TwoRepos):
    def test_the_repo_with_most_tickets_is_the_parent_and_every_ticket_is_kept(self):
        g = scan.from_schedule(FIX, self.core)
        parent, lines = buildsched.pick_repo(g, self.core, {})
        self.assertEqual(parent, "Core")
        self.assertEqual(len(g["tickets"]), 6)
        self.assertEqual(next(t for t in g["tickets"] if t["id"] == "AD-04")["after"], ["AU-01"])  # a plain id, not run:id
        self.assertEqual(next(t for t in g["tickets"] if t["id"] == "AU-01")["locks"], ["apps/au-01"])  # relative to Core-UI
        self.assertEqual(g["repos"], {"home": "Core", "counts": {"Core": 5, "Core-UI": 1}})
        self.assertIn("repos: multi-repo run: Core is the parent (5 tickets, holds the state); Core-UI (1 tickets) from sibling clones", lines)
        self.assertEqual(multirepo.choose_parent({"A": 3, "B": 3}, ["B", "A"]), "B")  # a tie goes to the schedule's order

    def test_scan_writes_the_config_and_the_beam(self):
        data = self.scanned()
        text = (self.core / ".warp" / "config.yaml").read_text()
        self.assertIn("multiRepo: true\n", text)
        self.assertNotIn("repo: ", text.replace("multiRepo", "").replace("siblingRepos", ""))  # `repo` names this checkout; scan leaves it
        self.assertIn("siblingRepos: {Core-UI: %s}\n" % self.ui_origin, text)
        self.assertIn("config: multiRepo: true", self.out)
        self.assertEqual(len(data["tickets"]), 6)
        self.assertTrue(data["config"]["multiRepo"])
        self.assertEqual(data["repos"]["home"], "Core")
        self.assertEqual(data["repos"]["siblings"]["Core-UI"], {"path": "../Core-UI", "url": str(self.ui_origin), "base": "main", "tickets": 1})
        self.assertEqual(data["tickets"]["AU-01"]["repo"], "Core-UI")
        self.assertEqual(data["tickets"]["AU-01"]["repoRole"], "sibling")
        self.assertNotIn("repoRole", data["tickets"]["AD-04"])
        self.assertTrue(multirepo.is_sibling(data, data["tickets"]["AU-01"]))
        self.assertFalse(multirepo.is_sibling(data, data["tickets"]["AD-04"]))
        self.assertEqual(multirepo.root_for(data, data["tickets"]["AU-01"], self.core), self.ui.resolve())
        # Rescanning keeps the URL it wrote, and a single-repo schedule turns it off.
        self.assertEqual(self.scan().returncode, 0)
        self.assertEqual((self.core / ".warp" / "config.yaml").read_text().count("siblingRepos:"), 1)

    def test_a_single_repo_schedule_writes_multi_repo_false(self):
        doc = json.loads(FIX.read_text())
        doc["repos"] = ["Core"]
        for t in doc["tickets"]:
            t["repo"] = "Core"
        (self.core / "PLAN" / "plan" / "schedule.json").write_text(json.dumps(doc))
        data = self.scanned()
        self.assertIn("multiRepo: false\n", (self.core / ".warp" / "config.yaml").read_text())
        self.assertFalse(data["config"]["multiRepo"])
        self.assertNotIn("repos", data)

    def test_repo_in_the_config_names_this_checkout(self):
        # A clone whose origin is named differently (a fork) is still the parent when `repo:` says so.
        git(self.core, "remote", "set-url", "origin", str(self.tmp / "origins" / "core-fork.git"))
        g = scan.from_schedule(FIX, self.core)
        parent, _ = buildsched.pick_repo(g, self.core, {"repo": "Core"})
        self.assertEqual((parent, len(g["tickets"])), ("Core", 6))
        g = scan.from_schedule(FIX, self.core)
        parent, lines = buildsched.pick_repo(g, self.core, {"repo": "Core-UI"})
        self.assertIsNone(parent)
        self.assertIn("this checkout is Core-UI", lines[0])

    def test_scanning_in_the_smaller_repo_refuses_and_names_the_parent(self):
        (self.ui / "PLAN" / "plan").mkdir(parents=True)
        shutil.copy(FIX, self.ui / "PLAN" / "plan" / "schedule.json")
        out = self.scan(cwd=self.ui)
        self.assertEqual(out.returncode, 2, out.stdout)
        self.assertIn("repos: the parent repo is Core (most tickets: Core 5, Core-UI 1), and this checkout is Core-UI.", out.stdout)
        self.assertIn("Run /warp-scan in a Core checkout, or set splitRepos: true", out.stdout)
        self.assertFalse((self.ui / ".warp" / "beam.json").exists())

    def test_finished_tickets_from_the_old_runs_are_kept(self):
        # The old one-run-per-repo mode published Core-UI's run to its own base branch, with AU-01 merged.
        published = {"run": "ui", "tickets": {"AU-01": {"id": "AU-01", "status": "merged", "branch": "warp/ui/AU-01", "jiraKey": "WAR-9",
                                                       "pr": {"url": "https://github.com/acme/Core-UI/pull/7", "mergedAt": "2026-10-14T02:00:00Z"},
                                                       "deps": ["stale"], "locks": ["stale"]}}}
        seed = self.tmp / "ui-pub"
        git(self.tmp, "clone", "-q", str(self.ui_origin), str(seed))
        (seed / ".warp" / "runs" / "ui").mkdir(parents=True)
        (seed / ".warp" / "runs" / "ui" / "beam.json").write_text(json.dumps(published))
        git(seed, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
        git(seed, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "Warp state: ui run")
        git(seed, "push", "-q", "origin", "main")
        # Rows the sibling's beams cannot speak for: a parent ticket, and AU-01 from another repo's program.
        other = {"tickets": {"AD-04": {"id": "AD-04", "status": "merged"}}}
        (seed / ".warp" / "runs" / "other").mkdir(parents=True)
        (seed / ".warp" / "runs" / "other" / "beam.json").write_text(json.dumps(other))
        (seed / ".warp" / "beam.json").write_text(json.dumps({"tickets": {"AU-01": {"id": "AU-01", "repo": "Elsewhere", "status": "skipped"}}}))
        git(seed, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
        git(seed, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "other runs")
        git(seed, "push", "-q", "origin", "main")
        # Core's own beam had AR-00 merged and AD-01 in review.
        (self.core / ".warp" / "beam.json").write_text(json.dumps({"tickets": {
            "AR-00": {"id": "AR-00", "status": "merged", "pr": {"url": "https://github.com/acme/Core/pull/1"}},
            "AD-01": {"id": "AD-01", "status": "review", "branch": "warp/core/AD-01"}}}))
        data = self.scanned()
        au = data["tickets"]["AU-01"]
        self.assertEqual((au["status"], au["branch"], au["jiraKey"], au["pr"]["mergedAt"]), ("merged", "warp/ui/AU-01", "WAR-9", "2026-10-14T02:00:00Z"))
        self.assertEqual((au["locks"], au["after"]), (["apps/au-01"], ["AD-01"]))  # the definition comes from the schedule
        self.assertEqual(data["tickets"]["AR-00"]["status"], "merged")
        self.assertEqual(data["tickets"]["AD-01"]["status"], "queued")
        self.assertEqual(data["tickets"]["AD-04"]["status"], "queued")  # Core-UI's beams do not speak for a Core ticket
        self.assertIn("repos: kept 2 finished tickets from earlier runs (this checkout, ", self.out)
        self.assertIn("Core-UI:.warp/runs/ui/beam.json", self.out)
        self.assertIn("AD-01 (review, warp/core/AD-01)", self.out)


class CheckTest(TwoRepos):
    def setUp(self):
        super().setUp()
        self.data = self.scanned()
        self.cfg = {"multiRepo": True, "launch": "worktree", "subagentVm": False, "pushMerge": False}
        self.seen = []

    def check(self, **kw):
        cfg = dict(self.cfg, **kw)
        return multirepo.check(self.core, json.loads(json.dumps(self.data)), cfg, clone=kw.pop("clone", False) if "clone" in kw else False,
                               gh_view=lambda clone, s: (self.seen.append(s), (True, ""))[1])

    def test_a_good_setup_starts(self):
        problems, lines = self.check()
        self.assertEqual(problems, [])
        self.assertEqual(lines[0], "repos: multi-repo run. Core is the parent (5 tickets, holds the state)")
        self.assertIn("repos: Core-UI (1 tickets) at %s, origin %s, base main: ok" % (self.ui.resolve(), multirepo.slug(str(self.ui_origin))), lines)

    def test_each_missing_dependency_refuses_with_its_reason(self):
        self.assertIn("launch: agent", self.check(launch="agent")[0][0])
        self.assertIn("subagentVm: true", self.check(subagentVm=True)[0][0])
        (self.ui / "apps" / "au-01" / "index.ts").write_text("edited\n")
        self.assertIn("has local changes (apps/au-01/index.ts)", self.check()[0][0])
        git(self.ui, "checkout", "--", ".")
        git(self.ui, "remote", "set-url", "origin", str(self.core_origin))
        self.assertIn("has origin %s, not %s" % (self.core_origin, self.ui_origin), self.check()[0][0])
        git(self.ui, "remote", "set-url", "origin", str(self.ui_origin))
        self.data["tickets"]["AU-01"]["repo"] = "Mystery"
        self.assertIn("tickets name a repo that is not the parent or a sibling: Mystery", self.check()[0][0])
        self.data["tickets"]["AU-01"]["repo"] = "Core-UI"
        self.data["repos"]["siblings"]["Core-UI"]["base"] = "nope"
        self.assertIn("cannot fetch nope", self.check()[0][0])

    def test_a_missing_clone_is_cloned_at_start(self):
        shutil.rmtree(self.ui)
        problems, _ = self.check()
        self.assertIn("no clone at %s" % self.ui.resolve(), problems[0])
        problems, lines = multirepo.check(self.core, self.data, self.cfg, clone=True, gh_view=lambda c, s: (True, ""))
        self.assertEqual(problems, [])
        self.assertIn("repos: cloned Core-UI into %s" % self.ui.resolve(), lines)
        self.assertTrue((self.ui / "apps" / "au-01" / "index.ts").is_file())

    def test_gh_must_see_the_sibling_when_connected(self):
        cfg = dict(self.cfg, pushMerge=True)
        with mock.patch.object(provider, "resolve", lambda root, c=None, gh=None: {"mode": "connected"}):
            ui_url = "https://github.com/acme/Core-UI.git"
            git(self.ui, "remote", "set-url", "origin", ui_url)
            self.data["repos"]["siblings"]["Core-UI"]["url"] = ui_url
            with mock.patch.object(multirepo, "_git", lambda root, *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr="")):
                problems, _ = multirepo.check(self.core, self.data, cfg, clone=False, gh_view=lambda c, s: (False, "HTTP 404"))
        self.assertEqual(problems, ["Core-UI: gh cannot see acme/core-ui (HTTP 404). Warp opens and merges its pull requests there."])

    def test_the_start_runs_the_check_from_the_config(self):
        path = self.core / ".warp" / "beam.json"
        lines = multirepo.start_refusal(path, dict(self.cfg, launch="agent"), clone=False)
        self.assertTrue(lines[-1].startswith("refuse: multi-repo run not started: launch: agent"), lines)
        # Turning it off by hand while sibling tickets are in the run would send them to this repo: refused, beam kept.
        lines = multirepo.start_refusal(path, dict(self.cfg, multiRepo=False))
        self.assertEqual(lines, ["refuse: multi-repo run not started: multiRepo is false in .warp/config.yaml, but this run has 1 tickets "
                                 "in sibling repos (AU-01). Set multiRepo: true again, or /warp-scan to rebuild the run."])
        self.assertTrue(json.loads(path.read_text())["config"]["multiRepo"])

    def test_a_slack_resume_runs_the_same_check(self):
        import inbound

        path = self.core / ".warp" / "beam.json"
        data = json.loads(path.read_text())
        data.update(runState="paused", paused=True)
        path.write_text(json.dumps(data))
        (self.core / ".warp" / "config.yaml").write_text((self.core / ".warp" / "config.yaml").read_text().replace("multiRepo: true", "multiRepo: false"))
        out = inbound.apply_command(path, "warp:resume", by="Shawn", source="slack")
        text = json.dumps(out)
        self.assertIn("warp:resume refused. multi-repo run not started: multiRepo is false", text)
        self.assertTrue(json.loads(path.read_text())["paused"])

    def test_this_checkout_must_be_the_parent(self):
        problems, _ = multirepo.check(self.ui, self.data, self.cfg, clone=False)
        self.assertIn("this checkout is Core-UI, but the parent repo is Core", problems[0])


class LockTest(unittest.TestCase):
    def test_the_same_path_in_two_repos_does_not_collide(self):
        data = {"config": {"multiRepo": True, "maxAgents": 4}, "repos": {"home": "Core", "siblings": {"Core-UI": {"path": "../Core-UI"}}},
                "runState": "running", "gates": [], "tickets": {}}
        def row(tid, repo, status="queued"):
            return {"id": tid, "repo": repo, "status": status, "locks": ["apps/shared"], "deps": [], "after": [], "size": "S", "summary": tid}
        data["tickets"] = {"C-1": row("C-1", "Core", "coding"), "U-1": row("U-1", "Core-UI"), "U-2": row("U-2", "Core-UI"), "C-2": row("C-2", "Core")}
        self.assertEqual(multirepo.scoped_locks(data, data["tickets"]["U-1"], ["apps/shared"]), ["@Core-UI/apps/shared"])
        held = dict(beam_mod.held_locks(data))
        self.assertEqual(held["C-1"], ["apps/shared"])
        ready = [t["id"] for t in beam_mod.ready(data)] if hasattr(beam_mod, "ready") else None
        if ready is not None:
            self.assertIn("U-1", ready)  # Core's C-1 holds apps/shared in Core, not in Core-UI
            self.assertNotIn("C-2", ready)  # the same path in the same repo still waits
            self.assertFalse({"U-1", "U-2"} <= set(ready))  # two Core-UI tickets on one path never start together


class LaunchTest(TwoRepos):
    def setUp(self):
        super().setUp()
        self.data = self.scanned()
        ticket = self.data["tickets"]["AU-01"]
        ticket.update(status="claimed", agent="shuttle-AU-01", claimedAt="2026-10-15T10:00:00Z")
        self.data["config"].update(runner="local", launch="worktree", subagentVm=False, maxAgents=4)
        self.data.update(runState="running", paused=False)
        self.path = self.core / ".warp" / "beam.json"
        self.path.write_text(json.dumps(self.data))

    def launch(self, tid="AU-01", cmd="launch"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.dict(os.environ, {"WARP_PUBLISH": "0"}):
            code = checkout.main([cmd, "--beam", str(self.path), "--id", tid, "--root", str(self.core)])
        return code, buf.getvalue()

    def test_a_sibling_ticket_is_a_worktree_of_the_sibling_clone_under_the_parent(self):
        code, out = self.launch()
        self.assertEqual(code, 0, out)
        wt = (self.core / ".warp" / "worktrees" / "AU-01").resolve()
        self.assertIn("created: worktree %s" % wt, out)
        self.assertIn("repo: Core-UI (sibling clone %s)" % self.ui.resolve(), out)
        self.assertEqual(git(wt, "remote", "get-url", "origin").stdout.strip(), str(self.ui_origin))
        self.assertTrue((wt / "apps" / "au-01" / "index.ts").is_file())  # Core-UI's tree, not Core's
        branch = json.loads(self.path.read_text())["tickets"]["AU-01"]["branch"]
        self.assertEqual(git(self.ui, "rev-parse", "--verify", "-q", "refs/heads/%s" % branch, check=False).returncode, 0)
        self.assertNotEqual(git(self.core, "rev-parse", "--verify", "-q", "refs/heads/%s" % branch, check=False).returncode, 0)
        prompt = out.split("\n", out.count("\n"))
        self.assertTrue(any(line.startswith("repo: Core-UI (sibling of Core; clone %s" % self.ui.resolve()) for line in prompt), out)
        self.assertIn("Never copy Core-UI files into the parent checkout.", out)

        # The Shuttle's work: verify passes, and neither the parent nor the sibling clone changed.
        (wt / "apps" / "au-01" / "index.ts").write_text("export const ui = 1\n")
        git(wt, "commit", "-qam", "AU-01 work")
        data = json.loads(self.path.read_text())
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(checkout.verify_ticket(self.core, self.path, data, "AU-01"), 0, buf.getvalue())
        (wt / "outside.txt").write_text("no\n")
        git(wt, "add", "outside.txt")
        git(wt, "commit", "-qm", "escape")
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(checkout.verify_ticket(self.core, self.path, json.loads(self.path.read_text()), "AU-01"), 2)
        self.assertIn("escaped: outside.txt", buf.getvalue())

        # Removing it removes the sibling's worktree.
        code, out = self.launch(cmd="remove")
        self.assertEqual(code, 0, out)
        self.assertFalse(wt.exists())
        self.assertNotIn(str(wt), git(self.ui, "worktree", "list").stdout)

    def test_the_parent_never_gets_the_sibling_files(self):
        code, out = self.launch()
        self.assertEqual(code, 0, out)
        wt = (self.core / ".warp" / "worktrees" / "AU-01").resolve()
        (wt / "apps" / "au-01" / "page.tsx").write_text("page\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "AU-01")
        import state_commit

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(state_commit.publish_base(self.core, str(self.path), force=True), 0)
        core_files = git(self.core_origin, "ls-tree", "-r", "--name-only", "main").stdout.split()
        self.assertTrue(any(f.startswith(".warp/") for f in core_files), core_files)
        self.assertFalse([f for f in core_files if f.startswith("apps/") or "worktrees" in f], core_files)
        self.assertEqual([l for l in git(self.core, "status", "--porcelain").stdout.splitlines() if " .warp/" not in l], [])
        ui_files = git(self.ui_origin, "ls-tree", "-r", "--name-only", "main").stdout.split()
        self.assertFalse([f for f in ui_files if f.startswith(".warp")], ui_files)

    def test_agent_and_vm_launches_refuse_a_sibling_ticket(self):
        data = json.loads(self.path.read_text())
        data["config"].update(runner="cloud", launch="agent")
        self.path.write_text(json.dumps(data))
        code, out = self.launch()
        self.assertEqual(code, 2)
        self.assertIn("refuse: AU-01 is a Core-UI ticket. A multi-repo run starts sibling tickets as worktrees on this machine", out)
        code, out = self.launch(cmd="add")
        self.assertEqual(code, 2)


class MergeRoutingTest(TwoRepos):
    def setUp(self):
        super().setUp()
        self.data = self.scanned()
        self.path = self.core / ".warp" / "beam.json"
        for tid, url in (("AU-01", "https://github.com/acme/Core-UI/pull/7"), ("AD-04", "https://github.com/acme/Core/pull/7")):
            t = self.data["tickets"][tid]
            t.update(status="merging", branch="warp/%s" % tid)
            t["pr"]["url"] = url
        self.data["config"]["checkCommand"] = "make ci"
        self.data["config"]["deleteBranchOnMerge"] = True

    def test_each_ticket_is_checked_merged_and_deleted_in_its_own_repo(self):
        checked, merged, deleted = [], [], []

        def checker(root, ticket, cfg):
            checked.append((ticket["id"], Path(root), cfg.get("checkCommand")))
            return "green", "ok"

        def merger(root, number, cfg):
            merged.append((Path(root), number))
            return "merged", "e" * 40

        lines = ["merge: request AU-01 https://github.com/acme/Core-UI/pull/7", "merge: request AD-04 https://github.com/acme/Core/pull/7"]
        with mock.patch.object(provider, "resolve", lambda root, cfg=None, gh=None: {"mode": "connected", "baseBranch": "main"}), \
                mock.patch.object(provider, "pr_repo_mismatch", lambda root, url: ""):
            out = pipeline.run_merges(self.data, self.path, lines, merger=merger, checker=checker)
        self.assertIn(("AU-01", self.ui.resolve(), None), checked)  # checkCommand is the parent's
        self.assertIn(("AD-04", self.core.resolve(), "make ci"), checked)
        self.assertIn((self.ui.resolve(), "https://github.com/acme/Core-UI/pull/7"), merged)  # by URL, never "#7" in Core
        self.assertIn((self.core.resolve(), "7"), merged)
        self.assertIn("merge: AU-01 merged", out)
        out2 = pipeline.after_merges(self.data, self.path, out, deleter=lambda root, branch, base: (deleted.append((Path(root), branch, base)), (True, ""))[1])
        self.assertIn((self.ui.resolve(), "warp/AU-01", "main"), deleted)
        self.assertIn((self.core.resolve(), "warp/AD-04", "main"), deleted)
        self.assertIn("merge: branch deleted AU-01 warp/AU-01", out2)

    def test_a_parent_ticket_with_another_repo_s_pull_request_is_not_merged_either(self):
        git(self.core, "remote", "set-url", "origin", "https://github.com/acme/Core.git")
        with mock.patch.object(provider, "resolve", lambda root, cfg=None, gh=None: {"mode": "connected"}):
            out = pipeline.run_merges(self.data, self.path, ["merge: request AD-04 https://github.com/acme/Core-UI/pull/7"],
                                      merger=lambda *a: self.fail("merged"), checker=lambda *a: ("green", "ok"))
        self.assertIn("merge: not merged AD-04: the pull request is in acme/Core-UI, but the clone at %s is acme/Core" % self.core.resolve(), out)

    def test_sibling_numbers_become_urls_and_checkcommand_is_the_parent_s(self):
        import orchestrator

        git(self.ui, "remote", "set-url", "origin", "https://github.com/acme/Core-UI.git")
        self.data["repos"]["siblings"]["Core-UI"]["url"] = "https://github.com/acme/Core-UI.git"
        au, ad = self.data["tickets"]["AU-01"], self.data["tickets"]["AD-04"]
        self.assertEqual(multirepo.pr_url(self.data, au, "12"), "https://github.com/acme/Core-UI/pull/12")
        self.assertEqual(multirepo.pr_url(self.data, ad, "12"), "12")
        cfg = {"checkCommand": "make ci", "bugbotRequired": False}
        for t in (au, ad):
            t["pr"].update(ci="green", ciSha="a" * 40, headSha="a" * 40, rollup="absent", ciNone=True)
        self.assertTrue(orchestrator.checks_green(au, cfg))  # Bugbot and CI decide a sibling ticket
        self.assertFalse(orchestrator.checks_green(ad, cfg))  # a parent ticket still needs make ci

    def test_proceed_merges_a_sibling_in_its_own_repo(self):
        import proceed

        au = self.data["tickets"]["AU-01"]
        au.update(status="awaiting_approval", autoMerge=False)
        au["pr"].update(bugbot="pass", ci="green")
        self.data["config"].update(bugbotRequired=False, jiraTransition=False)
        self.path.write_text(json.dumps(self.data))
        with mock.patch.object(proceed, "_mode", lambda root: "local" if Path(root) == self.ui.resolve() else "connected"), \
                mock.patch.object(proceed, "_reply", lambda *a, **k: None):
            _code, text = proceed.describe(self.path, "AU-01", "Shawn")
        self.assertIn("merge-local --root %s --beam %s --id AU-01 --branch warp/AU-01 --base main" % (self.ui.resolve(), self.path), text)

    def test_a_pull_request_from_another_repo_is_not_merged(self):
        git(self.ui, "remote", "set-url", "origin", "https://github.com/acme/Core-UI.git")
        self.assertEqual(provider.pr_repo_mismatch(self.ui, "https://github.com/acme/Core-UI/pull/7"), "")
        why = provider.pr_repo_mismatch(self.ui, "https://github.com/acme/Core/pull/7")
        self.assertEqual(why, "the pull request is in acme/Core, but the clone at %s is acme/Core-UI" % self.ui)
        self.data["tickets"]["AU-01"]["pr"]["url"] = "https://github.com/acme/Core/pull/7"
        with mock.patch.object(provider, "resolve", lambda root, cfg=None, gh=None: {"mode": "connected"}):
            out = pipeline.run_merges(self.data, self.path, ["merge: request AU-01 https://github.com/acme/Core/pull/7"],
                                      merger=lambda *a: self.fail("merged"), checker=lambda *a: ("green", "ok"))
        self.assertIn("merge: not merged AU-01: %s" % why, out)

    def test_pull_requests_open_in_the_sibling_repo(self):
        opened = []

        def opener(root, branch, base, title, body):
            opened.append((Path(root), branch, base))
            return "opened", "https://github.com/acme/Core-UI/pull/8"

        self.data["tickets"]["AU-01"].update(status="coding", pr={"url": None})
        with mock.patch.object(provider, "resolve", lambda root, cfg=None, gh=None: {"mode": "connected", "baseBranch": "main"}):
            pipeline.run_pr_opens(self.data, self.path, ["pr: open AU-01 warp/AU-01"], opener=opener)
        self.assertEqual(opened, [(self.ui.resolve(), "warp/AU-01", "main")])

    def test_a_local_merge_in_the_sibling_keeps_the_state_in_the_parent(self):
        self.data["config"].pop("deleteBranchOnMerge")
        self.data["tickets"]["AU-01"]["pr"].update(bugbot="pass", ci="green")
        self.data["config"].update(bugbotRequired=False, jiraTransition=False)
        self.path.write_text(json.dumps(self.data))
        git(self.ui, "checkout", "-q", "-b", "warp/AU-01")
        (self.ui / "apps" / "au-01" / "page.tsx").write_text("page\n")
        git(self.ui, "add", "-A")
        git(self.ui, "commit", "-qm", "AU-01")
        git(self.ui, "checkout", "-q", "main")
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "provider.py"), "merge-local", "--root", str(self.ui), "--beam", str(self.path),
                              "--id", "AU-01", "--branch", "warp/AU-01", "--base", "main"], capture_output=True, text=True, cwd=self.core)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("merged warp/AU-01 into main locally", out.stdout)
        ui_files = git(self.ui, "ls-tree", "-r", "--name-only", "main").stdout.split()
        self.assertIn("apps/au-01/page.tsx", ui_files)
        self.assertFalse([f for f in ui_files if f.startswith(".warp")], ui_files)
        self.assertEqual(json.loads(self.path.read_text())["tickets"]["AU-01"]["status"], "merged")
        core_files = git(self.core_origin, "ls-tree", "-r", "--name-only", "main").stdout.split()
        self.assertIn(".warp/beam.json", core_files)
        self.assertNotIn("apps/au-01/page.tsx", core_files)

    def test_claims_and_the_board_name_the_sibling_repo(self):
        t = dict(self.data["tickets"]["AU-01"], summary="UI shell", size="M", autoMerge=True, locks=["apps/au-01"], branch=None)
        self.assertIn("(size M, auto-merge; repo Core-UI; locks apps/au-01)", pipeline.claim_herald(t, "Core-UI"))
        self.assertEqual(multirepo.repo_tag(self.data, self.data["tickets"]["AU-01"]), " [Core-UI]")
        self.assertEqual(multirepo.repo_tag(self.data, self.data["tickets"]["AD-04"]), "")


if __name__ == "__main__":
    unittest.main()
