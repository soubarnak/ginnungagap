# SPDX-License-Identifier: Apache-2.0
"""Subordinate ID ranges: parsing, allocation above existing ranges, idempotent append.

Covers FR-001 (ranges written for the user), FR-002 (second run changes nothing) and AS-005
(at least 524,288 IDs per user). Each test runs under tmp_path, never on the real /etc.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ginn.hostsetup.subids import (
    DEFAULT_CEILING,
    MIN_SIZE,
    Change,
    SubIdEntry,
    SubIdExhausted,
    allocate,
    ensure_range,
    newuidmap_present,
    parse_entries,
)

pytestmark = [pytest.mark.fr("FR-001"), pytest.mark.fr("AS-005")]

SAMPLE = (
    b"# subordinate ids, managed by hand\n"
    b"\n"
    b"root:100000:65536\n"
    b"bob:165536:65536   # second user\n"
    b"not a range line\n"
    b"alice:100000:65536\n"
)


def _subuid(tmp_path: Path, content: bytes | None = None) -> Path:
    """Return <tmp>/etc/subuid, creating etc/ and the file when `content` is given."""
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    path = etc / "subuid"
    if content is not None:
        path.write_bytes(content)
    return path


def _names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_parse_entries_skips_comments_blanks_and_unknown_lines() -> None:
    assert parse_entries(SAMPLE.decode()) == [
        SubIdEntry("root", 100000, 65536),
        SubIdEntry("bob", 165536, 65536),
        SubIdEntry("alice", 100000, 65536),
    ]


def test_entry_render_parses_back_to_itself() -> None:
    entry = SubIdEntry("alice", 231072, MIN_SIZE)
    assert parse_entries(entry.render() + "\n") == [entry]


def test_allocate_starts_above_every_existing_range() -> None:
    existing = parse_entries(SAMPLE.decode())
    new = allocate(existing, "carol", MIN_SIZE)
    assert new == SubIdEntry("carol", 231072, MIN_SIZE)
    assert all(new.start >= old.end for old in existing)


def test_allocate_uses_the_floor_when_nothing_exists() -> None:
    assert allocate([], "alice") == SubIdEntry("alice", 100000, MIN_SIZE)


def test_allocate_refuses_when_no_room_is_left_below_the_ceiling() -> None:
    with pytest.raises(SubIdExhausted):
        allocate([SubIdEntry("x", 100000, 4000)], "alice", MIN_SIZE, ceiling=200000)


def test_default_ceiling_leaves_the_top_block_free() -> None:
    assert DEFAULT_CEILING == 2**32 - 1 - 65536


@pytest.mark.parametrize("size", [0, 1, MIN_SIZE - 1])
def test_sizes_below_the_as_005_minimum_are_refused(size: int, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="AS-005"):
        allocate([], "alice", size)
    with pytest.raises(ValueError, match="AS-005"):
        ensure_range(_subuid(tmp_path), "alice", size)


@pytest.mark.parametrize("bad", ["", " alice", "al:ice", "al\nice"])
def test_user_names_that_would_corrupt_the_file_are_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid user name"):
        allocate([], bad)


def test_existing_small_range_is_kept_and_a_full_size_range_is_appended(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    change = ensure_range(path, "alice", MIN_SIZE)
    assert change.changed is True
    assert change.before == SAMPLE
    assert change.after.startswith(SAMPLE)
    assert change.after == path.read_bytes()
    assert change.added == SubIdEntry("alice", 231072, MIN_SIZE)
    alice = [e for e in parse_entries(change.after.decode()) if e.name == "alice"]
    assert alice == [SubIdEntry("alice", 100000, 65536), SubIdEntry("alice", 231072, MIN_SIZE)]


@pytest.mark.fr("FR-002")
def test_second_call_changes_nothing_and_does_not_rewrite_the_file(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    first = ensure_range(path, "alice", MIN_SIZE)
    inode_after_first = os.stat(path).st_ino
    second = ensure_range(path, "alice", MIN_SIZE)
    assert second.changed is False
    assert second.before == second.after == first.after == path.read_bytes()
    assert os.stat(path).st_ino == inode_after_first


@pytest.mark.fr("FR-002")
def test_user_with_one_full_size_entry_is_left_alone(tmp_path: Path) -> None:
    content = b"alice:100000:524288\n"
    path = _subuid(tmp_path, content)
    assert ensure_range(path, "alice", MIN_SIZE) == Change(
        before=content, after=content, changed=False
    )


@pytest.mark.fr("FR-002")
def test_several_entries_that_total_enough_are_left_alone(tmp_path: Path) -> None:
    content = b"alice:100000:262144\nalice:400000:262144\n"
    path = _subuid(tmp_path, content)
    change = ensure_range(path, "alice", MIN_SIZE)
    assert change.changed is False
    assert path.read_bytes() == content


def test_numeric_uid_entries_count_toward_the_total(tmp_path: Path) -> None:
    content = b"1000:100000:524288\n"
    path = _subuid(tmp_path, content)
    change = ensure_range(path, "alice", MIN_SIZE, uid=1000)
    assert change.changed is False
    assert path.read_bytes() == content


def test_missing_file_is_created_with_mode_0644(tmp_path: Path) -> None:
    path = _subuid(tmp_path)
    change = ensure_range(path, "alice")
    assert change.before == b""
    assert path.read_bytes() == b"alice:100000:524288\n"
    assert _mode(path) == 0o644


def test_existing_mode_is_kept(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    os.chmod(path, 0o644)
    ensure_range(path, "alice", MIN_SIZE)
    assert _mode(path) == 0o644


def test_missing_newline_at_end_of_file_is_added_before_the_new_line(tmp_path: Path) -> None:
    path = _subuid(tmp_path, b"root:100000:65536")
    change = ensure_range(path, "alice", MIN_SIZE)
    assert change.after == b"root:100000:65536\nalice:165536:524288\n"


def test_atomic_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    ensure_range(path, "alice", MIN_SIZE)
    assert _names(path.parent) == ["subuid"]


def test_failed_replace_keeps_the_old_file_and_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _subuid(tmp_path, SAMPLE)

    def refuse(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="simulated rename failure"):
        ensure_range(path, "alice", MIN_SIZE)
    monkeypatch.undo()
    assert path.read_bytes() == SAMPLE
    assert _names(path.parent) == ["subuid"]


@pytest.mark.fr("FR-007")
def test_newuidmap_present_needs_both_tools_to_be_setuid(tmp_path: Path) -> None:
    bin_dir = tmp_path / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    assert newuidmap_present(tmp_path) is False
    (bin_dir / "newuidmap").write_bytes(b"")
    os.chmod(bin_dir / "newuidmap", 0o4755)  # noqa: S103 - fixture mode bits, not a host file
    assert newuidmap_present(tmp_path) is False
    (bin_dir / "newgidmap").write_bytes(b"")
    os.chmod(bin_dir / "newgidmap", 0o755)  # noqa: S103 - fixture mode bits, not a host file
    assert newuidmap_present(tmp_path) is False
    os.chmod(bin_dir / "newgidmap", 0o4755)  # noqa: S103 - fixture mode bits, not a host file
    assert newuidmap_present(tmp_path) is True
