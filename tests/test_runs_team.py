"""Several runs, several people: owner and takeover, delete, Slack addressing, headers, shared base."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from test_runs import CONFIG, RepoCase, git, ticket  # noqa: E402

import herald_fmt  # noqa: E402
import inbound  # noqa: E402
import orchestrator  # noqa: E402
import runs  # noqa: E402
import scan  # noqa: E402
import state_commit  # noqa: E402


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def owner(by="Ann", machine="ann-machine", at=None):
    return {"by": by, "machine": machine, "host": "annmac", "at": at or now(), "session": "s1"}


def run_cli(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            code = runs.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code or 0, buf.getvalue()


class SharedBaseTests(RepoCase):
    def test_two_runs_publish_to_a_base_that_keeps_moving(self):
        two = self.second_checkout()
        self.run_published(self.repo, "core", {"C-1": ticket("C-1")})
        self.run_published(two, "admin", {"A-1": ticket("A-1")})
        self.run_published(self.repo, "core", {"C-1": ticket("C-1", "merged")})
        fresh = self.second_checkout("three")
        self.assertEqual(runs.list_runs(fresh, "origin/main"), ["admin", "core"])
        core = json.loads(git(fresh, "show", "origin/main:.warp/runs/core/beam.json").stdout)
        self.assertEqual(core["tickets"]["C-1"]["status"], "merged")

    def test_a_publish_from_a_branch_checkout_lands_on_a_moved_origin(self):
        two = self.second_checkout()
        self.run_published(two, "admin", {"A-1": ticket("A-1")})
        git(self.repo, "checkout", "-q", "-b", "feature")
        self.run_published(self.repo, "core", {"C-1": ticket("C-1")})
        fresh = self.second_checkout("three")
        self.assertEqual(runs.list_runs(fresh, "origin/main"), ["admin", "core"])


class OwnerTests(RepoCase):
    def test_start_stamps_the_owner_on_a_named_run(self):
        self.write_config(self.repo, run="admin")
        self.write_beam(self.repo, {"A-1": ticket("A-1")}, run="admin", runState="stopped", paused=True)
        scan.set_run(self.repo / ".warp" / "beam.json", "running", None)
        beam = json.loads((self.repo / ".warp" / "beam.json").read_text())
        self.assertTrue(beam["owner"]["machine"])
        self.assertTrue(beam["owner"]["by"])

    def test_a_run_another_machine_is_running_needs_takeover(self):
        self.run_published(self.repo, "admin", {"A-1": ticket("A-1")}, owner=owner())
        two = self.second_checkout()
        refusal, notes = runs.owner_blocker(two, "admin")
        self.assertIn("refuse: admin is running under Ann on annmac", refusal)
        self.assertIn("--takeover", refusal)
        refusal, notes = runs.owner_blocker(two, "admin", takeover=True)
        self.assertIsNone(refusal)
        self.assertIn("takeover: admin is running under Ann", notes[0])

    def test_no_takeover_needed_when_it_is_the_same_machine_or_not_live_or_stopped(self):
        me = runs.identity()
        self.run_published(self.repo, "admin", {"A-1": ticket("A-1")}, owner=owner(machine=me["machine"]))
        two = self.second_checkout()
        self.assertEqual(runs.owner_blocker(two, "admin"), (None, []))
        self.run_published(self.repo, "admin", {"A-1": ticket("A-1")}, owner=owner(), state="stopped")
        self.assertEqual(runs.owner_blocker(two, "admin"), (None, []))
        self.assertEqual(runs.owner_blocker(two, ""), (None, []))

    def test_start_refuses_then_takes_over(self):
        self.run_published(self.repo, "admin", {"A-1": ticket("A-1")}, owner=owner())
        two = self.second_checkout()
        beam_path = two / ".warp" / "beam.json"
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scan.go(beam_path, None, run="admin")
        self.assertEqual(code, 2)
        self.assertIn("refuse: admin is running under Ann", buf.getvalue())
        self.assertFalse(beam_path.exists())

    def test_a_later_owner_on_another_machine_blocks_a_stale_publish(self):
        early = owner(machine="m-early", at="2026-10-12T10:00:00Z")
        late = owner(by="Bo", machine="m-late", at="2026-10-12T11:00:00Z")
        self.run_published(self.repo, "admin", {"A-1": ticket("A-1")}, owner=late)
        beam = {"owner": early}
        self.assertEqual(runs.owner_conflict(self.repo, "admin", beam, "HEAD")["by"], "Bo")
        self.assertIsNone(runs.owner_conflict(self.repo, "admin", {"owner": late}, "HEAD"))
        self.assertIsNone(runs.owner_conflict(self.repo, "admin", {}, "HEAD"))
        stale = self.second_checkout()
        self.write_config(stale, run="admin")
        self.write_beam(stale, {"A-1": ticket("A-1", "coding")}, run="admin", owner=early)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = state_commit.publish_base(stale, force=True)
        self.assertEqual(code, 0)
        self.assertIn("was taken over by Bo", buf.getvalue())
        shown = json.loads(git(self.repo, "show", "HEAD:.warp/runs/admin/beam.json").stdout)
        self.assertEqual(shown["tickets"]["A-1"]["status"], "queued")

    def test_the_old_parent_exits_when_taken_over(self):
        early = owner(machine="m-early", at="2026-10-12T10:00:00Z")
        late = owner(by="Bo", machine="m-late", at="2026-10-12T11:00:00Z")
        self.run_published(self.repo, "admin", {"A-1": ticket("A-1")}, owner=late)
        other = self.second_checkout()
        self.write_config(other, run="admin")
        self.write_beam(other, {"A-1": ticket("A-1")}, run="admin", owner=early)
        data = json.loads((other / ".warp" / "beam.json").read_text())
        self.assertTrue(orchestrator._taken_over(other / ".warp" / "beam.json", data))
        data["owner"] = late
        self.assertFalse(orchestrator._taken_over(other / ".warp" / "beam.json", data))


class DeleteTests(RepoCase):
    def make(self):
        self.run_published(self.repo, "admin", {"A-1": ticket("A-1", "merged")}, state="stopped")

    def test_dry_run_changes_nothing(self):
        self.make()
        code, out = run_cli("delete", "admin", "--root", str(self.repo))
        self.assertEqual(code, 0, out)
        self.assertIn("delete: run admin on origin/main", out)
        self.assertIn("dry run: nothing deleted. Add --apply", out)
        self.assertIsNotNone(self.show("origin/main", ".warp/runs/admin/beam.json"))

    def test_apply_removes_the_folder_in_one_commit_and_pushes(self):
        self.make()
        code, out = run_cli("delete", "admin", "--apply", "--root", str(self.repo))
        self.assertEqual(code, 0, out)
        self.assertIn("deleted: admin from main", out)
        fresh = self.second_checkout("three")
        self.assertEqual(runs.list_runs(fresh, "origin/main"), [])
        self.assertIn("delete run admin", git(fresh, "log", "-1", "--format=%s").stdout)
        self.assertEqual(git(self.repo, "ls-files", ".warp/runs/admin").stdout.strip(), "")
        self.assertFalse((self.repo / ".warp" / "runs" / "admin").exists())

    def test_apply_from_a_branch_checkout(self):
        self.make()
        git(self.repo, "checkout", "-q", "-b", "feature")
        code, out = run_cli("delete", "admin", "--apply", "--root", str(self.repo))
        self.assertEqual(code, 0, out)
        fresh = self.second_checkout("three")
        self.assertEqual(runs.list_runs(fresh, "origin/main"), [])

    def test_a_running_run_is_refused_without_force(self):
        self.run_published(self.repo, "admin", {"A-1": ticket("A-1")})
        code, out = run_cli("delete", "admin", "--apply", "--root", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("refuse: admin is running", out)
        self.assertIsNotNone(self.show("origin/main", ".warp/runs/admin/beam.json"))
        code, out = run_cli("delete", "admin", "--apply", "--force", "--root", str(self.repo))
        self.assertEqual(code, 0, out)
        self.assertIn("warn: admin is running", out)

    def test_a_run_another_run_waits_on_is_refused(self):
        self.make()
        two = self.second_checkout()
        self.write_config(two, CONFIG.replace('scanFolder: "HOS/UI/Admin"', 'scanFolder: ""') + "startAfter: admin:G2\n", run="ui")
        self.write_beam(two, {"U-1": ticket("U-1", deps=["admin:A-1"])}, run="ui", runState="stopped")
        (two / ".warp" / "scan.json").write_text(json.dumps({"scannedAt": "2026-10-12T12:00:00Z"}))
        self.assertEqual(state_commit.publish_base(two, force=True), 0)
        git(self.repo, "pull", "-q", "origin", "main")
        code, out = run_cli("delete", "admin", "--root", str(self.repo))
        self.assertEqual(code, 2, out)
        self.assertIn("refuse: ui waits on admin (G2, ticket A-1)", out)

    def test_branches_are_listed_and_only_deleted_with_the_flag(self):
        self.make()
        git(self.repo, "push", "origin", "HEAD:refs/heads/warp/admin/A-1")
        code, out = run_cli("delete", "admin", "--apply", "--root", str(self.repo))
        self.assertIn("branches: 1 on origin under warp/admin/. They are kept.", out)
        self.assertIn("warp/admin/A-1", git(self.repo, "ls-remote", "--heads", "origin").stdout)
        self.run_published(self.repo, "admin", {"A-1": ticket("A-1", "merged")}, state="stopped")
        code, out = run_cli("delete", "admin", "--apply", "--branches", "--root", str(self.repo))
        self.assertIn("branches: deleted 1", out)
        self.assertNotIn("warp/admin/A-1", git(self.repo, "ls-remote", "--heads", "origin").stdout)

    def test_the_checkout_that_ran_it_keeps_its_files_and_forgets_the_name(self):
        self.make()
        run_cli("delete", "admin", "--apply", "--root", str(self.repo))
        self.assertEqual(runs.read_run(self.repo), "")
        self.assertTrue((self.repo / ".warp" / "previous-admin" / "beam.json").is_file())

    def test_an_unknown_run_is_not_found(self):
        code, out = run_cli("delete", "nope", "--root", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("run: nope not found", out)


class ListTests(RepoCase):
    def test_list_shows_owner_live_and_start_after(self):
        self.run_published(self.repo, "core", {"C-1": ticket("C-1")}, owner=owner())
        code, out = run_cli("list", "--root", str(self.repo))
        self.assertEqual(code, 0)
        self.assertIn("runs: 1 on origin/main", out)
        self.assertIn("run: core (this checkout) state=running live owner=Ann", out)
        self.assertIn("startAfter=-", out)


class AddressTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.run_published(self.repo, "admin", {"A-1": ticket("A-1")})
        self.mine = self.second_checkout()
        self.write_config(self.mine, run="core")
        self.write_beam(self.mine, {"C-1": ticket("C-1")}, run="core")
        self.path = self.mine / ".warp" / "beam.json"

    def decide(self, text):
        return inbound._decide(self.path, text)

    def test_a_command_for_another_run_is_ignored(self):
        for text in ("warp:pause admin", "warp:stop @admin lunch", "warp:pause @typo"):
            out = self.decide(text)
            self.assertTrue(out["ignored"], text)
            self.assertIn("addressed to", out["why"])

    def test_a_command_for_this_run_applies_and_keeps_the_reason(self):
        out = self.decide("warp:pause core overnight")
        self.assertFalse(out["ignored"])
        self.assertEqual(out["decision"]["action"], "pause")
        self.assertEqual(out["decision"]["reason"], "overnight")
        self.assertEqual(out["parsed"]["command"], "warp:pause core")

    def test_an_unnamed_state_command_is_refused_when_more_than_one_run_exists(self):
        self.write_config(self.mine, run="abc")
        self.write_beam(self.mine, {"C-1": ticket("C-1")}, run="abc")
        out = self.decide("warp:stop")
        self.assertFalse(out["ignored"])
        self.assertEqual(out["decision"]["action"], "none")
        self.assertIn("more than one run (abc, admin)", out["decision"]["ack"])
        self.assertIn("warp:stop <run>", out["decision"]["ack"])

    def test_only_one_run_answers_an_unaddressed_command(self):
        # "admin" sorts before "zeta", so zeta stays quiet
        self.write_config(self.mine, run="zeta")
        out = self.decide("warp:pause")
        self.assertTrue(out["ignored"])
        self.assertEqual(out["why"], "not named")

    def test_status_is_answered_by_every_run(self):
        self.assertFalse(self.decide("warp:status")["ignored"])

    def test_a_ticket_command_for_another_runs_ticket_is_ignored(self):
        out = self.decide("warp:proceed A-1")
        self.assertTrue(out["ignored"])
        self.assertEqual(out["why"], "no such ticket in this run")

    def test_a_repo_with_one_run_behaves_as_before(self):
        solo = self.second_checkout("solo")
        # no runs published on this checkout's refs besides admin; name it admin
        self.write_config(solo, run="admin")
        self.write_beam(solo, {"A-1": ticket("A-1")}, run="admin")
        out = inbound._decide(solo / ".warp" / "beam.json", "warp:pause overnight")
        self.assertEqual(out["decision"]["action"], "pause")
        self.assertEqual(out["decision"]["reason"], "overnight")


class HeaderTests(RepoCase):
    def test_a_named_run_adds_its_name_to_the_header(self):
        self.write_config(self.repo, run="admin")
        self.write_beam(self.repo, {"A-1": ticket("A-1")}, run="admin", instance={"id": "abc123"})
        head = herald_fmt.header(self.repo)
        self.assertIn(" | run admin | warp:abc123", head)

    def test_an_unnamed_run_header_is_unchanged(self):
        self.write_config(self.repo)
        self.write_beam(self.repo, {"A-1": ticket("A-1")}, instance={"id": "abc123"})
        head = herald_fmt.header(self.repo)
        self.assertNotIn("run ", head)
        self.assertTrue(head.endswith(" | warp:abc123"))


if __name__ == "__main__":
    unittest.main()
