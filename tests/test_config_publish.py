"""/warp-start and /warp-resume always publish the config; a config edit alone is a checkpoint."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import runs  # noqa: E402
import scan  # noqa: E402
import state_commit  # noqa: E402
from test_runs import RepoCase, git, ticket  # noqa: E402

CFG = "maxAgents: 4\nbugbotTrigger: auto\nrunner: local\nslackWebhookUrl: https://hooks.slack.com/x\n"


class ConfigPublishTests(RepoCase):
    def origin_file(self, rel):
        git(self.repo, "fetch", "-q", "origin")
        done = git(self.repo, "show", "origin/main:%s" % rel)
        return done.stdout if done.returncode == 0 else None

    def resume(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            scan.set_run(self.repo / ".warp" / "beam.json", "running", "resume")
        return buf.getvalue()

    def test_named_run_resume_pushes_a_config_edited_by_hand(self):
        self.write_config(self.repo, text=CFG, run="view")
        self.write_beam(self.repo, {"WV-01": ticket("WV-01")}, runState="running")
        self.assertEqual(state_commit.publish_base(self.repo, ".warp/beam.json", force=True), 0)
        self.assertIn("bugbotTrigger: auto", self.origin_file(".warp/runs/view/config.yaml"))
        # Edit by hand. Nothing else changed, so the old rule would not have published.
        cfg = self.repo / ".warp" / "config.yaml"
        cfg.write_text(cfg.read_text().replace("bugbotTrigger: auto", "bugbotTrigger: shuttle"))
        out = self.resume()
        self.assertIn("config: published to .warp/runs/view/config.yaml (machine keys and secrets left out)", out)
        published = self.origin_file(".warp/runs/view/config.yaml")
        self.assertIn("bugbotTrigger: shuttle", published)
        self.assertNotIn("runner:", published)
        self.assertNotIn("hooks.slack.com", published)
        self.assertIsNone(self.origin_file(".warp/config.yaml"))

    def test_unnamed_run_resume_pushes_config_published_yaml(self):
        self.write_config(self.repo, text=CFG)
        self.write_beam(self.repo, {"WV-01": ticket("WV-01")}, runState="paused", paused=True)
        out = self.resume()
        self.assertIn("config: published to .warp/config.published.yaml (machine keys and secrets left out)", out)
        published = self.origin_file(".warp/config.published.yaml")
        self.assertIn("maxAgents: 4", published)
        self.assertNotIn("runner:", published)
        self.assertNotIn("hooks.slack.com", published)
        self.assertIsNone(self.origin_file(".warp/config.yaml"), "the config you edit stays gitignored")

    def test_a_config_edit_alone_makes_a_checkpoint_due(self):
        self.write_config(self.repo, text=CFG, run="view")
        data = {"runState": "running", "tickets": {"WV-01": {"status": "queued"}}, "config": {}}
        warp = self.repo / ".warp"
        runs.record_checkpoint(warp, data, "2099-01-01T00:00:00Z")
        self.assertFalse(runs.checkpoint_due(warp, data))
        cfg = warp / "config.yaml"
        cfg.write_text(cfg.read_text() + "maxInProgress: 9\n")
        self.assertTrue(runs.checkpoint_due(warp, data))
        # A machine key is never published, so it does not make one due.
        runs.record_checkpoint(warp, data, "2099-01-01T00:00:00Z")
        cfg.write_text(cfg.read_text().replace("runner: local", "runner: cloud"))
        self.assertFalse(runs.checkpoint_due(warp, data))

    def test_no_git_says_so(self):
        line = scan._config_published_line(self.tmp / "nogit", self.tmp / "nogit" / ".warp" / "beam.json", {}, 0)
        self.assertEqual(line, "config: no .warp/config.yaml to publish")
        (self.tmp / "nogit" / ".warp").mkdir(parents=True)
        (self.tmp / "nogit" / ".warp" / "config.yaml").write_text(CFG)
        line = scan._config_published_line(self.tmp / "nogit", self.tmp / "nogit" / ".warp" / "beam.json", {}, 0)
        self.assertEqual(line, "config: not published (not a git checkout)")

    def test_init_on_a_fresh_checkout_takes_the_published_config(self):
        import install

        self.write_config(self.repo, text=CFG.replace("maxAgents: 4", "maxAgents: 7"))
        self.write_beam(self.repo, {"WV-01": ticket("WV-01")}, runState="paused", paused=True)
        self.resume()
        other = self.second_checkout()
        self.assertTrue((other / ".warp" / "config.published.yaml").is_file())
        self.assertFalse((other / ".warp" / "config.yaml").is_file())
        with redirect_stdout(io.StringIO()):
            steps = install.init(other, None, dry=False, allow=False)
        text = (other / ".warp" / "config.yaml").read_text()
        self.assertIn("maxAgents: 7", text)
        self.assertTrue(any("config.published.yaml" in str(step) for step in steps), steps)


if __name__ == "__main__":
    import unittest

    unittest.main()
