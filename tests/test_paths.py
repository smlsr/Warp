"""1.7.24: one canonical spelling for every spec file Warp writes."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import paths  # noqa: E402

NAMES = ["GATES.md", "DECISIONS.md", "CHANGES.md", "GAP-REPORT.md", "INFRA.md", "PROCESSES.md", "SEED.md",
         "VERSIONING.md", "VERIFICATION.md", "DOCS-IN-CODE.md"]


class PathsTest(unittest.TestCase):
    def test_registry_has_every_spec_file_in_uppercase(self):
        self.assertEqual(sorted(paths.SPEC_PATH_REGISTRY), sorted(n.lower() for n in NAMES))
        for name in NAMES:
            self.assertEqual(paths.SPEC_PATH_REGISTRY[name.lower()], Path("docs/spec") / name)
            self.assertEqual(paths.SPEC_PATH_REGISTRY[name.lower()].name, name)
        self.assertEqual(paths.SPEC_GATES_MD, Path("docs/spec/GATES.md"))

    def test_canonical(self):
        self.assertEqual(paths.canonical("docs/spec/gates.md"), Path("docs/spec/GATES.md"))
        self.assertEqual(paths.canonical("Docs/Spec/Gates.md"), Path("docs/spec/GATES.md"))
        self.assertEqual(paths.canonical("docs/spec/roadmap.md"), Path("docs/spec/ROADMAP.md"))
        self.assertEqual(paths.canonical("readme.md"), Path("README.md"))
        self.assertEqual(paths.canonical("src/My_File.go"), Path("src/my-file.go"))

    def test_no_writer_spells_a_spec_file_inline(self):
        """Every Warp script composes these paths from paths.py (strings in docs and help text are fine)."""
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        offenders = []
        for py in scripts.glob("*.py"):
            if py.name in {"paths.py"}:
                continue
            for number, line in enumerate(py.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                for name in NAMES:
                    for spelling in {"docs/spec/" + name, "docs/spec/" + name.lower()}:
                        if ('"%s"' % spelling in code or "'%s'" % spelling in code) and "python3 " not in code:
                            offenders.append("%s:%d" % (py.name, number))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
