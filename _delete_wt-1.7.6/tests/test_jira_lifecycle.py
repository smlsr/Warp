"""Fake Jira and pull-request client for the whole ticket lifecycle.

Run with: python3 -m unittest discover -s tests
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import jira_sync  # noqa: E402

PLAN = """# Tickets

| id | size | summary |
|---|---|---|
| ABC-123 | S | Login |
| T-9 | L | Ship ABC-456 login |
| T-8 | S | no key here |
"""

OFFERED = [
    {"id": "11", "name": "Start progress", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
    {"id": "22", "name": "QA Ready", "to": {"name": "QA Ready", "statusCategory": {"key": "indeterminate"}}},
    {"id": "33", "name": "Done", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
]


def run(repo, script, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "maintenance.auto=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


class FakeClient:
    """Applies whatever .warp/jira-todo.json asks for. No network."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.status = {}
        self.jira_comments = []
        self.pr_comments = []
        self.transition_ids = []

    def follow(self):
        todo = json.loads((self.repo / ".warp/jira-todo.json").read_text())
        for ticket in todo["tickets"]:
            for action in ticket["actions"]:
                if action["type"] == "transition":
                    self._transition(action["plan"])
                else:
                    self._comment(ticket, action)
        return todo

    def _transition(self, plan):
        current = self.status.get(plan["jiraKey"])
        picked = jira_sync.pick(OFFERED, plan["target"], current, kind=plan["kind"])
        if picked["result"] == "pick":
            tr = picked["transition"]
            self.transition_ids.append(tr["id"])
            to = tr["to"]
            self.status[plan["jiraKey"]] = {"name": to["name"], "category": to["statusCategory"]["key"]}
            result, dest = "moved", to["name"]
        else:
            result, dest = picked["result"], (current or {}).get("name")
        r = run(
            self.repo,
            "jira_sync.py",
            "record",
            "--beam",
            ".warp/beam.json",
            "--id",
            plan["id"],
            "--event",
            plan["event"],
            "--result",
            result,
            "--to",
            dest or "",
        )
        if r.returncode != 0:
            raise AssertionError(r.stdout + r.stderr)

    def _comment(self, ticket, action):
        r = run(
            self.repo,
            "jira_sync.py",
            "record-comment",
            "--beam",
            ".warp/beam.json",
            "--id",
            ticket["id"],
            "--where",
            action["where"],
            "--event",
            action["event"],
            "--comment-id",
            f"{action['where']}-{action['event']}-{len(self.jira_comments) + len(self.pr_comments) + 1}",
        )
        if "already recorded" in r.stdout:
            return
        if r.returncode != 0:
            raise AssertionError(r.stdout + r.stderr)
        row = (action["event"], action["body"])
        if action["where"] == "jira":
            self.jira_comments.append(row)
        else:
            self.pr_comments.append(row)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, mode):
        repo = self.tmp / mode
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "remote", "add", "origin", "https://github.com/acme/app.git")
        (repo / "CURSOR_PLAN.md").write_text(PLAN)
        (repo / ".warp").mkdir(parents=True)
        cfg = "jiraProject: ABC\n"
        if mode == "local":
            cfg += "pushMerge: false\n"
        (repo / ".warp/config.yaml").write_text(cfg)
        r = run(repo, "scan.py", "scan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return repo

    def beam(self, repo):
        return json.loads((repo / ".warp/beam.json").read_text())

    def set(self, repo, tid, *extra):
        r = run(repo, "beam.py", "set", "--beam", ".warp/beam.json", "--id", tid, *extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_markdown_keys_come_from_id_and_summary(self):
        repo = self.make("connected")
        tickets = self.beam(repo)["tickets"]
        self.assertEqual(tickets["ABC-123"]["jiraKey"], "ABC-123")
        self.assertTrue(tickets["ABC-123"]["autoMerge"])
        self.assertEqual(tickets["T-9"]["jiraKey"], "ABC-456")
        self.assertFalse(tickets["T-9"]["autoMerge"])
        self.assertIsNone(tickets["T-8"]["jiraKey"])

    def test_branch_name_supplies_the_key_on_claim(self):
        repo = self.make("connected")
        self.set(repo, "T-8", "--status", "claimed", "--agent", "s", "--branch", "warp/T-8-ABC-789")
        self.assertEqual(self.beam(repo)["tickets"]["T-8"]["jiraKey"], "ABC-789")
        todo = json.loads((repo / ".warp/jira-todo.json").read_text())
        self.assertEqual(todo["tickets"][0]["jiraKey"], "ABC-789")
        self.assertEqual(todo["tickets"][0]["actions"][0]["plan"]["target"], "In Progress")

    def test_connected_auto_and_manual_paths(self):
        repo = self.make("connected")
        fake = FakeClient(repo)
        self._auto(repo, fake, where="connected", expect_pr=True)
        self._manual(repo, fake, expect_pr=True)
        again = len(fake.jira_comments)
        prs = len(fake.pr_comments)
        self.set(repo, "ABC-123", "--status", "merged")
        second = fake.follow()
        self.assertEqual(second["tickets"], [])
        self.assertEqual(len(fake.jira_comments), again)
        self.assertEqual(len(fake.pr_comments), prs)
        report = run(repo, "jira_sync.py", "verify", "--beam", ".warp/beam.json", "--id", "ABC-123")
        self.assertIn("missing: (nothing)", report.stdout)
        self.assertIn("doneAt=", report.stdout)
        self.assertNotIn("doneAt=(none)", report.stdout)

    def test_local_manual_merge_moves_jira_to_done(self):
        repo = self.make("local")
        fake = FakeClient(repo)
        self._manual(repo, fake, expect_pr=False)
        self.assertNotIn("merged", [e for e, _ in fake.pr_comments])
        self.assertEqual(fake.status["ABC-456"]["name"], "Done")

    def test_local_mode_comments_on_jira_only(self):
        repo = self.make("local")
        fake = FakeClient(repo)
        self._auto(repo, fake, where="local", expect_pr=False)
        self.assertEqual(fake.pr_comments, [])
        report = run(repo, "jira_sync.py", "verify", "--beam", ".warp/beam.json", "--id", "ABC-123")
        self.assertIn("missing: (nothing)", report.stdout)
        self.assertIn("mode: local", report.stdout)

    def test_catchup_of_an_old_merged_ticket_does_not_reopen(self):
        repo = self.make("connected")
        beam = self.beam(repo)
        t = beam["tickets"]["ABC-123"]
        t["jiraKey"] = None
        t["status"] = "merged"
        t["jira"] = {"status": None, "lastCommentAt": None}
        (repo / ".warp/beam.json").write_text(json.dumps(beam))
        report = run(repo, "jira_sync.py", "verify", "--beam", ".warp/beam.json", "--id", "ABC-123")
        self.assertIn("inferred ABC-123", report.stdout)
        self.assertIn("transition done -> Done", report.stdout)
        self.assertNotIn("transition claim", report.stdout)
        self.assertIn("not a backwards move to In Progress", report.stdout)
        caught = run(repo, "jira_sync.py", "catchup", "--beam", ".warp/beam.json", "--id", "ABC-123", "--write")
        self.assertIn('move to "Done"', caught.stdout)
        self.assertNotIn('move to "In Progress"', caught.stdout)
        self.assertEqual(self.beam(repo)["tickets"]["ABC-123"]["jiraKey"], "ABC-123")
        fake = FakeClient(repo)
        fake.follow()
        self.assertEqual(fake.status["ABC-123"]["name"], "Done")
        self.assertEqual([e for e, _ in fake.jira_comments], ["merged"])
        self.assertIn("Merged (connected)", fake.jira_comments[0][1])

    def test_local_merge_marks_the_beam_and_requests_done(self):
        repo = self.make("local")
        (repo / "f.txt").write_text("base\n")
        git(repo, "add", "f.txt")
        git(repo, "commit", "-q", "-m", "base")
        git(repo, "checkout", "-q", "-b", "warp/ABC-123")
        (repo / "f.txt").write_text("base\nmore\n")
        git(repo, "add", "f.txt")
        git(repo, "commit", "-q", "-m", "ticket")
        self.set(repo, "ABC-123", "--status", "claimed", "--agent", "s1", "--branch", "warp/ABC-123")
        FakeClient(repo).follow()
        self.set(repo, "ABC-123", "--status", "review", "--bugbot", "pass", "--ci", "green")
        r = run(
            repo,
            "provider.py",
            "merge-local",
            "--root",
            ".",
            "--id",
            "ABC-123",
            "--branch",
            "warp/ABC-123",
            "--base",
            "main",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.beam(repo)["tickets"]["ABC-123"]["status"], "merged")
        self.assertIn('move to "Done"', r.stdout)
        self.assertIn("Merged (local)", r.stdout)
        self.assertNotIn("comment on the pull request", r.stdout)
        fake = FakeClient(repo)
        fake.follow()
        self.assertEqual(fake.status["ABC-123"]["name"], "Done")
        self.assertTrue(fake.jira_comments)
        self.assertEqual(fake.pr_comments, [])

    def test_failed_sync_is_in_the_outbox_and_asks_herald(self):
        repo = self.make("local")
        self.set(repo, "ABC-123", "--status", "claimed", "--agent", "s1")
        r = run(
            repo,
            "jira_sync.py",
            "record",
            "--beam",
            ".warp/beam.json",
            "--id",
            "ABC-123",
            "--event",
            "claim",
            "--result",
            "unavailable",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("The claim was not affected", r.stdout)
        self.assertIn("herald:", r.stdout)
        box = (repo / ".warp/outbox.md").read_text()
        self.assertIn("Jira is not connected", box)
        posted = json.loads((repo / ".warp/notify-post.json").read_text())
        self.assertIn("Jira is not connected", posted["text"])
        self.assertIn("Jira not updated", posted["text"])

    def _auto(self, repo, fake, where, expect_pr):
        self.set(repo, "ABC-123", "--status", "claimed", "--agent", "shuttle-1", "--branch", "warp/ABC-123-ABC-123")
        fake.follow()
        self.assertEqual(fake.transition_ids[-1], "11")
        self.assertEqual(fake.status["ABC-123"]["name"], "In Progress")
        claim = fake.jira_comments[-1][1]
        self.assertIn("Warp |", claim)
        self.assertIn("ABC-123", claim)
        self.assertIn("shuttle-1", claim)
        self.assertIn("warp/ABC-123-ABC-123", claim)
        self.set(repo, "ABC-123", "--status", "review", "--pr", "https://github.com/acme/app/pull/5")
        fake.follow()
        self.assertIn("https://github.com/acme/app/pull/5", fake.jira_comments[-1][1])
        if expect_pr:
            self.assertEqual(fake.pr_comments[-1][0], "pr-opened")
            self.assertIn("ABC-123", fake.pr_comments[-1][1])
        self.set(repo, "ABC-123", "--bugbot", "pass", "--ci", "green")
        fake.follow()
        events = [e for e, _ in fake.jira_comments]
        self.assertIn("bugbot", events)
        self.assertIn("ci", events)
        self.set(repo, "ABC-123", "--status", "merged", "--sha", "abc1234")
        todo = fake.follow()
        kinds = [a["plan"]["event"] for t in todo["tickets"] for a in t["actions"] if a["type"] == "transition"]
        self.assertEqual(kinds, ["done"])
        self.assertEqual(fake.transition_ids[-1], "33")
        self.assertEqual(fake.status["ABC-123"]["name"], "Done")
        merged = [body for event, body in fake.jira_comments if event == "merged"][-1]
        self.assertIn(f"Merged ({where})", merged)
        self.assertIn("abc1234", merged)
        self.assertIn("https://github.com/acme/app/pull/5", merged)
        if expect_pr:
            self.assertIn("merged", [e for e, _ in fake.pr_comments])
        else:
            self.assertNotIn("merged", [e for e, _ in fake.pr_comments])
        # A second follow of a fresh set must not add another Done transition.
        before = list(fake.transition_ids)
        self.set(repo, "ABC-123", "--status", "done")
        self.assertEqual(json.loads((repo / ".warp/jira-todo.json").read_text())["tickets"], [])
        self.assertEqual(fake.transition_ids, before)

    def _manual(self, repo, fake, expect_pr):
        before = len(fake.transition_ids)
        self.set(repo, "T-9", "--status", "claimed", "--agent", "shuttle-2", "--branch", "warp/T-9")
        fake.follow()
        self.assertEqual(fake.transition_ids[before:], ["11"])
        self.assertEqual(fake.status["ABC-456"]["name"], "In Progress")
        self.set(repo, "T-9", "--status", "review", "--pr", "https://github.com/acme/app/pull/9")
        fake.follow()
        self.set(repo, "T-9", "--bugbot", "fail")
        self.set(repo, "T-9", "--bugbot", "pass", "--ci", "green")
        self.set(repo, "T-9", "--status", "awaiting_approval")
        todo = fake.follow()
        trans = [a for t in todo["tickets"] for a in t["actions"] if a["type"] == "transition"]
        self.assertEqual([a["plan"]["event"] for a in trans], ["qa-ready"])
        self.assertEqual(fake.transition_ids[-1], "22")
        self.assertEqual(fake.status["ABC-456"]["name"], "QA Ready")
        qa = [body for event, body in fake.jira_comments if event == "qa-ready"][-1]
        self.assertIn("warp:proceed T-9", qa)
        self.assertIn("Bugbot clean, ready for manual review", qa)
        self.assertIn("Findings fixed: 1", qa)
        if expect_pr:
            self.assertIn("qa-ready", [e for e, _ in fake.pr_comments])
        merged_out = self.set(repo, "T-9", "--status", "merged", "--sha", "fff9999", "--via", "connected" if expect_pr else "local")
        self.assertIn("post-merge MUST DO", merged_out.stdout)
        self.assertIn("locks: released", merged_out.stdout)
        todo = fake.follow()
        trans = [a["plan"]["event"] for t in todo["tickets"] for a in t["actions"] if a["type"] == "transition"]
        self.assertEqual(trans, ["done"])
        self.assertEqual(fake.transition_ids[-1], "33")
        self.assertEqual(fake.status["ABC-456"]["name"], "Done")
        merged = [body for event, body in fake.jira_comments if event == "merged"][-1]
        self.assertIn("fff9999", merged)
        self.assertIn("https://github.com/acme/app/pull/9", merged)
        report = run(repo, "jira_sync.py", "verify", "--beam", ".warp/beam.json", "--id", "T-9")
        self.assertIn("missing: (nothing)", report.stdout)
        self.assertNotIn("merged-but-not-done", report.stdout)


if __name__ == "__main__":
    unittest.main()
