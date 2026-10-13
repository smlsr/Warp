"""A live pass merges through the provider and records merged only when the provider shows it merged."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import agents  # noqa: E402
import heal  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402
import watch  # noqa: E402
from test_bugbot_heal import SHA, URL, Repo, row  # noqa: E402

OPEN = {"WV-01": {"headSha": SHA, "rollup": "absent", "checkCount": 0, "ciConfigured": False,
                  "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "prState": "OPEN"}}
MERGED = {"WV-01": dict(OPEN["WV-01"], prState="MERGED", mergeSha="a" * 40)}


def ready_row(**extra):
    fields = dict(status="review", phase="ready", acs=["builds"], gate="G0")
    fields.update(extra)
    ticket = row(**fields)
    ticket["pr"].update({"bugbot": "pass", "bugbotSha": SHA, "check": "green", "checkSha": SHA, "ciNone": True,
                         "acResults": {"AC-1": "pass"}, "acSha": SHA})
    return ticket


class Live:
    """supervise with no --provider: snapshots and the merge command are fixed."""

    def __init__(self, snaps, outcome=("merged", "b" * 40)):
        self.snaps, self.outcome, self.calls = snaps, outcome, []

    def __enter__(self):
        import ticket_state

        self.saved = (pipeline.live_provider, provider.merge_connected, ticket_state._default_checks)
        pipeline.live_provider = lambda data, path: self.snaps
        provider.merge_connected = lambda root, number, cfg=None: (self.calls.append(number), self.outcome)[1]
        ticket_state._default_checks = lambda root, ticket: None
        return self

    def __exit__(self, *exc):
        import ticket_state

        pipeline.live_provider, provider.merge_connected, ticket_state._default_checks = self.saved


class MergeTest(Repo):
    def setUp(self):
        super().setUp()
        import subprocess

        subprocess.run(["git", "remote", "add", "origin", "https://github.com/smlsr/WarpDemo.git"], cwd=self.tmp, check=True)
        self.config.write_text("bugbotRequired: true\ngitProvider: github\n")
        self.write([ready_row()])
        data = self.load()
        data["gates"] = [{"key": "G0", "members": ["WV-01"], "status": "pending", "blocking": True}]
        self.beam_path.write_text(json.dumps(data))

    def test_a_live_pass_merges_through_the_provider_and_confirms(self):
        with Live(OPEN) as live:
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T22:00:00Z")
        self.assertEqual(live.calls, ["4"])
        self.assertIn("merge: request WV-01 %s" % URL, lines)
        self.assertIn("merge: WV-01 merged on the provider (gh pr merge --squash, confirmed MERGED)", lines)
        self.assertIn("merge: WV-01 merged", lines)
        saved = self.load()["tickets"]["WV-01"]
        self.assertEqual((saved["status"], saved["pr"]["mergeConfirmed"], saved["pr"]["sha"]), ("merged", True, "b" * 40))

    def test_a_merge_that_cannot_run_is_not_recorded_and_the_next_pass_confirms_it(self):
        with Live(OPEN, outcome=("rejected", "GraphQL: Resource not accessible by integration")) as live:
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T22:00:00Z")
        self.assertEqual(live.calls, ["4"])
        self.assertIn("merge: not merged WV-01: GraphQL: Resource not accessible by integration", lines)
        self.assertTrue(any(line.startswith("merge: MUST DO WV-01: merge %s (squash)" % URL) for line in lines), lines)
        self.assertNotIn("merge: WV-01 merged", lines)
        self.assertFalse(any(line.startswith("jira: MUST DO WV-01") for line in lines), lines)
        saved = self.load()
        self.assertEqual(saved["tickets"]["WV-01"]["status"], "merging")
        self.assertEqual(saved["gates"][0]["status"], "pending")
        # Not merged yet: wait, do not ask again.
        with Live(OPEN) as live:
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T22:05:00Z")
        self.assertEqual(live.calls, [])
        self.assertIn("merge: wait WV-01 (asked; the provider does not show it merged yet)", lines)
        # Someone merged it on GitHub: the next pass records it.
        with Live(MERGED) as live:
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T22:10:00Z")
        self.assertIn("merge: WV-01 merged on the provider (confirmed)", lines)
        self.assertIn("jira: MUST DO WV-01 transition to Done.", lines)
        self.assertIn("herald: G0 pending cleared. Members merged. Tick ran.", lines)
        saved = self.load()["tickets"]["WV-01"]
        self.assertEqual((saved["status"], saved["pr"]["sha"]), ("merged", "a" * 40))

    def test_a_halt_during_the_pass_merges_nothing(self):
        saved_halt = agents.halted_meanwhile

        def halted(data, path):
            disk = json.loads(Path(path).read_text())
            disk["runState"] = "paused"
            disk["paused"] = True
            return disk

        try:
            agents.halted_meanwhile = halted
            with Live(OPEN) as live:
                lines = orchestrator.supervise(self.beam_path, now="2026-10-13T22:00:00Z")
        finally:
            agents.halted_meanwhile = saved_halt
        self.assertEqual(live.calls, [])
        self.assertIn("pass: aborted. The run was paused during this pass.", lines)
        self.assertNotIn("_liveMerge", self.load())

    def test_the_provider_file_still_decides_in_tests(self):
        lines = orchestrator.supervise(self.beam_path, now="2026-10-13T22:00:00Z", provider={"WV-01": dict(OPEN["WV-01"], merge="merged")})
        self.assertIn("merge: WV-01 merged", lines)


class FalseMergeRepairTest(Repo):
    def test_resume_puts_a_falsely_merged_ticket_back_and_reopens_its_gate(self):
        ticket = ready_row(status="merged", phase="merged")
        ticket["pr"]["sha"] = "merged"
        other = ready_row(status="merged", phase="merged")
        other["id"] = "WV-02"
        other["pr"]["url"] = "https://github.com/smlsr/WarpDemo/pull/5"
        self.write([ticket, other])
        data = self.load()
        data["gates"] = [{"key": "G0", "members": ["WV-01"], "status": "green", "evidence": "members merged: WV-01", "greenAt": "x"}]
        self.beam_path.write_text(json.dumps(data))
        states = {URL: {"prState": "OPEN"}, "https://github.com/smlsr/WarpDemo/pull/5": {"prState": "MERGED", "mergeSha": "c" * 40}}
        lines = heal.check_merged(self.beam_path, fetch=lambda url: states[url])
        self.assertIn("heal: WV-01 was recorded merged, but %s is open on the provider. Back to ready to merge; the next pass merges it." % URL, lines)
        self.assertIn("heal: gate G0 is pending again: WV-01 is not merged.", lines)
        saved = self.load()
        self.assertEqual((saved["tickets"]["WV-01"]["status"], saved["tickets"]["WV-01"]["phase"]), ("review", "ready"))
        self.assertNotIn("sha", saved["tickets"]["WV-01"]["pr"])
        self.assertTrue(saved["tickets"]["WV-02"]["pr"]["mergeConfirmed"])
        self.assertEqual(saved["gates"][0]["status"], "pending")
        # Checked once: a confirmed merge is not asked about again.
        self.assertEqual(heal.check_merged(self.beam_path, fetch=lambda url: (_ for _ in ()).throw(AssertionError(url)) if "pull/5" in url else states[url]), [])


class AgentIdTest(Repo):
    def test_a_new_step_never_reuses_a_finished_shuttles_id(self):
        ticket = ready_row(status="review", phase="reviewing", agent="shuttle-WV-01-r3")
        data = self.write([ticket])
        (self.warp / "agents.json").write_text(json.dumps({"agents": [
            {"id": "shuttle-WV-01-r3", "ticket": "WV-01", "role": "shuttle", "state": "ended", "endReason": "returned"}]}))
        data = self.load()
        decision = agents.prepare_launch(data, data["tickets"]["WV-01"], "verify", beam_path=self.beam_path)
        self.assertEqual(decision["action"], "start")
        self.assertNotEqual(decision["id"], "shuttle-WV-01-r3")
        rows = json.loads((self.warp / "agents.json").read_text())["agents"]
        ids = [r["id"] for r in rows if isinstance(r, dict)]
        self.assertEqual(len(ids), len(set(ids)), ids)


class QueuedStallTest(Repo):
    def test_a_ticket_held_behind_a_dependency_is_not_stalled(self):
        first = ready_row(status="review", phase="reviewing")
        held = row("WV-02", status="queued", phase=None, deps=["WV-01"], pr={}, locks=["app/other"])
        held.pop("phase", None)
        held["watch"] = {"progressAt": "2026-10-13T10:00:00Z"}
        data = self.write([first, held])
        lines = []
        watch._mark_stalls(data, "2026-10-13T22:00:00Z", None, lines)
        self.assertFalse(data["tickets"]["WV-02"]["stalled"])
        self.assertFalse(any("WV-02" in line for line in lines), lines)
        self.assertEqual(data["tickets"]["WV-02"]["watch"]["progressAt"], "2026-10-13T22:00:00Z")


if __name__ == "__main__":
    import unittest

    unittest.main()
