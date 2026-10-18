"""1.6.25: docs/config.html, the chart of every config key, is built from docs/CONFIG.md and kept current."""

from __future__ import annotations

import contextlib
import io
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))

import beam  # noqa: E402
import config_doc  # noqa: E402


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = fn(*args)
    return code, out.getvalue()


class ConfigDocTest(unittest.TestCase):
    def setUp(self):
        self.text = config_doc.SOURCE.read_text()
        self.keys = {row["key"] for row in config_doc.rows(self.text)}
        self.page = config_doc.TARGET.read_text()

    def test_the_page_is_current(self):
        code, out = quiet(config_doc.main, ["--check"])
        self.assertEqual(code, 0, out)

    def test_every_key_in_the_table_has_a_section_and_three_cells(self):
        self.assertEqual(config_doc.unplaced(self.text), [])
        self.assertEqual(config_doc.malformed(self.text), [])
        broken = self.text.replace("--result pass\\|fail", "--result pass|fail")
        self.assertNotEqual(broken, self.text)
        self.assertEqual(len(config_doc.malformed(broken)), 1)

    def test_every_default_key_is_in_the_table_and_on_the_page(self):
        defaults = set(beam.default_config())
        self.assertEqual(sorted(defaults - self.keys), [])
        on_page = set(re.findall(r'<code id="([A-Za-z0-9]+)">', self.page))
        self.assertEqual(on_page, self.keys)
        self.assertIn("%d keys" % len(self.keys), self.page)
        self.assertIn("Warp v%s" % (ROOT / "VERSION").read_text().strip(), self.page)

    def test_combined_rows_are_split_and_values_are_typed(self):
        for key in ("modelS", "modelM", "modelL", "modelXL"):
            self.assertIn(key, self.keys)
        kind, chips = config_doc.values("mergeMethod", "`merge`")
        self.assertEqual(kind, "choice")
        self.assertEqual(chips, ["merge", "squash", "rebase"])
        self.assertEqual(config_doc.values("jiraMatchOnText", "`false`")[0], "switch")

    def test_a_stale_page_fails_the_check(self):
        original = self.page
        try:
            config_doc.TARGET.write_text(original.replace("What it does", "What it did", 1))
            code, out = quiet(config_doc.main, ["--check"])
            self.assertEqual(code, 1)
            self.assertIn("out of date", out)
        finally:
            config_doc.TARGET.write_text(original)


if __name__ == "__main__":
    unittest.main()
