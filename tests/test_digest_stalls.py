"""1.6.19: the status digest counts stalled tickets, not every open one, and goes out in the pass update."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from test_bugbot_heal import Repo  # noqa: E402
from test_watch import NOW, beam, stalled, ticket  # noqa: E402

import notify  # noqa: E402
import watch  # noqa: E402


def edit_run():
    """The edit run when the parent left the digest out: 13 queued behind G0, WE-01 waiting on Bugbot."""
    rows = [ticket("WE-%02d" % n) for n in range(2, 15)]
    rows.append(ticket("WE-01", status="review", phase="reviewing", pr={"url": "https://github.com/smlsr/WarpDemo/pull/24", "bugbot": "pending"}))
    return beam(rows)


class DigestTest(unittest.TestCase):
    def test_nothing_stalled_counts_zero(self):
        lines = []
        watch._digest(edit_run(), lines, NOW)
        digest = next(line for line in lines if line.startswith("digest: "))
        self.assertIn("stalls=0", digest)
        self.assertIn("open=14", digest)
        self.assertIn("herald: Status: 14 open (13 queued, 1 reviewing), in progress 1/20. No alarms. Nothing stalled.", lines)

    def test_only_stalled_tickets_count_and_an_alarm_counts_once(self):
        data = edit_run()
        slow = stalled(data["tickets"]["WE-01"], "reviewing")
        slow["stalled"] = True
        slow["watch"]["stalled"] = True
        data["tickets"]["WE-02"].update(status="alarm", alarm="ci-red", stalled=True)
        lines = []
        watch._digest(data, lines, NOW)
        digest = next(line for line in lines if line.startswith("digest: "))
        self.assertIn("alarms=1 stalls=1 open=14", digest)
        self.assertEqual((data["statusDigest"]["alarms"], data["statusDigest"]["stalls"]), (["WE-02"], ["WE-01"]))
        status = next(line for line in lines if line.startswith("herald: Status: "))
        self.assertIn("Alarms: WE-02.", status)
        self.assertIn("Stalled: WE-01 (reviewing, ", status)


class DigestPostTest(Repo):
    def test_the_digest_rides_in_the_pass_update_but_not_in_quiet_mode(self):
        self.config.write_text("messenger: slack\nslackChannel: warp-demo\n")
        lines = []
        watch._digest(edit_run(), lines, NOW)
        notify.pass_post(self.tmp, lines, 9)
        body = json.loads((self.warp / "notify-pass.json").read_text())
        self.assertTrue(body["lines"][0].startswith("Status: 14 open"), body["lines"])
        self.config.write_text("messenger: slack\nslackChannel: warp-demo\nnotify: quiet\n")
        self.assertEqual(notify.pass_post(self.tmp, lines, 10), [])


if __name__ == "__main__":
    unittest.main()
