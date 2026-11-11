#!/usr/bin/env python3
"""Every Warp write of a markdown file in docs/spec/ goes through here (1.7.24).

safe_write_spec_md refuses a path whose casing is not the canonical one in paths.SPEC_PATH_REGISTRY, and refuses to
write next to a sibling that differs only in case (a collision already on disk): run `make fix-gates` first.
"""

from __future__ import annotations

from pathlib import Path

from paths import SPEC_PATH_REGISTRY, spec_relative


def _sibling_collision(path: Path):
    folder = path.parent
    if not folder.is_dir():
        return None
    try:
        names = [entry.name for entry in folder.iterdir()]
    except OSError:
        return None
    for name in names:
        if name != path.name and name.lower() == path.name.lower():
            return folder / name
    return None


def check_spec_path(path: Path, strict: bool = True) -> None:
    """Raise ValueError when `path` (ending in docs/spec/<name>) is not spelled canonically, or collides on disk.
    `strict` also refuses a docs/spec name that is not in the registry."""
    rel = spec_relative(path)
    if rel is None:
        raise ValueError(f"not a docs/spec file: {path}")
    canonical = SPEC_PATH_REGISTRY.get(rel.name.lower())
    if canonical is None:
        if strict:
            raise ValueError(f"unknown spec file: {path}")
    elif rel != canonical:
        raise ValueError(
            f"spec writer used non-canonical casing: {rel} "
            f"(canonical: {canonical}); update the writer to use "
            f"SPEC_PATH_REGISTRY, do not write the string directly"
        )
    other = _sibling_collision(Path(path))
    if other is not None:
        raise ValueError(f"case collision on disk: {path} and {other} differ only in case; run `make fix-gates` "
                         f"(or `make fix-case-collisions`) first")


def safe_write_spec_md(path: Path, content: str) -> None:
    # Reject a path whose casing differs from its canonical spelling.
    path = Path(path)
    check_spec_path(path, strict=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_text(path: Path, content: str) -> None:
    """Write any file; one directly in docs/spec/ is checked first (registry names must be canonical, and no casing
    twin may sit next to it). For writers that also write other files, such as the merge driver."""
    path = Path(path)
    if spec_relative(path) is not None and path.suffix.lower() == ".md":
        check_spec_path(path, strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
