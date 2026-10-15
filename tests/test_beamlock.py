"""1.7.0: concurrent beam writers merge instead of overwriting, and decisions share one run lock."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import beam  # noqa: E402
import beamlock  # noqa: E402
from test_alarm_repair import NOW, ticket  # noqa: E402


class MergeTest(unittest.TestCase):
    def test_three_way_rules(self):
        base = {"a": 1, "b": 1, "c": 1, "gone": 1, "events": [1], "lastSeenAt": "2026-10-15T10:00:00Z"}
        ours = {"a": 2, "b": 1, "c": 5, "events": [1, 2], "lastSeenAt": "2026-10-15T10:01:00Z"}
        theirs = {"a": 1, "b": 3, "c": 7, "gone": 1, "new": 9, "events": [1, 3], "lastSeenAt": "2026-10-15T10:02:00Z"}
        stats = {}
        got = beamlock.merge3(base, ours, theirs, (), stats)
        self.assertEqual(got["a"], 2)  # only ours changed
        self.assertEqual(got["b"], 3)  # only theirs changed
        self.assertEqual(got["c"], 5)  # both changed: this save wins
        self.assertNotIn("gone", got)  # deleted here, untouched there
        self.assertEqual(got["new"], 9)  # added there
        self.assertEqual(got["events"], [1, 2, 3])  # both appended
        self.assertEqual(got["lastSeenAt"], "2026-10-15T10:02:00Z")  # the later heartbeat
        self.assertEqual(stats["conflicts"], 1)


class BeamFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "repo" / ".warp" / "beam.json"
        self.path.parent.mkdir(parents=True)
        data = {"version": 1, "runState": "running", "gates": [], "config": {"maxAgents": 4},
                "tickets": {"T-1": ticket("T-1", status="coding", alarm=None, escaped=None),
                            "T-2": ticket("T-2", status="coding", alarm=None, escaped=None)}}
        beam.atomic_write(self.path, json.dumps(data, indent=2) + "\n")
        self.before = json.loads(self.path.read_text())

    def load(self):
        return json.loads(self.path.read_text())

    def other_process(self, code: str):
        """Save from a separate process, as a Shuttle's `beam.py set` would."""
        script = ("import json,sys; sys.path.insert(0, %r); import beam; p=__import__('pathlib').Path(%r); "
                  "d=json.loads(p.read_text()); %s; beam.atomic_write(p, json.dumps(d))") % (str(SCRIPTS), str(self.path), code)
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_save_keeps_what_another_process_saved_meanwhile(self):
        mine = self.load()  # the pass reads the beam
        self.other_process("d['tickets']['T-2']['status']='done'; d['tickets']['T-2']['lastSeenAt']='2026-10-15T10:05:00Z'")
        mine["tickets"]["T-1"]["status"] = "review"  # the pass's own change
        beam.atomic_write(self.path, json.dumps(mine, indent=2) + "\n")
        saved = self.load()
        self.assertEqual(saved["tickets"]["T-1"]["status"], "review")
        self.assertEqual(saved["tickets"]["T-2"]["status"], "done")  # before 1.7.0 the pass wrote this back to coding
        self.assertEqual(saved["tickets"]["T-2"]["lastSeenAt"], "2026-10-15T10:05:00Z")
        self.assertEqual(beamlock.merged_line(self.path), "beam: merged 1 saves by other processes (1 of their changes kept)")
        self.assertEqual(beamlock.merged_line(self.path), "")

    def test_a_pass_that_saves_twice_keeps_what_it_merged_the_first_time(self):
        mine = self.load()  # the pass reads rev N
        ticket_row = mine["tickets"]["T-1"]  # a reference the pass holds across saves
        self.other_process("d['tickets']['T-2']['status']='done'")
        ticket_row["status"] = "review"
        beam.atomic_write(self.path, json.dumps(mine) + "\n", data=mine)  # merges: T-2 done
        self.assertEqual(mine["tickets"]["T-2"]["status"], "done")  # the pass's copy is brought up to date
        self.assertIs(mine["tickets"]["T-1"], ticket_row)  # in place: held references stay live
        ticket_row["shuttle"] = {"pending": True}
        beam.atomic_write(self.path, json.dumps(mine) + "\n", data=mine)
        ticket_row["shuttle"] = {"pending": False}  # undone later in the same pass
        beam.atomic_write(self.path, json.dumps(mine) + "\n", data=mine)
        saved = self.load()
        self.assertEqual(saved["tickets"]["T-2"]["status"], "done")  # before the fix the second save wrote coding back
        self.assertEqual(saved["tickets"]["T-1"]["status"], "review")
        self.assertFalse(saved["tickets"]["T-1"]["shuttle"]["pending"])

    def test_two_copies_in_one_process_keep_each_others_changes(self):
        first, second = self.load(), self.load()
        first["tickets"]["T-1"]["status"] = "review"
        beam.atomic_write(self.path, json.dumps(first) + "\n", data=first)
        second["tickets"]["T-2"]["status"] = "fix"
        beam.atomic_write(self.path, json.dumps(second) + "\n", data=second)
        saved = self.load()
        self.assertEqual((saved["tickets"]["T-1"]["status"], saved["tickets"]["T-2"]["status"]), ("review", "fix"))

    def test_a_status_both_sides_changed_keeps_the_newer_report(self):
        mine = self.load()
        self.other_process("d['tickets']['T-2']['status']='done'; d['tickets']['T-2']['doneAt']='2026-10-15T10:00:00Z'")
        mine["tickets"]["T-2"].update(status="alarm", alarm="stuck")  # the pass reaped it on stale data
        beam.atomic_write(self.path, json.dumps(mine) + "\n", data=mine)
        row = self.load()["tickets"]["T-2"]
        self.assertEqual((row["status"], row.get("alarm"), row["doneAt"]), ("done", None, "2026-10-15T10:00:00Z"))
        self.assertIn("status changed on both sides for T-2", beamlock.merged_line(self.path))

    def test_the_status_rank_does_not_depend_on_who_saved_first(self):
        mine = self.load()
        mine["tickets"]["T-2"].update(status="alarm", alarm="stuck")  # the pass reaps on a stale heartbeat, and saves first
        beam.atomic_write(self.path, json.dumps(mine) + "\n", data=mine)
        # The Shuttle's `beam.py set done`, built on the version before the reap, saves after.
        script = "d=json.loads(%r); d['tickets']['T-2']['status']='done'" % json.dumps(self.before)
        self.other_process(script)
        row = self.load()["tickets"]["T-2"]
        self.assertEqual((row["status"], row.get("alarm")), ("done", None))
        # A lock-escape alarm is a safety stop: a Shuttle's coding does not clear it.
        self.assertEqual(beamlock._status_winner({"status": "coding"}, {"status": "alarm", "alarm": "lock-escape"})["alarm"], "lock-escape")
        self.assertEqual(beamlock._status_winner({"status": "review"}, {"status": "coding"})["status"], "review")

    def test_a_truncated_kept_version_does_not_break_saves(self):
        mine = self.load()
        for kept in (self.path.parent / ".beam-revs").glob("*.gz"):
            kept.write_bytes(kept.read_bytes()[:20])
        beamlock._cache.clear()
        self.other_process("d['tickets']['T-2']['status']='done'")
        mine["tickets"]["T-1"]["status"] = "review"
        beam.atomic_write(self.path, json.dumps(mine) + "\n", data=mine)  # no base: the two-way merge
        saved = self.load()
        self.assertEqual(saved["tickets"]["T-1"]["status"], "review")

    def test_a_beam_loaded_from_main_is_a_base(self):
        import state_commit  # noqa: F401
        import beamlock as bl

        remote = dict(self.load(), _revKey="remote000000")
        bl.replace_whole(self.path, remote, beam._replace)
        mine = self.load()
        self.assertNotEqual(mine["_revKey"], "remote000000")
        self.other_process("d['tickets']['T-2']['status']='done'")
        mine["tickets"]["T-1"]["status"] = "review"
        beam.atomic_write(self.path, json.dumps(mine) + "\n", data=mine)
        saved = self.load()
        self.assertEqual((saved["tickets"]["T-1"]["status"], saved["tickets"]["T-2"]["status"]), ("review", "done"))

    def test_with_no_base_kept_the_file_only_fields_survive(self):
        mine = self.load()
        mine["_revKey"] = "unknownkey00"  # a version no process here kept
        self.other_process("d['tickets']['T-2']['note']='x'")
        mine["tickets"]["T-1"]["summary"] = "changed here"
        beam.atomic_write(self.path, json.dumps(mine) + "\n")
        saved = self.load()
        self.assertEqual((saved["tickets"]["T-1"]["summary"], saved["tickets"]["T-2"]["note"]), ("changed here", "x"))

    def test_published_copies_are_written_as_given(self):
        pub = self.path.parent / "runs" / "core" / "beam.json"
        beam.atomic_write(pub, json.dumps({"tickets": {}}) + "\n")
        self.assertNotIn("_rev", json.loads(pub.read_text()))
        self.assertGreaterEqual(self.load()["_rev"], 1)


class RunLockTest(BeamFile):
    def test_it_is_reentrant_here_and_exclusive_across_processes(self):
        with beamlock.run_lock(self.path):
            with beamlock.run_lock(self.path):
                pass
        holder = subprocess.Popen([sys.executable, "-c", (
            "import sys,time; sys.path.insert(0, %r); import beamlock\n"
            "with beamlock.run_lock(%r):\n    print('held', flush=True); time.sleep(1.5)") % (str(SCRIPTS), str(self.path))],
            stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        started = time.monotonic()
        out = subprocess.run([sys.executable, str(SCRIPTS / "alarm_repair.py"), "next", "--beam", str(self.path), "--now", NOW],
                             capture_output=True, text=True)
        waited = time.monotonic() - started
        holder.wait()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertGreater(waited, 1.0)  # `next` run by hand waits for the pass that holds the run lock


if __name__ == "__main__":
    unittest.main()
