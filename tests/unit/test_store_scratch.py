# SPDX-License-Identifier: Apache-2.0
"""Scratch directories of a services-on create and their sweep (F3, F6, FR-122).

A create killed with SIGKILL leaves its scratch directory under the runtime root. The sweep removes
what the user owns under that prefix, and it never follows a link (store/scratch.py).
"""

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from ginn.store.paths import OwnedPaths
from ginn.store.scratch import (
    SCRATCH_PREFIX,
    ScratchSweep,
    leftover_names,
    remove_leftovers,
    scratch_dir,
)

pytestmark = [pytest.mark.fr("FR-080"), pytest.mark.fr("FR-122")]


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    yield resolved


def _leftover(root: Path, name: str) -> Path:
    directory = root / f"{SCRATCH_PREFIX}{name}"
    directory.mkdir()
    return directory


def test_a_scratch_directory_is_private_under_the_runtime_root_and_removed_on_exit(
    paths: OwnedPaths,
) -> None:
    with scratch_dir(paths) as scratch:
        assert scratch.parent == paths.runtime
        assert scratch.name.startswith(SCRATCH_PREFIX)
        assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
        (scratch / "init").write_bytes(b"x")
        held = scratch
    assert not held.exists()


def test_a_scratch_directory_is_removed_when_its_block_raises(paths: OwnedPaths) -> None:
    with pytest.raises(RuntimeError), scratch_dir(paths) as scratch:
        held = scratch
        raise RuntimeError("the create fails inside the block")
    assert not held.exists()


def test_a_sweep_removes_an_emptied_scratch_directory_and_leaves_every_other_entry(
    paths: OwnedPaths,
) -> None:
    leftover = _leftover(paths.runtime, "killed")
    (leftover / "init").write_bytes(b"x")
    (leftover / "machine-id").write_text("0" * 32, encoding="ascii")
    unrelated = paths.runtime / "keep"
    unrelated.mkdir()
    (unrelated / "f").write_text("y", encoding="utf-8")
    loose = paths.runtime / f"{SCRATCH_PREFIX}loose"
    loose.write_text("file", encoding="utf-8")
    result = remove_leftovers(paths.runtime, os.getuid())
    assert result.removed == (leftover.name,)
    assert [name for name, _reason in result.kept] == [loose.name]
    assert not leftover.exists()
    assert (unrelated / "f").read_text(encoding="utf-8") == "y"
    assert loose.read_text(encoding="utf-8") == "file"


def test_a_link_with_the_prefix_is_kept_and_its_target_is_never_followed(
    paths: OwnedPaths, tmp_path: Path
) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    (target / "precious").write_text("keep", encoding="utf-8")
    link = paths.runtime / f"{SCRATCH_PREFIX}link"
    link.symlink_to(target, target_is_directory=True)
    result = remove_leftovers(paths.runtime, os.getuid())
    assert result.removed == ()
    assert [name for name, _reason in result.kept] == [link.name]
    assert link.is_symlink()
    assert (target / "precious").read_text(encoding="utf-8") == "keep"


def test_a_link_inside_a_scratch_directory_is_unlinked_and_its_target_survives(
    paths: OwnedPaths, tmp_path: Path
) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    (target / "precious").write_text("keep", encoding="utf-8")
    leftover = _leftover(paths.runtime, "inner")
    (leftover / "init").symlink_to(target, target_is_directory=True)
    result = remove_leftovers(paths.runtime, os.getuid())
    assert result.removed == (leftover.name,)
    assert not leftover.exists()
    assert (target / "precious").read_text(encoding="utf-8") == "keep"


def test_a_scratch_directory_that_holds_a_directory_is_kept_with_its_contents(
    paths: OwnedPaths,
) -> None:
    leftover = _leftover(paths.runtime, "nested")
    (leftover / "sub").mkdir()
    (leftover / "sub" / "f").write_text("x", encoding="utf-8")
    result = remove_leftovers(paths.runtime, os.getuid())
    assert result.removed == ()
    assert [name for name, _reason in result.kept] == [leftover.name]
    assert (leftover / "sub" / "f").read_text(encoding="utf-8") == "x"


def test_a_scratch_directory_of_another_owner_is_kept(paths: OwnedPaths) -> None:
    leftover = _leftover(paths.runtime, "theirs")
    result = remove_leftovers(paths.runtime, os.getuid() + 1)
    assert result.removed == ()
    assert leftover.is_dir()


def test_a_missing_runtime_root_is_an_empty_sweep(tmp_path: Path) -> None:
    assert remove_leftovers(tmp_path / "absent", os.getuid()) == ScratchSweep((), ())


def test_leftover_names_lists_the_prefixed_names_and_never_follows_a_link(
    paths: OwnedPaths, tmp_path: Path
) -> None:
    (paths.runtime / f"{SCRATCH_PREFIX}a").mkdir()
    (paths.runtime / "other").mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (paths.runtime / f"{SCRATCH_PREFIX}link").symlink_to(target, target_is_directory=True)
    assert leftover_names(paths.runtime) == [f"{SCRATCH_PREFIX}a", f"{SCRATCH_PREFIX}link"]
    assert leftover_names(tmp_path / "absent") == []
