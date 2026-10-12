# SPDX-License-Identifier: Apache-2.0
"""Repository hygiene: no identifiers from other projects, Apache-2.0 text, SPDX headers.

Covers CR-004 (all code is new) and CR-005 (Apache-2.0 license).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPDX = "# SPDX-License-Identifier: Apache-2.0"

# Lowercase identifiers that must not appear in repo text. Each is split across
# adjacent literals so this file does not contain it. The bytecode cache still
# holds the joined string, so __pycache__ is pruned during the walk.
FORBIDDEN = ("distro" "box", "ana" "tase", "spaces" ".priv", "machine" "ctl", "ns" "pawn")

# Directory names pruned during the walk (VCS metadata and caches).
PRUNE_NAMES = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
# Repo-relative directories outside the scan. art/ is binary artwork, docs/reference
# holds clean-room notes, and .claude/ is untracked local tooling that is never shipped.
PRUNE_REL_DIRS = {"art", "docs/reference", ".claude"}
# Repo-relative files outside the scan. The roadmap copies the approved plan, which
# names the reference projects.
SKIP_REL_FILES = {"GINN-SRS-001-v1.0.md", "docs/roadmap.md"}


def _repo_text_files():
    """Yield (path, repo-relative posix path) for every file in scope."""
    for dirpath, dirnames, filenames in os.walk(REPO):
        rel_dir = Path(dirpath).relative_to(REPO)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in PRUNE_NAMES and (rel_dir / d).as_posix() not in PRUNE_REL_DIRS
        ]
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            if rel not in SKIP_REL_FILES:
                yield REPO / rel, rel


@pytest.mark.fr("CR-004")
def test_no_forbidden_identifiers_in_repo_text() -> None:
    hits: list[str] = []
    for path, rel in _repo_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary file
        hits.extend(f"{rel}: {term}" for term in FORBIDDEN if term in text)
    assert not hits, "forbidden identifiers found:\n" + "\n".join(hits)


@pytest.mark.fr("CR-004")
def test_license_file_starts_with_apache_header() -> None:
    lines = (REPO / "LICENSE").read_text(encoding="utf-8").lstrip().splitlines()
    assert lines[0] == "Apache License"
    assert lines[1].strip() == "Version 2.0, January 2004"


@pytest.mark.fr("CR-004")
def test_python_sources_start_with_spdx_header() -> None:
    missing: list[str] = []
    for top in ("src", "tests"):
        base = REPO / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            if not lines or lines[0].strip() != SPDX:
                missing.append(path.relative_to(REPO).as_posix())
    assert not missing, "missing SPDX header: " + ", ".join(missing)
