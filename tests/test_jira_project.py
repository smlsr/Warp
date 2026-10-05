"""jiraProject is filled from local evidence or a saved Jira project list, and never guessed."""

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
import jira_project  # noqa: E402
import jira_sync  # noqa: E402


def run(script, *args, cwd):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *args], cwd=cwd, capture_output=True, text=True)


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / ".warp").mkdir(parents=True)
        self.cfg = self.repo / ".warp/config.yaml"
        self.cfg.write_text('jiraProject: ""\njiraKeyPrefixes: ""\njiraSite: ""\n')

    def text(self):
        return self.cfg.read_text()

    def test_plan_jira_fields_win_over_plan_ids(self):
        (self.repo / "schedule.json").write_text(
            json.dumps(
                {
                    "tickets": [
                        {"id": "WV-01", "jiraKey": "WAR-1", "summary": "Build"},
                        {"id": "WV-02", "jiraKey": "WAR-2", "summary": "Other"},
                    ]
                }
            )
        )
        note = jira_project.ensure(self.repo, write=True, branches=[], commits=[])
        self.assertIn("set jiraProject to WAR", note)
        self.assertIn("detected", self.text())
        self.assertIn('jiraProject: "WAR"', self.text())
        self.assertIn('jiraKeyPrefixes: "WAR"', self.text())
        self.assertIn("detected jiraProject", self.text())
        again = jira_project.ensure(self.repo, write=True, branches=[], commits=[])
        self.assertEqual(again, "")

    def test_plan_ids_alone_are_not_a_jira_project(self):
        (self.repo / "CURSOR_PLAN.md").write_text("- WV-01 Build the widget\n- WV-02 Next\n")
        note = jira_project.ensure(self.repo, write=True, branches=[], commits=[])
        self.assertIn("jiraProject not set: Jira moves are disabled until you set it (candidates: none)", note)
        self.assertIn('jiraProject: ""', self.text())

    def test_equal_jira_prefixes_are_not_guessed(self):
        (self.repo / "schedule.json").write_text(
            json.dumps({"tickets": [{"id": "WV-01", "jiraKey": "WAR-1"}, {"id": "WV-02", "jiraKey": "ABC-2"}]})
        )
        note = jira_project.ensure(self.repo, write=True, branches=[], commits=[])
        self.assertIn("candidates: ABC, WAR", note)
        self.assertIn("project --set WAR", note)
        self.assertIn("project --set ABC", note)
        self.assertIn('jiraProject: ""', self.text())
        self.assertIn("jiraProject not set", (self.repo / ".warp/outbox.md").read_text())

    def test_a_dominant_prefix_is_kept(self):
        tickets = [{"id": f"WV-0{i}", "jiraKey": f"WAR-{i}"} for i in range(1, 5)]
        tickets.append({"id": "WV-09", "jiraKey": "ABC-1"})
        (self.repo / "schedule.json").write_text(json.dumps({"tickets": tickets}))
        note = jira_project.ensure(self.repo, write=True, branches=[], commits=[])
        self.assertIn("set jiraProject to WAR", note)
        self.assertIn('jiraProject: "WAR"', self.text())

    def test_branches_and_commits_count_when_plan_ids_do_not(self):
        (self.repo / "CURSOR_PLAN.md").write_text("- WV-01 Only a plan id\n")
        note = jira_project.ensure(
            self.repo,
            write=True,
            branches=["feature/WAR-15-widget"],
            commits=["WAR-3 Fix the login"],
        )
        self.assertIn("set jiraProject to WAR", note)
        self.assertIn("git", self.text())

    def test_a_set_project_is_not_overwritten(self):
        self.cfg.write_text('jiraProject: "HOS"\njiraKeyPrefixes: "HOS"\n')
        (self.repo / "schedule.json").write_text(json.dumps({"tickets": [{"id": "WV-01", "jiraKey": "WAR-9"}]}))
        note = jira_project.ensure(self.repo, write=True, branches=[], commits=[])
        self.assertEqual(note, "")
        self.assertIn('jiraProject: "HOS"', self.text())
        forced = jira_project.write_project(self.repo, "war", "set with project --set", force=True)
        self.assertIn("was HOS; now WAR", forced)
        self.assertIn('jiraProject: "WAR"', self.text())

    def test_online_one_project_and_one_site(self):
        client = jira_project.ReplayClient(
            {
                "resources": [{"id": "cloud-1", "url": "https://acme.atlassian.net"}],
                "projects": [{"key": "WAR", "name": "Warp"}],
            }
        )
        note = jira_project.apply_remote(self.repo, client)
        self.assertIn("set jiraProject to WAR", note)
        self.assertIn("set jiraSite to https://acme.atlassian.net", note)
        self.assertIn('jiraSite: "https://acme.atlassian.net"', self.text())
        self.assertEqual(jira_sync.settings(self.repo / ".warp/beam.json", {})["jiraProject"], "WAR")
        self.assertEqual(jira_sync.settings(self.repo / ".warp/beam.json", {})["jiraSite"], "https://acme.atlassian.net")

    def test_online_match_uses_a_candidate_and_does_not_replace_a_set_project(self):
        (self.repo / ".warp/jira-project.json").write_text(json.dumps({"candidates": ["WAR"]}))
        client = jira_project.ReplayClient(
            {"projects": [{"key": "WAR", "name": "Widget"}, {"key": "ABC", "name": "Other"}], "resources": []}
        )
        note = jira_project.apply_remote(self.repo, client)
        self.assertIn("set jiraProject to WAR", note)
        self.cfg.write_text(self.text().replace('jiraProject: "WAR"', 'jiraProject: "HOS"'))
        again = jira_project.apply_remote(
            self.repo,
            jira_project.ReplayClient(
                {
                    "projects": [{"key": "ABC", "name": "Other"}],
                    "resources": [{"url": "https://other.atlassian.net"}],
                }
            ),
        )
        self.assertIn("jiraProject is HOS; left as is", again)
        self.assertIn("set jiraSite", again)
        self.assertIn('jiraProject: "HOS"', self.text())

    def test_online_several_projects_ask_instead_of_picking(self):
        note = jira_project.apply_remote(
            self.repo,
            jira_project.ReplayClient(
                {
                    "projects": [{"key": "WAR", "name": "Widget"}, {"key": "ABC", "name": "Other"}],
                    "resources": [
                        {"url": "https://a.atlassian.net"},
                        {"url": "https://b.atlassian.net"},
                    ],
                }
            ),
        )
        self.assertIn("jiraProject not set: Jira moves are disabled until you set it (candidates: ABC, WAR)", note)
        self.assertIn("several Atlassian sites", note)
        self.assertIn('jiraProject: ""', self.text())
        self.assertIn('jiraSite: ""', self.text())
        self.assertIn("project --set WAR", (self.repo / ".warp/outbox.md").read_text())

    def test_commands_list_set_and_apply(self):
        (self.repo / "schedule.json").write_text(
            json.dumps({"tickets": [{"id": "WV-01", "jiraKey": "WAR-1"}, {"id": "WV-02", "jiraKey": "ABC-2"}]})
        )
        listed = run("jira_sync.py", "project", "--root", ".", "--list", cwd=self.repo)
        self.assertIn("candidates: ABC, WAR", listed.stdout)
        self.assertIn('jiraProject: ""', self.text())
        stored = run("jira_sync.py", "project", "--root", ".", "--set", "WAR", cwd=self.repo)
        self.assertIn("set jiraProject to WAR", stored.stdout)
        self.assertIn('jiraProject: "WAR"', self.text())
        self.cfg.write_text('jiraProject: ""\njiraKeyPrefixes: ""\njiraSite: ""\n')
        results = self.repo / "remote.json"
        results.write_text(json.dumps({"projects": [{"key": "WAR", "name": "Warp"}], "resources": []}))
        applied = run("jira_sync.py", "project", "--root", ".", "--apply", "remote.json", cwd=self.repo)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("only visible Jira project", applied.stdout)
        self.assertIn('jiraProject: "WAR"', self.text())

    def test_scan_warns_and_check_repeats_it(self):
        (self.repo / "CURSOR_PLAN.md").write_text("- WV-01 Build\n- WV-02 Next\n")
        scanned = run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(scanned.returncode, 0, scanned.stdout + scanned.stderr)
        self.assertIn("jiraProject not set: Jira moves are disabled until you set it (candidates: none)", scanned.stdout)
        self.assertIn("jira: PROJECT", scanned.stdout)
        checked = run("jira_sync.py", "verify", "--beam", ".warp/beam.json", cwd=self.repo)
        self.assertIn("jiraProject not set: Jira moves are disabled until you set it (candidates: none)", checked.stdout)

    def test_claim_tries_detection_before_the_transition(self):
        (self.repo / "schedule.json").write_text(
            json.dumps({"tickets": [{"id": "WV-01", "summary": "Build", "size": "M", "jiraKey": "WAR-4"}]})
        )
        run("scan.py", "scan", cwd=self.repo)
        self.assertIn('jiraProject: "WAR"', self.text())
        claimed = run(
            "beam.py", "set", "--beam", ".warp/beam.json", "--id", "WV-01", "--status", "claimed", "--agent", "s", cwd=self.repo
        )
        self.assertIn("MUST DO", claimed.stdout)
        self.assertIn("WAR-4", claimed.stdout)


class StoredKeyProjectTests(unittest.TestCase):
    """Matched issue keys set jiraProject even when the project-list tool is missing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / ".warp").mkdir(parents=True)
        self.cfg = self.repo / ".warp/config.yaml"
        self.cfg.write_text('jiraProject: ""\njiraKeyPrefixes: ""\njiraSite: ""\n')

    def text(self):
        return self.cfg.read_text()

    def test_one_project_and_no_project_list_writes_config(self):
        note = jira_project.adopt_from_keys(
            self.repo, ["WAR-17", "WAR-32"], project_list_available=False
        )
        self.assertIn("set jiraProject to WAR (every stored key is in project WAR)", note)
        self.assertNotIn("project --set", note)
        self.assertNotIn("Jira moves are disabled", note)
        self.assertIn('jiraProject: "WAR"', self.text())
        self.assertIn('jiraKeyPrefixes: "WAR"', self.text())
        before = self.text()
        again = jira_project.adopt_from_keys(
            self.repo, ["WAR-17", "WAR-32"], project_list_available=False
        )
        self.assertIn("jiraProject is WAR; left as is", again)
        self.assertNotIn("project --set", again)
        self.assertEqual(self.text(), before)

    def test_apply_without_project_list_writes_the_agreed_key(self):
        payload = {
            "projectsTool": "unavailable",
            "resources": [{"url": "https://smldev.atlassian.net", "id": "cloud"}],
            "searches": [
                {"jql": 'externalId = "WV-01"', "issues": [{"key": "WAR-17"}]},
                {"jql": 'externalId = "WV-16"', "issues": [{"key": "WAR-32"}]},
            ],
        }
        (self.repo / "remote.json").write_text(json.dumps(payload))
        applied = run("jira_sync.py", "project", "--root", ".", "--apply", "remote.json", cwd=self.repo)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("set jiraProject to WAR (every stored key is in project WAR)", applied.stdout)
        self.assertNotIn("project --set", applied.stdout)
        self.assertNotIn("Jira moves are disabled", applied.stdout)
        self.assertIn('jiraProject: "WAR"', self.text())
        self.assertIn("https://smldev.atlassian.net", self.text())
        before = self.text()
        again = run("jira_sync.py", "project", "--root", ".", "--apply", "remote.json", cwd=self.repo)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("jiraProject is WAR; left as is", again.stdout)
        self.assertNotIn("project --set", again.stdout)
        self.assertEqual(self.text(), before)

    def test_mixed_projects_do_not_write(self):
        note = jira_project.adopt_from_keys(
            self.repo, ["WAR-17", "ABC-3"], project_list_available=False
        )
        self.assertIn("jiraProject left empty", note)
        self.assertIn("ABC, WAR", note)
        self.assertNotIn("project --set", note)
        self.assertIn('jiraProject: ""', self.text())
        payload = {
            "projectsTool": "unavailable",
            "searches": [
                {"issues": [{"key": "WAR-17"}]},
                {"issues": [{"key": "ABC-3"}]},
            ],
        }
        (self.repo / "remote.json").write_text(json.dumps(payload))
        applied = run("jira_sync.py", "project", "--root", ".", "--apply", "remote.json", cwd=self.repo)
        self.assertIn("Stored keys are in ABC, WAR", applied.stdout)
        self.assertNotIn("project --set", applied.stdout)
        self.assertIn('jiraProject: ""', self.text())

    def test_existing_different_value_is_not_overwritten(self):
        self.cfg.write_text('jiraProject: "HOS"\njiraKeyPrefixes: "HOS"\n')
        before = self.text()
        note = jira_project.adopt_from_keys(
            self.repo, ["WAR-17", "WAR-32"], project_list_available=False
        )
        self.assertIn("jiraProject is HOS; matched issues are in WAR. Left as is.", note)
        self.assertNotIn("project --set", note)
        self.assertEqual(self.text(), before)

    def test_already_correct_value_is_unchanged(self):
        self.cfg.write_text('jiraProject: "WAR"\n')
        before = self.text()
        note = jira_project.adopt_from_keys(self.repo, ["WAR-17"], project_list_available=False)
        self.assertEqual(note, "jira: jiraProject is WAR; left as is")
        self.assertNotIn("project --set", note)
        self.assertEqual(self.text(), before)

    def test_scan_resolve_writes_one_project_and_a_rescan_leaves_it(self):
        (self.repo / "CURSOR_PLAN.md").write_text("- WV-01 Build the widget\n- WV-02 Next\n")
        scanned = run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(scanned.returncode, 0, scanned.stdout + scanned.stderr)
        self.assertIn('jiraProject: ""', self.text())
        (self.repo / "hit.json").write_text(
            json.dumps(
                {
                    "WV-01": [{"key": "WAR-17", "externalId": "WV-01"}],
                    "WV-02": [{"key": "WAR-32", "externalId": "WV-02"}],
                }
            )
        )
        resolved = run(
            "jira_sync.py", "resolve", "--beam", ".warp/beam.json", "--apply", "hit.json", cwd=self.repo
        )
        self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)
        self.assertIn("set jiraProject to WAR (every stored key is in project WAR)", resolved.stdout)
        self.assertNotIn("project --set", resolved.stdout)
        self.assertNotIn("Jira moves are disabled", resolved.stdout)
        self.assertIn('jiraProject: "WAR"', self.text())
        before = self.text()
        again = run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("jiraProject is WAR; left as is", again.stdout)
        self.assertNotIn("project --set", again.stdout)
        self.assertEqual(self.text(), before)
