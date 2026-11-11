#!/usr/bin/env python3
"""Canonical spellings of the spec files Warp reads and writes (1.7.24).

`docs/spec/GATES.md` and `docs/spec/gates.md` are one file on a case-insensitive filesystem (macOS APFS, Windows NTFS)
and two files on Linux. Two writers using different casings overwrote each other on every tick on a Mac, and pull
requests built on Linux committed both. Every spec file under docs/spec/ has one spelling, uppercase, and every Warp
writer composes its path from here: never an inline string literal.
"""

from __future__ import annotations

from pathlib import Path

SPEC_DIR = Path("docs/spec")

SPEC_GATES_MD = Path("docs/spec/GATES.md")  # canonical casing
SPEC_DECISIONS_MD = SPEC_DIR / "DECISIONS.md"
SPEC_CHANGES_MD = SPEC_DIR / "CHANGES.md"
SPEC_GAP_REPORT_MD = SPEC_DIR / "GAP-REPORT.md"
SPEC_INFRA_MD = SPEC_DIR / "INFRA.md"
SPEC_PROCESSES_MD = SPEC_DIR / "PROCESSES.md"
SPEC_SEED_MD = SPEC_DIR / "SEED.md"
SPEC_VERSIONING_MD = SPEC_DIR / "VERSIONING.md"
SPEC_VERIFICATION_MD = SPEC_DIR / "VERIFICATION.md"
SPEC_DOCS_IN_CODE_MD = SPEC_DIR / "DOCS-IN-CODE.md"

# Lowercase basename -> the canonical Path. A writer that names one of these files with any other casing is refused.
SPEC_PATH_REGISTRY = {
    p.name.lower(): p
    for p in (SPEC_GATES_MD, SPEC_DECISIONS_MD, SPEC_CHANGES_MD, SPEC_GAP_REPORT_MD, SPEC_INFRA_MD, SPEC_PROCESSES_MD,
              SPEC_SEED_MD, SPEC_VERSIONING_MD, SPEC_VERIFICATION_MD, SPEC_DOCS_IN_CODE_MD)
}

# Files kept uppercase everywhere; everything else in a case collision is resolved to kebab-lowercase.
KNOWN_UPPERCASE = {"README.md", "LICENSE", "CHANGELOG.md"}


def canonical(path) -> Path:
    """The canonical spelling of a repo-relative path: a docs/spec/*.md file uppercase, a known root file as it is,
    anything else kebab-lowercase."""
    rel = Path(str(path).replace("\\", "/"))
    if rel.name.lower() in SPEC_PATH_REGISTRY and [p.lower() for p in rel.parts[:-1]] == ["docs", "spec"]:
        return SPEC_PATH_REGISTRY[rel.name.lower()]
    if [p.lower() for p in rel.parts[:-1]] == ["docs", "spec"] and rel.suffix.lower() == ".md":
        return SPEC_DIR / (rel.stem.upper() + ".md")
    for known in KNOWN_UPPERCASE:
        if rel.name.lower() == known.lower():
            return rel.with_name(known)
    return Path(*[part.lower().replace("_", "-").replace(" ", "-") for part in rel.parts])


def spec_relative(path, root=None):
    """The docs/spec/<name> tail of `path` (absolute or relative), or None when it is not directly in docs/spec/."""
    p = Path(path)
    if root is not None:
        try:
            p = p.resolve().relative_to(Path(root).resolve()) if p.is_absolute() else p
        except ValueError:
            pass
    parts = p.parts
    if len(parts) >= 3 and parts[-3].lower() == "docs" and parts[-2].lower() == "spec":
        return Path(parts[-3], parts[-2], parts[-1])
    return None
