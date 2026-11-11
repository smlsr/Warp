"""1.7.23: the heartbeat window and the boot grace for Shuttles launched with the HEARTBEAT RULE."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agents  # noqa: E402
import beam  # noqa: E402
import orchestrator  # noqa: E402

NOW = "2026-11-11T12:00:00Z"


def ago(seconds):
    return (beam.parse_ts(NOW) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def coding(launched=None, heartbeat=None, contract=True, **extra):
    shuttle = {"step": "implement", "pending": True, "startedAt": launched}
    if contract:
        shuttle["heartbeatContract"] = orchestrator.HEARTBEAT_CONTRACT
    t = {"id": "D-001", "status": "coding", "workerStartedAt": launched, "shuttle": shuttle}
    if heartbeat:
        t["lastSeenAt"] = heartbeat
    t.update(extra)
    return t


DATA = {"config": {}, "tickets": {}}


class ReapGraceTest(unittest.TestCase):
    def dead(self, t, data=DATA):
        return agents.confirmed_dead(t, data, now=NOW)

    def test_no_heartbeat_launched_60s_ago_is_not_reaped(self):
        self.assertFalse(self.dead(coding(launched=ago(60))))

    def test_no_heartbeat_launched_600s_ago_is_reaped(self):
        self.assertTrue(self.dead(coding(launched=ago(600))))

    def test_heartbeat_10s_ago_is_not_reaped(self):
        self.assertFalse(self.dead(coding(launched=ago(3600), heartbeat=ago(10))))

    def test_heartbeat_300s_ago_is_reaped(self):
        self.assertTrue(self.dead(coding(launched=ago(3600), heartbeat=ago(300))))

    def test_busy_holds_the_reaper_off(self):
        busy = (beam.parse_ts(NOW) + timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertFalse(self.dead(coding(launched=ago(3600), heartbeat=ago(300), busyUntil=busy)))

    def test_a_shuttle_launched_before_1723_keeps_stale_minutes(self):
        # Told to beat every 5 minutes: a heartbeat 300s old is alive, 16 minutes is dead (staleMinutes 15).
        self.assertFalse(self.dead(coding(launched=ago(3600), heartbeat=ago(300), contract=False)))
        self.assertTrue(self.dead(coding(launched=ago(3600), heartbeat=ago(16 * 60), contract=False)))

    def test_config_moves_the_windows(self):
        data = {"config": {"shuttleHeartbeatReapAfterSeconds": 900, "shuttleHeartbeatGracePeriodSeconds": 1200}, "tickets": {}}
        self.assertFalse(self.dead(coding(launched=ago(3600), heartbeat=ago(300)), data))
        self.assertFalse(self.dead(coding(launched=ago(600)), data))

    def test_the_slot_count_and_the_recovery_pass_use_the_same_rule(self):
        self.assertTrue(orchestrator.shuttle_is_live(coding(launched=ago(3600), heartbeat=ago(10)), DATA, now=NOW))
        self.assertFalse(orchestrator.shuttle_is_live(coding(launched=ago(3600), heartbeat=ago(300)), DATA, now=NOW))

    def test_launch_marks_the_contract(self):
        t = {"id": "D-002", "status": "claimed", "size": "M", "locks": ["desk/"], "shuttle": {"step": "implement"}}
        data = {"version": 1, "runState": "running", "config": {"runner": "local"}, "tickets": {"D-002": t}}
        agents.launch_lines(data, t, "implement")
        self.assertEqual(t["shuttle"]["heartbeatContract"], orchestrator.HEARTBEAT_CONTRACT)


class BeatRefTest(unittest.TestCase):
    """A VM heartbeat goes to refs/warp/beat/<id>, never the branch, and the parent reads it."""

    def test_heartbeat_push_leaves_the_branch_alone_and_is_read_back(self):
        import contextlib
        import io
        import os

        import ticket_state

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)

        def git(cwd, *a):
            done = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a],
                                  cwd=cwd, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout.strip()

        origin = tmp / "origin.git"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        vm = tmp / "vm"
        git(tmp, "clone", "-q", str(origin), str(vm))
        git(vm, "checkout", "-q", "-b", "main")
        (vm / "a.txt").write_text("a\n")
        git(vm, "add", "-A")
        git(vm, "commit", "-qm", "base")
        git(vm, "push", "-q", "origin", "main")
        git(vm, "checkout", "-q", "-b", "warp/desk/D-001")
        before = git(vm, "rev-parse", "HEAD")
        env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@t")
        cwd = Path.cwd()
        os.chdir(vm)
        old_env = dict(os.environ)
        os.environ.update(env)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = ticket_state.append_status(vm, "D-001", "heartbeat", agent="shuttle-D-001-r1", push=True, busy=900)
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(old_env)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("beat: refs/warp/beat/D-001", out.getvalue())
        self.assertEqual(git(vm, "rev-parse", "HEAD"), before, "the branch got no commit")
        self.assertIn("refs/warp/beat/D-001", git(tmp, "ls-remote", str(origin)))
        # The parent's clone reads the heartbeat (and the --busy) from the beat ref.
        parent = tmp / "parent"
        git(tmp, "clone", "-q", str(origin), str(parent))
        state = ticket_state._beat_state(parent, "D-001", [".warp/runs/desk/tickets/D-001", ".warp/tickets/D-001"])  # warp/desk/...: run "desk"
        self.assertTrue(state["heartbeatAt"])
        self.assertTrue(state["busyUntil"])


if __name__ == "__main__":
    unittest.main()
