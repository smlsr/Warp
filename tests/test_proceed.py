"""Manual merge moves Jira to Done, and warp:proceed resolves the id the person wrote."""

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
import beam  # noqa: E402
import install  # noqa: E402
import jira_sync  # noqa: E402
import proceed  # noqa: E402


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


def ticket(tid, **extra):
    row = {
        "id": tid,
        "status": "awaiting_approval",
        "summary": f"ship {tid}",
        "size": "L",
        "module": "app",
        "hours": 1,
        "tokens": 0,
        "minutes": 0,
        "autoMerge": False,
        "deps": [],
        "locks": [f"src/{tid}"],
        "critical": False,
        "rankDays": 0,
        "complexity": "HIGH",
        "agent": "shuttle-1",
        "branch": f"warp/{tid}",
        "attempts": 0,
        "alarm": None,
        "pr": {
            "url": "https://github.com/acme/app/pull/7",
            "bugbot": "pass",
            "ci": "green",
            "sha": "head111",
        },
        "jira": {"status": "QA Ready", "qaReadyAt": "2026-01-01T00:00:00Z"},
        "jiraKey": "WAR-1",
        "jiraKeySource": "manual",
        "jiraMapping": "mapped",
    }
    row.update(extra)
    if "pr" in extra:
        row["pr"] = {**{
            "url": "https://github.com/acme/app/pull/7",
            "bugbot": "pass",
            "ci": "green",
            "sha": "head111",
        }, **extra["pr"]}
    return row


class ProceedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        warp = self.repo / ".warp"
        warp.mkdir()
        self.child = ticket(
            "WV-02",
            status="queued",
            jiraKey="WAR-2",
            deps=["WV-01"],
            locks=["src/other"],
            pr={"bugbot": None, "ci": None, "url": None, "sha": None},
            jira={},
        )
        self.parent = ticket("WV-01")
        self.write_beam({"WV-01": self.parent, "WV-02": self.child})
        (warp / "config.yaml").write_text('jiraProject: "WAR"\n')

    def write_beam(self, tickets):
        body = {
            "version": 1,
            "tickets": tickets,
            "gates": [],
            "runState": "running",
            "paused": False,
            "config": {"maxAgents": 18, "jiraProject": "WAR"},
            "program": {"criticalPath": []},
        }
        (self.repo / ".warp/beam.json").write_text(json.dumps(body, indent=2) + "\n")

    def beam(self):
        return json.loads((self.repo / ".warp/beam.json").read_text())

    def test_id_variants_and_a_refusal_does_not_merge(self):
        for token, how in (
            ("WV-01", "id"),
            ("warp:proceed WV-01", "id"),
            ("WAR-1", "jira"),
            ("war-1", "jira"),
            ("#01", "number"),
            ("01", "number"),
        ):
            self.write_beam({"WV-01": ticket("WV-01"), "WV-02": self.child})
            code, text = proceed.describe(self.repo / ".warp/beam.json", token, "alex")
            self.assertEqual(code, 0, text)
            self.assertIn(f"matched by {how}", text)
            self.assertIn("reply:", text)
            self.assertIn("Jira status Done", text)
            saved = self.beam()["tickets"]["WV-01"]
            self.assertEqual(saved["status"], "awaiting_approval")
            self.assertEqual(saved["pr"]["proceededBy"], "alex")

        self.write_beam({"WV-01": ticket("WV-01", status="coding", pr={"bugbot": None, "ci": None})})
        before = self.beam()["tickets"]["WV-01"]["status"]
        code, text = proceed.describe(self.repo / ".warp/beam.json", "WV-01", None)
        self.assertEqual(code, 1, text)
        self.assertIn("not awaiting approval", text)
        self.assertIn("coding", text)
        self.assertIn("Not merged", text)
        self.assertEqual(self.beam()["tickets"]["WV-01"]["status"], before)
        self.assertNotIn("proceededBy", self.beam()["tickets"]["WV-01"].get("pr") or {})

        self.write_beam({"WV-01": ticket("WV-01", pr={"bugbot": "pass", "ci": None})})
        code, text = proceed.describe(self.repo / ".warp/beam.json", "WV-01", None)
        self.assertEqual(code, 1, text)
        self.assertIn("gate is closed", text)
        self.assertIn("Not merged", text)
        self.assertEqual(self.beam()["tickets"]["WV-01"]["status"], "awaiting_approval")

        self.write_beam({
            "WV-01": ticket("WV-01"),
            "AB-01": ticket("AB-01", jiraKey="WAR-8"),
        })
        code, text = proceed.describe(self.repo / ".warp/beam.json", "01", None)
        self.assertEqual(code, 1, text)
        self.assertIn("more than one", text)
        self.assertEqual(self.beam()["tickets"]["WV-01"]["status"], "awaiting_approval")

    def test_local_and_connected_commands(self):
        code, text = proceed.describe(self.repo / ".warp/beam.json", "WV-01", "alex")
        self.assertEqual(code, 0, text)
        self.assertIn("local mode", text)
        self.assertIn("merge-local", text)
        self.assertIn("--id WV-01", text)

        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "remote", "add", "origin", "https://github.com/acme/app.git")
        self.write_beam({"WV-01": ticket("WV-01"), "WV-02": self.child})
        code, text = proceed.describe(self.repo / ".warp/beam.json", "warp:proceed WV-01", "alex")
        self.assertEqual(code, 0, text)
        self.assertIn("connected mode", text)
        self.assertIn("--via connected", text)
        self.assertIn("--status merged", text)
        self.assertNotIn("merge-local", text)
        posted = json.loads((self.repo / ".warp/notify-post.json").read_text())
        self.assertIn("Jira status Done", posted["text"])
        self.assertIn("WV-01", posted["text"])

    def test_manual_merge_moves_to_done_and_unblocks(self):
        blocked = beam.ready(self.beam())
        self.assertEqual([t["id"] for t in blocked], [])
        r = run(
            self.repo,
            "beam.py",
            "set",
            "--beam",
            ".warp/beam.json",
            "--id",
            "WV-01",
            "--status",
            "merged",
            "--sha",
            "deadbee",
            "--via",
            "local",
            "--proceeded-by",
            "alex",
            "--merge-method",
            "squash",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("post-merge MUST DO WV-01", r.stdout)
        self.assertIn('move to "Done"', r.stdout)
        self.assertIn("--kind done", r.stdout)
        self.assertIn("locks: released", r.stdout)
        self.assertIn("unblocked: WV-02", r.stdout)
        self.assertIn("ready: WV-02", r.stdout)
        self.assertIn("record --beam <beam> --id WV-01 --event done", r.stdout)
        self.assertIn("record-comment", r.stdout)
        self.assertIn("status_post.py", r.stdout)
        self.assertIn("slack reply: Merged WV-01 sha deadbee. Jira status Done.", r.stdout)
        self.assertIn("Merged (local)", r.stdout)
        self.assertIn("Approved by alex", r.stdout)
        self.assertNotIn("comment on the pull request", r.stdout)
        saved = self.beam()["tickets"]["WV-01"]
        self.assertEqual(saved["status"], "merged")
        self.assertEqual(saved["pr"]["sha"], "deadbee")
        self.assertEqual(saved["pr"]["via"], "local")
        self.assertEqual(saved["pr"]["mergeMethod"], "squash")
        self.assertEqual(saved["pr"]["proceededBy"], "alex")
        self.assertTrue(saved["pr"]["mergedAt"])
        self.assertNotIn("WV-01", [i for i, _ in beam.held_locks(self.beam())])
        self.assertEqual([t["id"] for t in beam.ready(self.beam())], ["WV-02"])
        board = (self.repo / ".warp/BOARD.md").read_text()
        self.assertIn("WV-01", board)
        self.assertIn("sha=deadbee", board)
        self.assertIn("jira=not Done", board)
        status = (self.repo / ".warp/STATUS.md").read_text()
        self.assertIn("sha=deadbee", status)
        self.assertIn("jira=not Done", status)
        report = run(self.repo, "jira_sync.py", "verify", "--beam", ".warp/beam.json", "--id", "WV-01")
        self.assertIn("merged-but-not-done", report.stdout)
        self.assertIn("catchup --beam", report.stdout)
        self.assertIn("--id WV-01", report.stdout)
        caught = run(self.repo, "jira_sync.py", "catchup", "--beam", ".warp/beam.json", "--id", "WV-01")
        self.assertIn('move to "Done"', caught.stdout)
        self.assertIn("event=merged", caught.stdout)
        self.assertIn("post-merge MUST DO", caught.stdout)
        self.assertIn("catchup --beam .warp/beam.json --id WV-01", caught.stdout)
        self.assertIn("no-transition", caught.stdout)

    def test_flag_false_keeps_qa_ready(self):
        (self.repo / ".warp/config.yaml").write_text('jiraProject: "WAR"\njiraDoneOnManualMerge: false\n')
        r = run(
            self.repo,
            "beam.py",
            "set",
            "--beam",
            ".warp/beam.json",
            "--id",
            "WV-01",
            "--status",
            "merged",
            "--sha",
            "abc999",
            "--via",
            "local",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("jiraDoneOnManualMerge is false", r.stdout)
        self.assertNotIn('move to "Done"', r.stdout)
        self.assertIn("event=merged", r.stdout)
        self.assertIn("locks: released", r.stdout)
        self.assertIn("unblocked: WV-02", r.stdout)
        self.assertIn("Jira stays at QA Ready", r.stdout)
        self.assertEqual(self.beam()["tickets"]["WV-01"]["status"], "merged")
        self.assertNotIn("WV-01", [i for i, _ in beam.held_locks(self.beam())])
        report = run(self.repo, "jira_sync.py", "verify", "--beam", ".warp/beam.json", "--id", "WV-01")
        self.assertIn("merged-and-left-at-qa", report.stdout)
        self.assertNotIn("merged-but-not-done", report.stdout)

    def test_qa_ready_to_done_uses_category_and_reports_a_miss(self):
        qa = {"id": "Q", "name": "QA Ready", "to": {"name": "QA Ready", "statusCategory": {"key": "indeterminate"}}}
        finish = {"id": "Z", "name": "Finish", "to": {"name": "Complete", "statusCategory": {"key": "done"}}}
        picked = jira_sync.pick([qa, finish], "Done", {"name": "QA Ready", "category": "indeterminate"}, kind="done")
        self.assertEqual((picked["transition"]["id"], picked["how"]), ("Z", "category"))
        missed = jira_sync.pick([qa], "Done", {"name": "QA Ready", "category": "indeterminate"}, kind="done")
        self.assertEqual(missed["result"], "no-transition")
        recorded = run(
            self.repo,
            "jira_sync.py",
            "record",
            "--beam",
            ".warp/beam.json",
            "--id",
            "WV-01",
            "--event",
            "done",
            "--result",
            "no-transition",
            "--from",
            "QA Ready",
            "--to",
            "Done",
        )
        self.assertIn("no matching transition", recorded.stdout)
        self.assertIn("outbox.md", recorded.stdout)
        box = (self.repo / ".warp/outbox.md").read_text()
        self.assertIn("no matching transition", box)
        self.assertIn("WV-01", box)
        posted = json.loads((self.repo / ".warp/notify-post.json").read_text())
        self.assertIn("Jira not updated", posted["text"])

    def test_init_backfills_the_flag(self):
        example = (ROOT / "assets/config.example.yaml").read_text()
        text, added = install.add_missing_keys('jiraDoneStatus: "Done"\n', example)
        self.assertIn("jiraDoneOnManualMerge", added)
        self.assertIn("jiraDoneOnManualMerge: true", text)
        self.assertIn("let QA set Done", text)


if __name__ == "__main__":
    unittest.main()
