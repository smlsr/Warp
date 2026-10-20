"""1.7.9: a run's published copy is never worked as a live beam, and /warp-update-state keeps the live listener.

Before, `/warp-update-state` loaded main's beam over the live one: the listener's lease went back to the
generation of the last publish, and the live listener exited `superseded` at its next poll. A command given
`.warp/runs/<run>/beam.json` worked on the copy and published the run folder inside itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))

import beam  # noqa: E402
import inbound  # noqa: E402
import runs  # noqa: E402
import state_commit  # noqa: E402
import update_state  # noqa: E402

NOW = "2026-10-20T12:00:00Z"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=warp@example.com", "-c", "user.name=Warp",
                           "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args], capture_output=True, text=True)


class PathTest(unittest.TestCase):
    def test_the_published_copy_is_named_and_its_repo_is_the_checkout(self):
        copy = Path("/r/.warp/runs/core/beam.json")
        self.assertTrue(runs.is_published_copy(copy))
        self.assertFalse(runs.is_published_copy(Path("/r/.warp/beam.json")))
        self.assertEqual(runs.repo_root(copy), Path("/r"))
        self.assertEqual(runs.repo_root(Path("/r/.warp/beam.json")), Path("/r"))

    def test_a_command_given_the_copy_works_on_the_live_beam(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        copy = tmp / ".warp" / "runs" / "core" / "beam.json"
        copy.parent.mkdir(parents=True)
        copy.write_text("{}\n")
        self.assertEqual(runs.live_beam(str(copy)), (str(copy), None))  # no live beam: left as given
        (tmp / ".warp" / "beam.json").write_text("{}\n")
        chosen, note = runs.live_beam(str(copy))
        self.assertEqual(chosen, str(tmp / ".warp" / "beam.json"))
        self.assertIn("published copy", note)


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hi\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "init")
        bare = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", str(bare))
        git(self.repo, "remote", "add", "origin", str(bare))
        self.assertEqual(git(self.repo, "push", "-q", "-u", "origin", "main").returncode, 0)
        self.warp = self.repo / ".warp"
        self.warp.mkdir()
        (self.warp / "config.yaml").write_text("run: core\nbaseBranch: main\nslackChannel: warp-run\nmessenger: slack\n")
        self.live = self.warp / "beam.json"

    def beam(self, generation, **extra):
        body = {"version": 1, "run": "core", "runState": "running", "paused": False, "gates": [],
                "config": {"baseBranch": "main", "runner": "local", "listenerFastSeconds": 4, "listenerSlowSeconds": 120},
                "tickets": {"T-1": {"id": "T-1", "status": "review", "size": "M", "module": "app", "locks": [], "deps": [], "summary": "t"}},
                "parentSession": "s-live", "pass": {"startedAt": NOW, "number": 40},
                "listener": {"generation": generation, "holder": "listener", "state": "running", "agentId": "listener",
                             "generationStartedAt": NOW, "pollStartedAt": NOW, "lastSeenAt": NOW, "parentSeenAt": NOW,
                             "reads": 1, "session": "s-live", "cursor": "1760000000.000100"}}
        body.update(extra)
        return body

    def publish_main(self, data):
        folder = self.warp / "runs" / "core"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "beam.json").write_text(json.dumps(data, indent=2) + "\n")
        git(self.repo, "add", ".warp/runs/core/beam.json")
        git(self.repo, "commit", "-q", "-m", "Warp state: core run")
        self.assertEqual(git(self.repo, "push", "-q", "origin", "main").returncode, 0)


class PublishTest(Repo):
    def test_publishing_the_copy_is_refused_and_never_nests_the_folder(self):
        self.publish_main(self.beam(3))
        copy = self.warp / "runs" / "core" / "beam.json"
        self.assertEqual(state_commit.publish_base(copy.parent.parent.parent.parent, str(copy)), 2)
        self.assertEqual(state_commit.publish_base(self.warp / "runs" / "core", str(copy)), 2)
        self.assertFalse((self.warp / "runs" / "core" / ".warp").exists())

    def test_update_state_refuses_the_copy(self):
        self.publish_main(self.beam(3))
        self.assertEqual(update_state.update_state(self.warp / "runs" / "core" / "beam.json", push=False), 2)


class KeepListenerTest(Repo):
    def test_update_state_keeps_the_live_listener_and_it_keeps_listening(self):
        self.publish_main(self.beam(3, parentSession="s-old", pass_={}))
        live = self.beam(9)
        live["autoScan"] = {"at": NOW, "count": 7}
        self.live.write_text(json.dumps(live, indent=2) + "\n")
        self.assertEqual(update_state.update_state(self.live, push=False), 0)
        data = json.loads(self.live.read_text())
        raw = data["listener"]
        self.assertEqual((raw["generation"], raw["holder"], raw["cursor"]), (9, "listener", "1760000000.000100"))
        self.assertEqual(data["parentSession"], "s-live")
        self.assertEqual(data["autoScan"]["count"], 7)
        # The live listener's next poll goes on: before 1.7.9 it read generation 3 and exited superseded.
        lines = inbound.poll_lines(self.live, lease=9, holder="listener", now="2026-10-20T12:01:00Z")
        self.assertTrue(lines[0].startswith("reap: continue"), lines)
        self.assertFalse(any("superseded" in line for line in lines), lines)

    def test_the_cli_given_the_copy_polls_the_live_beam(self):
        self.publish_main(self.beam(3))
        fresh = self.beam(9)
        stamp = beam.utcnow()  # the CLI reads the real clock
        fresh["listener"].update(generationStartedAt=stamp, pollStartedAt=stamp, lastSeenAt=stamp, parentSeenAt=stamp)
        self.live.write_text(json.dumps(fresh, indent=2) + "\n")
        copy = self.warp / "runs" / "core" / "beam.json"
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "inbound.py"), "poll", "--beam", str(copy),
                               "--lease", "9", "--holder", "listener"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("using this checkout's live beam", done.stderr)
        self.assertNotIn("superseded", done.stdout)
        self.assertEqual(json.loads(self.live.read_text())["listener"]["reads"], 2)
        self.assertEqual(json.loads(copy.read_text())["listener"]["generation"], 3)  # the copy is untouched


if __name__ == "__main__":
    unittest.main()
