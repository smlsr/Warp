"""1.7.6: /warp-upgrade while a run runs: hold the passes, keep the Shuttles and the listener, refresh, go on."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))

import beamlock  # noqa: E402
import upgrade  # noqa: E402

NOW = "2026-10-17T19:00:00Z"


class CopyTest(unittest.TestCase):
    def test_each_file_is_replaced_in_one_step_and_unchanged_files_are_left(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        src, dst = tmp / "src", tmp / "dst"
        (src / "scripts").mkdir(parents=True)
        (dst / "scripts").mkdir(parents=True)
        (src / "scripts" / "a.py").write_text("new a\n")
        (src / "scripts" / "same.py").write_text("same\n")
        (dst / "scripts" / "a.py").write_text("old a\n")
        (dst / "scripts" / "same.py").write_text("same\n")
        (dst / "scripts" / "gone.py").write_text("old\n")
        changed_inode = os.stat(dst / "scripts" / "a.py").st_ino
        same_inode = os.stat(dst / "scripts" / "same.py").st_ino
        self.assertEqual(upgrade.copy_tree(src, dst), 1)
        self.assertEqual((dst / "scripts" / "a.py").read_text(), "new a\n")
        self.assertNotEqual(os.stat(dst / "scripts" / "a.py").st_ino, changed_inode)  # renamed onto, not rewritten
        self.assertEqual(os.stat(dst / "scripts" / "same.py").st_ino, same_inode)
        self.assertFalse((dst / "scripts" / "gone.py").exists())
        self.assertEqual([p.name for p in (dst / "scripts").iterdir() if "warp-upgrade" in p.name], [])


class LiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "repo"
        self.source = self.tmp / "warp-src"
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "VERSION").write_text("9.9.9\n")
        (self.source / "scripts" / "install.py").write_text("print('init ran')\n")
        (self.source / "scripts" / "config_sync.py").write_text("print('config synced')\n")
        plugin = self.root / ".cursor" / "plugins" / "warp"
        plugin.mkdir(parents=True)
        (plugin / "VERSION").write_text("1.7.5\n")
        warp = self.root / ".warp"
        warp.mkdir()
        self.beam_path = warp / "beam.json"
        self.beam_path.write_text(json.dumps({
            "runState": "running", "config": {}, "gates": [],
            "listener": {"generation": 7, "holder": "listener", "state": "running"},
            "tickets": {"T-1": {"id": "T-1", "status": "coding", "shuttle": {"pending": True, "step": "implement"}},
                        "T-2": {"id": "T-2", "status": "queued"}}}))

    def run_live(self):
        held = []
        real = upgrade.copy_tree

        def copy(src, dst):
            key = str((self.beam_path.parent / beamlock.RUN_LOCK_NAME).resolve())
            held.append(bool(beamlock._held.get(key)))
            return real(src, dst)

        upgrade.copy_tree = copy
        self.addCleanup(setattr, upgrade, "copy_tree", real)
        calls = []

        def runner(cmd):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="init: refreshed for a live upgrade\n" if "install.py" in cmd[1] else "", stderr="")

        out = io.StringIO()
        with redirect_stdout(out):
            code = upgrade.live_upgrade(self.root, self.source, init_runner=runner)
        return code, out.getvalue(), held, calls

    def test_the_run_goes_on_with_its_shuttles_and_listener(self):
        code, text, held, calls = self.run_live()
        self.assertEqual(code, 0, text)
        self.assertEqual(held, [True])  # the copy ran under the run lock
        self.assertIn("upgrade: live, from Warp v1.7.5.", text)
        self.assertIn("1 Shuttle stay out and keep working; the listener (generation 7) stays up.", text)
        self.assertIn("Warp v9.9.9", text)
        self.assertIn("next: do not reload Cursor while Shuttles are out", text)
        self.assertIn("  init: refreshed for a live upgrade", text)
        self.assertEqual(calls[0][2:], ["init", "--root", str(self.root.resolve()), "--live"])
        self.assertTrue(calls[1][1].endswith("config_sync.py"))
        data = json.loads(self.beam_path.read_text())
        self.assertEqual(data["runState"], "running")
        self.assertTrue(data["tickets"]["T-1"]["shuttle"]["pending"])
        self.assertEqual(data["listener"]["holder"], "listener")
        self.assertEqual((data["upgrade"]["state"], data["upgrade"]["from"], data["upgrade"]["to"]), ("done", "1.7.5", "9.9.9"))
        self.assertEqual(data["pluginVersion"], "9.9.9")
        journal = (self.root / ".warp" / "journal.jsonl").read_text()
        self.assertIn('"type":"upgrade"', journal)
        self.assertTrue(any(l.startswith("herald: Warp upgraded live from v1.7.5 to v9.9.9. 1 Shuttle kept working")
                            for l in text.splitlines()), text)

    def test_main_upgrades_live_while_the_run_runs_and_says_so_when_told_not_to(self):
        real = upgrade.live_upgrade
        seen = []
        upgrade.live_upgrade = lambda root, source=None: seen.append("live") or 0
        self.addCleanup(setattr, upgrade, "live_upgrade", real)
        with redirect_stdout(io.StringIO()):
            upgrade.main(["--root", str(self.root), "--source", str(self.source)])
        self.assertEqual(seen, ["live"])
        out = io.StringIO()
        with redirect_stdout(out):
            upgrade.main(["--root", str(self.root), "--source", str(self.source), "--no-live"])
        self.assertIn("warn: the run is running and 1 Shuttles are out", out.getvalue())
        self.assertEqual(seen, ["live"])


class InitLiveTest(unittest.TestCase):
    def test_a_live_refresh_keeps_the_run_and_refuses_another(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp)], check=True)
        (tmp / ".warp").mkdir()
        (tmp / ".warp" / "config.yaml").write_text("run: core\nslackChannel: warp\nteamsChannel: warp\n")
        script = str(ROOT / "scripts" / "install.py")
        done = subprocess.run([sys.executable, script, "init", "--root", str(tmp), "--live", "--no-allow"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("run: core kept (live refresh; the run goes on)", done.stdout)
        self.assertIn("init: refreshed for a live upgrade", done.stdout)
        self.assertNotIn("Reload Cursor so the commands", done.stdout)
        other = subprocess.run([sys.executable, script, "init", "admin", "--root", str(tmp), "--live", "--no-allow"],
                               capture_output=True, text=True)
        self.assertNotEqual(other.returncode, 0)
        self.assertIn("a live refresh keeps it", other.stdout + other.stderr)


if __name__ == "__main__":
    unittest.main()
