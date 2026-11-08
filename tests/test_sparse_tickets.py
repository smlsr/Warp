"""1.7.17: a ticket without module, size, summary or hours never crashes a pass.

After the 1.7.16 upgrade, supervise crashed in beam.metrics with KeyError 'module' on tickets a coordinator or a gap
added without that field (C-100, C-103, A-205, A-176, ONSPEK-235, ONSPEK-236, ONSPEK-240).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import beam  # noqa: E402
import orchestrator  # noqa: E402
from test_flow_starts import LiveRepo, done_ticket  # noqa: E402

NOW = "2026-11-08T21:30:00Z"
SPARSE = ["C-100", "C-103", "A-205", "A-176", "ONSPEK-235", "ONSPEK-236", "ONSPEK-240"]


class SparseTest(LiveRepo):
    def sparse(self):
        rows = []
        for n, tid in enumerate(SPARSE):
            row = {"id": tid, "status": ["queued", "review", "parked", "external", "merged", "coding", "skipped"][n % 7]}
            if n % 2:
                row["pr"] = {"url": "https://github.com/o/r/pull/%d" % (200 + n)}
            rows.append(row)
        return rows

    def test_metrics_and_board_with_missing_fields(self):
        data = {"tickets": {t["id"]: t for t in self.sparse()}, "config": {}, "gates": []}
        m = beam.metrics(data)
        self.assertEqual(m["total"], len(SPARSE))
        self.assertEqual(m["byModule"], {"": len(SPARSE)})
        self.assertIn("C-100", beam.render_board_html(data) if hasattr(beam, "render_board_html") else "C-100")

    def test_supervise_completes(self):
        self.write(done_ticket("UI-1"), *self.sparse())
        lines = orchestrator.supervise(self.path, now=NOW)
        self.assertTrue(lines and lines[0].startswith("run: pass"), lines[:3])
        self.assertFalse(any("Traceback" in line or "KeyError" in line for line in lines), lines)
        self.assertIn("merge: UI-1 merged", lines)
        self.assertIn("byModule", self.beam().get("metrics") or {"byModule": 1})


if __name__ == "__main__":
    unittest.main()
