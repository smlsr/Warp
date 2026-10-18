"""Run with: python3 -m unittest discover -s tests"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import provider  # noqa: E402

INSTALL = ROOT / "scripts" / "install.py"
NO_GH = {"installed": False, "authenticated": False}
GH_OK = {"installed": True, "authenticated": True}


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "maintenance.auto=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")

    def cfg(self, **kv):
        base = {
            "gitProvider": "auto",
            "githubMcp": "github",
            "bitbucketMcp": "bitbucket",
            "ghCli": "true",
            "pushMerge": "true",
            "baseBranch": "",
        }
        base.update(kv)
        return base

    def test_hosts(self):
        self.assertEqual(provider.detect_provider("git@github.com:smlsr/WarpDemo.git"), "github")
        self.assertEqual(provider.detect_provider("https://bitbucket.org/acme/app.git"), "bitbucket")
        self.assertEqual(provider.detect_provider("git@gitlab.com:acme/app.git"), "gitlab.com")
        self.assertIsNone(provider.detect_provider(""))

    def test_no_remote_is_local(self):
        r = provider.resolve(self.repo, self.cfg(), NO_GH)
        self.assertEqual(r["mode"], "local")
        self.assertEqual(r["reason"], "no origin remote")
        self.assertEqual(r["methods"], [])

    def test_auto_github_offers_mcp_then_gh(self):
        git(self.repo, "remote", "add", "origin", "git@github.com:smlsr/WarpDemo.git")
        r = provider.resolve(self.repo, self.cfg(), GH_OK)
        self.assertEqual((r["mode"], r["provider"]), ("connected", "github"))
        self.assertEqual(r["methods"], ["mcp:github", "gh"])
        r = provider.resolve(self.repo, self.cfg(), NO_GH)
        self.assertEqual(r["methods"], ["mcp:github"])
        self.assertTrue(any("not installed" in m for m in r["missing"]))

    def test_bitbucket_and_explicit_override(self):
        git(self.repo, "remote", "add", "origin", "git@bitbucket.org:acme/app.git")
        r = provider.resolve(self.repo, self.cfg(), NO_GH)
        self.assertEqual(r["provider"], "bitbucket")
        self.assertEqual(r["methods"], ["mcp:bitbucket"])
        git(self.repo, "remote", "set-url", "origin", "git@github.com:acme/app.git")
        r = provider.resolve(self.repo, self.cfg(gitProvider="bitbucket"), NO_GH)
        self.assertEqual(r["provider"], "bitbucket")

    def test_push_merge_false_and_unsupported_host(self):
        git(self.repo, "remote", "add", "origin", "git@github.com:acme/app.git")
        r = provider.resolve(self.repo, self.cfg(pushMerge="false"), GH_OK)
        self.assertEqual(r["mode"], "local")
        self.assertIn("pushMerge is false", r["reason"])
        git(self.repo, "remote", "set-url", "origin", "git@gitlab.com:acme/app.git")
        r = provider.resolve(self.repo, self.cfg(), NO_GH)
        self.assertEqual(r["mode"], "local")
        self.assertIn("not GitHub or Bitbucket", r["reason"])

    def test_bad_provider_value(self):
        git(self.repo, "remote", "add", "origin", "git@github.com:acme/app.git")
        r = provider.resolve(self.repo, self.cfg(gitProvider="svn"), NO_GH)
        self.assertEqual(r["mode"], "local")
        self.assertIn("not github, bitbucket, or auto", r["reason"])


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".warp").mkdir()

    def write(self, text="gitProvider: auto\npushMerge: true\ngithubMcp: github\nbitbucketMcp: bitbucket\nghCli: true\n"):
        (self.repo / ".warp/config.yaml").write_text(text)

    def test_writes_detected_provider_and_warns(self):
        git(self.repo, "remote", "add", "origin", "git@github.com:smlsr/WarpDemo.git")
        self.write()
        steps = dict(provider.init_steps(self.repo, gh=NO_GH))
        self.assertIn("github", steps["done"])
        self.assertIn('gitProvider: "github"', (self.repo / ".warp/config.yaml").read_text())
        self.assertIn("installs and logs in nothing", steps["warn"])

    def test_custom_provider_left_alone(self):
        git(self.repo, "remote", "add", "origin", "git@github.com:acme/app.git")
        self.write('gitProvider: "bitbucket"\npushMerge: true\nbitbucketMcp: bitbucket\nghCli: true\ngithubMcp: github\n')
        steps = provider.init_steps(self.repo, gh=NO_GH)
        self.assertTrue(any(s == "warn" and "left as is" in m for s, m in steps))
        self.assertIn('gitProvider: "bitbucket"', (self.repo / ".warp/config.yaml").read_text())

    def test_no_remote_enables_local_mode(self):
        self.write()
        steps = provider.init_steps(self.repo, gh=GH_OK)
        self.assertTrue(any("pushMerge: false" in m for _, m in steps))
        self.assertIn("pushMerge: false", (self.repo / ".warp/config.yaml").read_text())

    def test_dry_run_writes_nothing(self):
        git(self.repo, "remote", "add", "origin", "git@github.com:acme/app.git")
        self.write()
        before = (self.repo / ".warp/config.yaml").read_text()
        provider.init_steps(self.repo, dry=True, gh=NO_GH)
        self.assertEqual(before, (self.repo / ".warp/config.yaml").read_text())


class MergeLocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "f.txt").write_text("base\n")
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-q", "-m", "base")
        (self.repo / ".warp").mkdir()
        (self.repo / ".warp/beam.json").write_text(json.dumps({"tickets": {"HOS-1": {"summary": "add a line"}}}))

    def branch(self, text, name="warp/HOS-1"):
        git(self.repo, "checkout", "-q", "-b", name)
        (self.repo / "f.txt").write_text(text)
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-q", "-m", "ticket")
        return name

    def test_squash_merges_and_restores_branch_and_notes(self):
        self.branch("base\nticket\n")
        r = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/provider.py"), "merge-local", "--root", ".", "--id", "HOS-1", "--branch", "warp/HOS-1", "--base", "main"],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Not pushed", r.stdout)
        self.assertIn("Nothing was pushed", (self.repo / ".warp/outbox.md").read_text())
        self.assertEqual((self.repo / "f.txt").read_text(), "base\nticket\n")
        head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(head.stdout.strip(), "warp/HOS-1")
        log = subprocess.run(["git", "log", "main", "-1", "--format=%s"], cwd=self.repo, capture_output=True, text=True)
        self.assertIn("[HOS-1]", log.stdout)

    def test_conflict_changes_nothing(self):
        git(self.repo, "checkout", "-q", "-b", "warp/HOS-1")
        (self.repo / "f.txt").write_text("ticket\n")
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-q", "-m", "ticket")
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "f.txt").write_text("main\n")
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-q", "-m", "main")
        before = subprocess.run(["git", "rev-parse", "main"], cwd=self.repo, capture_output=True, text=True).stdout
        ok, detail = provider.merge_local(self.repo, "warp/HOS-1", "main", "msg")
        self.assertFalse(ok)
        self.assertIn("conflict", detail)
        after = subprocess.run(["git", "rev-parse", "main"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(before, after)
        self.assertEqual(subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip(), "main")

    def test_dirty_tree_refuses(self):
        self.branch("base\nticket\n")
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "f.txt").write_text("dirty\n")
        ok, detail = provider.merge_local(self.repo, "warp/HOS-1", "main", "msg")
        self.assertFalse(ok)
        self.assertIn("uncommitted", detail)


class InstallWiringTests(unittest.TestCase):
    def test_warp_init_records_provider_from_origin(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "app"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "remote", "add", "origin", "git@github.com:smlsr/WarpDemo.git")
        r = subprocess.run([sys.executable, "-B", str(INSTALL), "init", "--root", "."], cwd=repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = (repo / ".warp/config.yaml").read_text()
        self.assertIn('gitProvider: "github"', text)
        self.assertIn("github", r.stdout)
        self.assertTrue("installs and logs in nothing" in r.stdout or "logged in" in r.stdout)


if __name__ == "__main__":
    unittest.main()
