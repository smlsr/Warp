"""1.7.24: safe_write_spec_md refuses non-canonical casing and on-disk collisions."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import safe_write  # noqa: E402


class SafeWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_lowercase_and_title_case_are_refused(self):
        for bad in ("docs/spec/gates.md", "docs/spec/Gates.md"):
            with self.assertRaises(ValueError) as ctx:
                safe_write.safe_write_spec_md(self.tmp / bad, "x")
            self.assertIn("non-canonical casing", str(ctx.exception))
            self.assertIn("docs/spec/GATES.md", str(ctx.exception))
        self.assertFalse((self.tmp / "docs" / "spec").exists() and any((self.tmp / "docs" / "spec").iterdir()))

    def test_relative_paths_too(self):
        with self.assertRaises(ValueError):
            safe_write.safe_write_spec_md(Path("docs/spec/gates.md"), "x")

    def test_canonical_is_written(self):
        target = self.tmp / "docs" / "spec" / "GATES.md"
        safe_write.safe_write_spec_md(target, "# Gates\n")
        self.assertEqual(target.read_text(), "# Gates\n")

    def test_unknown_spec_file_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            safe_write.safe_write_spec_md(self.tmp / "docs" / "spec" / "ROADMAP.md", "x")
        self.assertIn("unknown spec file", str(ctx.exception))

    def test_a_twin_on_disk_is_refused(self):
        spec = self.tmp / "docs" / "spec"
        spec.mkdir(parents=True)
        (spec / "gates.md").write_text("old")
        if (spec / "GATES.md").exists():
            self.skipTest("case-insensitive filesystem: the twin cannot exist side by side here")
        with self.assertRaises(ValueError) as ctx:
            safe_write.safe_write_spec_md(spec / "GATES.md", "x")
        self.assertIn("case collision on disk", str(ctx.exception))

    def test_the_merge_driver_writer_checks_spec_files_only(self):
        other = self.tmp / "src" / "a.md"
        safe_write.write_text(other, "fine")
        self.assertEqual(other.read_text(), "fine")
        safe_write.write_text(self.tmp / "docs" / "spec" / "ROADMAP.md", "an unregistered spec file is allowed here")
        with self.assertRaises(ValueError):
            safe_write.write_text(self.tmp / "docs" / "spec" / "gap-report.md", "x")


if __name__ == "__main__":
    unittest.main()
