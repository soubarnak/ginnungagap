# SPDX-License-Identifier: Apache-2.0
"""Subordinate ID ranges: parsing, allocation above existing ranges, idempotent append.

Covers FR-001 (ranges written for the user), FR-002 (second run changes nothing) and AS-005
(at least 524,288 IDs per user). Each test runs under tmp_path, never on the real /etc or
/run/lock: every call passes `root_dir=tmp_path`, so its lock lives in `<tmp_path>/run/lock`.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ginn.errors import GinnError
from ginn.hostsetup import subids
from ginn.hostsetup.subids import (
    DEFAULT_CEILING,
    MIN_SIZE,
    Change,
    SubIdEntry,
    SubIdExhausted,
    SubIdLocked,
    allocate,
    ensure_exact,
    ensure_range,
    ensure_single,
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


def _lock_dir(tmp_path: Path) -> Path:
    """Return <tmp>/run/lock, creating it as a host has it (the standing lock directory)."""
    lock_dir = tmp_path / "run" / "lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


def _subuid(tmp_path: Path, content: bytes | None = None) -> Path:
    """Return <tmp>/etc/subuid, creating etc/, run/lock and the file when `content` is given."""
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    _lock_dir(tmp_path)
    path = etc / "subuid"
    if content is not None:
        path.write_bytes(content)
    return path


def _lock_path(tmp_path: Path, path: Path) -> Path:
    """Return <tmp>/run/lock/ginn-<name>.lock for `path`, creating the lock directory."""
    return _lock_dir(tmp_path) / f"ginn-{path.name}.lock"


def _names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _inode(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_dev, st.st_ino


def _dead_pid() -> int:
    """Return the PID of a child that has exited and been reaped, so no process has it now."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    child.wait()
    return child.pid


def test_parse_entries_skips_comments_blanks_and_unknown_lines() -> None:
    assert parse_entries(SAMPLE.decode()) == [
        SubIdEntry("root", 100000, 65536),
        SubIdEntry("bob", 165536, 65536),
        SubIdEntry("alice", 100000, 65536),
    ]


def test_entry_render_parses_back_to_itself() -> None:
    entry = SubIdEntry("alice", 231072, MIN_SIZE)
    assert parse_entries(entry.render() + "\n") == [entry]


@pytest.mark.fr("FR-001")
def test_an_eleven_digit_subid_count_still_parses() -> None:
    # PID digits have their own pattern, so a subuid count is not limited to ten digits.
    assert parse_entries("alice:100000:10000000000\n") == [
        SubIdEntry("alice", 100000, 10_000_000_000)
    ]


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
        ensure_range(_subuid(tmp_path), "alice", size, root_dir=tmp_path)


@pytest.mark.parametrize("bad", ["", " alice", "al:ice", "al\nice"])
def test_user_names_that_would_corrupt_the_file_are_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid user name"):
        allocate([], bad)


def test_existing_small_range_is_kept_and_a_full_size_range_is_appended(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    change = ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
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
    first = ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    inode_after_first = os.stat(path).st_ino
    second = ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert second.changed is False
    assert second.before == second.after == first.after == path.read_bytes()
    assert os.stat(path).st_ino == inode_after_first


@pytest.mark.fr("FR-002")
def test_user_with_one_full_size_entry_is_left_alone(tmp_path: Path) -> None:
    content = b"alice:100000:524288\n"
    path = _subuid(tmp_path, content)
    assert ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path) == Change(
        before=content, after=content, changed=False
    )


@pytest.mark.fr("FR-002")
def test_several_entries_that_total_enough_are_left_alone(tmp_path: Path) -> None:
    content = b"alice:100000:262144\nalice:400000:262144\n"
    path = _subuid(tmp_path, content)
    change = ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert change.changed is False
    assert path.read_bytes() == content


def test_numeric_uid_entries_count_toward_the_total(tmp_path: Path) -> None:
    content = b"1000:100000:524288\n"
    path = _subuid(tmp_path, content)
    change = ensure_range(path, "alice", MIN_SIZE, uid=1000, root_dir=tmp_path)
    assert change.changed is False
    assert path.read_bytes() == content


def test_missing_file_is_created_with_mode_0644(tmp_path: Path) -> None:
    path = _subuid(tmp_path)
    change = ensure_range(path, "alice", root_dir=tmp_path)
    assert change.before == b""
    assert path.read_bytes() == b"alice:100000:524288\n"
    assert _mode(path) == 0o644


def test_existing_mode_is_kept(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    os.chmod(path, 0o644)
    ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert _mode(path) == 0o644


def test_missing_newline_at_end_of_file_is_added_before_the_new_line(tmp_path: Path) -> None:
    path = _subuid(tmp_path, b"root:100000:65536")
    change = ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert change.after == b"root:100000:65536\nalice:165536:524288\n"


def test_atomic_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert _names(path.parent) == ["subuid"]
    assert _names(_lock_path(tmp_path, path).parent) == []


def test_failed_replace_keeps_the_old_file_and_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _subuid(tmp_path, SAMPLE)

    def refuse(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="simulated rename failure"):
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    monkeypatch.undo()
    assert path.read_bytes() == SAMPLE
    assert _names(path.parent) == ["subuid"]
    assert _names(_lock_path(tmp_path, path).parent) == []


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


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("name", ["subuid", "subgid"])
def test_the_lock_for_each_file_sits_under_run_lock_of_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    etc = tmp_path / "etc"
    etc.mkdir()
    _lock_dir(tmp_path)
    path = etc / name
    path.write_bytes(SAMPLE)
    locks_while_held: list[list[str]] = []
    etc_while_held: list[list[str]] = []
    real_atomic_write = subids.atomic_write

    def probe(target: Path, data: bytes, mode: int) -> None:
        locks_while_held.append(_names(_lock_path(tmp_path, path).parent))
        etc_while_held.append(_names(etc))
        real_atomic_write(target, data, mode)

    monkeypatch.setattr(subids, "atomic_write", probe)
    ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert locks_while_held == [[f"ginn-{name}.lock"]]
    assert etc_while_held == [[name]]
    assert _names(tmp_path / "run" / "lock") == []
    assert _names(etc) == [name]


@pytest.mark.fr("FR-001")
def test_a_missing_lock_directory_is_created_with_mode_0755(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    (tmp_path / "run" / "lock").rmdir()
    ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert _mode(tmp_path / "run" / "lock") == 0o755
    assert _names(tmp_path / "run" / "lock") == []


@pytest.mark.fr("FR-001")
def test_the_lock_directory_parent_is_never_created(tmp_path: Path) -> None:
    # Only run/lock may be created. A missing /run is not made here, and the lock never falls back
    # to a path under etc.
    path = _subuid(tmp_path, SAMPLE)
    (tmp_path / "run" / "lock").rmdir()
    (tmp_path / "run").rmdir()
    with pytest.raises(FileNotFoundError):
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert not (tmp_path / "run").exists()
    assert path.read_bytes() == SAMPLE
    assert _names(path.parent) == ["subuid"]


@pytest.mark.fr("FR-001")
def test_the_lock_never_exists_without_its_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the link can make the lock visible. Until then the lock must be absent, and the
    # temporary file must already hold the PID. After the link the lock holds the PID.
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    real_link = os.link
    pid_line = f"{os.getpid()}\n".encode("ascii")
    checks: list[str] = []

    def checked_link(src: object, dst: object, *args: object, **kwargs: object) -> None:
        if Path(str(dst)) == lock:
            assert not lock.exists()
            assert Path(str(src)).read_bytes() == pid_line
            checks.append("before")
        real_link(src, dst, *args, **kwargs)
        if Path(str(dst)) == lock:
            assert lock.read_bytes() == pid_line
            checks.append("after")

    monkeypatch.setattr(os, "link", checked_link)
    ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert checks == ["before", "after"]


@pytest.mark.fr("FR-001")
def test_a_creator_that_finds_the_lock_taken_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    lock.write_text(f"{os.getpid()}\n")
    assert subids._create_lock(lock) is None
    assert _names(lock.parent) == [lock.name]
    assert lock.read_text() == f"{os.getpid()}\n"


@pytest.mark.fr("FR-001")
def test_a_link_failure_other_than_eexist_propagates_and_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)

    def refuse(src: object, dst: object, *args: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "simulated: link not permitted")

    monkeypatch.setattr(os, "link", refuse)
    with pytest.raises(OSError) as failed:
        subids._create_lock(lock)
    assert failed.value.errno == errno.EPERM
    assert _names(lock.parent) == []


@pytest.mark.fr("FR-001")
def test_lock_is_held_during_the_edit_and_removed_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    real_atomic_write = subids.atomic_write
    probed: list[str] = []

    def probe(target: Path, data: bytes, mode: int) -> None:
        # Another caller must not get in while this one replaces the file, and the lock names us.
        assert lock.read_text() == f"{os.getpid()}\n"
        with pytest.raises(SubIdLocked):
            ensure_range(target, "bob", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.0)
        probed.append("refused")
        real_atomic_write(target, data, mode)

    monkeypatch.setattr(subids, "atomic_write", probe)
    ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert probed == ["refused"]
    assert _names(path.parent) == ["subuid"]
    assert _names(lock.parent) == []


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"garbage\n", id="garbage"),
        pytest.param(b"12345678901\n", id="eleven-digits"),
        pytest.param(b"0\n", id="zero"),
        pytest.param(b"\xff\xfe\n", id="not-ascii"),
    ],
)
def test_an_empty_or_unparseable_lock_is_held_never_reclaimed_and_refused_after_the_timeout(
    tmp_path: Path, content: bytes
) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    lock.write_bytes(content)
    started = time.monotonic()
    with pytest.raises(SubIdLocked, match="held by another process") as refused:
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.1)
    assert time.monotonic() - started >= 0.1
    assert refused.value.code == "GINN-E-SUBID-LOCKED"
    assert refused.value.exit_code == 75
    assert "(no readable pid)" in str(refused.value)
    assert "ginn host-setup" in refused.value.remedy
    assert "by hand" in refused.value.remedy
    assert lock.read_bytes() == content
    assert path.read_bytes() == SAMPLE
    assert _names(lock.parent) == [lock.name]


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("holder", [os.getpid(), 1])
def test_a_lock_held_by_a_live_pid_times_out_and_names_that_pid(
    tmp_path: Path, holder: int
) -> None:
    # PID 1 belongs to another user for an ordinary test run, which is a PermissionError on
    # kill(0). That still means alive, so the lock is not reclaimed.
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    lock.write_text(f"{holder}\n")
    with pytest.raises(SubIdLocked, match=re.escape(f"(pid {holder})")):
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.1)
    assert lock.read_text() == f"{holder}\n"
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
def test_a_lock_left_by_a_dead_process_is_reclaimed_without_waiting(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    lock.write_text(f"{_dead_pid()}\n")
    change = ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.0)
    assert change.changed is True
    # The reclaim gate is left in place by design; it is an empty, harmless file.
    assert _names(lock.parent) == [f"{lock.name}.reclaim"]


@pytest.mark.fr("FR-001")
def test_a_caller_proceeds_once_the_other_holder_releases_the_lock(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    lock.write_text(f"{os.getpid()}\n")
    release = threading.Timer(0.2, lock.unlink)
    release.start()
    try:
        change = ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=10.0)
    finally:
        release.join()
    assert change.changed is True
    assert _names(path.parent) == ["subuid"]
    assert _names(lock.parent) == []


@pytest.mark.fr("FR-001")
def test_a_symbolic_link_at_the_lock_path_is_held_and_never_followed_or_removed(
    tmp_path: Path,
) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    target = tmp_path / "elsewhere"
    target_text = f"{_dead_pid()}\n"
    target.write_text(target_text)
    lock.symlink_to(target)
    with pytest.raises(SubIdLocked):
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.1)
    assert lock.is_symlink()
    assert target.read_text() == target_text
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
def test_the_reclaim_gate_is_polled_without_blocking_and_gives_up_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    dead_text = f"{_dead_pid()}\n"
    lock.write_text(dead_text)
    gate = lock.with_name(f"{lock.name}.reclaim")
    gate.write_bytes(b"")
    holder = os.open(gate, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    real_flock = fcntl.flock
    calls: list[int] = []

    def recording_flock(fd: int, op: int) -> None:
        calls.append(op)
        if not op & fcntl.LOCK_NB:
            # Refuse at once, so a blocking call fails the test instead of hanging it.
            raise AssertionError("the reclaim gate must be taken without blocking")
        real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", recording_flock)
    started = time.monotonic()
    try:
        with pytest.raises(SubIdLocked):
            ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.3)
    finally:
        os.close(holder)
    elapsed = time.monotonic() - started
    assert len(calls) >= 3, "the gate should be polled several times before the deadline"
    assert all(op & fcntl.LOCK_NB for op in calls)
    assert 0.3 <= elapsed < 5.0
    assert lock.read_text() == dead_text
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
def test_a_reclaim_gate_replaced_while_held_is_not_used_to_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another process unlinks the gate and puts a new file at its path after this call took the
    # old one, and a rival then holds the new file. Reclaiming through the old descriptor would
    # bypass the rival. The call must reopen the path, wait on the new file, and give up at the
    # deadline without reclaiming.
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    dead_text = f"{_dead_pid()}\n"
    lock.write_text(dead_text)
    gate = lock.with_name(f"{lock.name}.reclaim")
    gate.write_bytes(b"")
    real_take = subids._take_gate
    real_locked = subids._reclaim_locked
    rivals: list[int] = []
    takes: list[int] = []
    reclaimed: list[Path] = []

    def take_then_replace(fd: int, deadline: float) -> bool:
        taken = real_take(fd, deadline)
        takes.append(fd)
        if taken and not rivals:
            gate.unlink()
            gate.write_bytes(b"")
            rival = os.open(gate, os.O_RDONLY)
            fcntl.flock(rival, fcntl.LOCK_EX)
            rivals.append(rival)
        return taken

    def recording_locked(target: Path, seen: subids._LockIdentity) -> subids._Inode | None:
        reclaimed.append(target)
        return real_locked(target, seen)

    monkeypatch.setattr(subids, "_take_gate", take_then_replace)
    monkeypatch.setattr(subids, "_reclaim_locked", recording_locked)
    try:
        with pytest.raises(SubIdLocked):
            ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.3)
    finally:
        for rival in rivals:
            os.close(rival)
    assert len(takes) >= 2, "the call must open the path again after the replacement"
    assert reclaimed == []
    assert lock.read_text() == dead_text
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("successor", ["live", "dead-pid"])
def test_a_waiter_that_judged_a_lock_stale_does_not_remove_its_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, successor: str
) -> None:
    # Two waiters judge the same crashed lock stale. The first reclaims it and a new holder takes
    # the path. The second reaches the reclaim gate only after that, and must leave the new lock.
    # "dead-pid" writes the same bytes as the crashed lock, so only the inode and mtime differ.
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    dead = _dead_pid()
    lock.write_text(f"{dead}\n")
    fresh = f"{os.getpid()}\n" if successor == "live" else f"{dead}\n"
    real_reclaim = subids._reclaim_stale

    def interleaved_reclaim(
        lock_path: Path, seen: subids._LockIdentity, deadline: float
    ) -> subids._Inode | None:
        staged = lock_path.with_name(f"{lock_path.name}.first")
        staged.write_text(fresh)
        os.replace(staged, lock_path)
        return real_reclaim(lock_path, seen, deadline)

    monkeypatch.setattr(subids, "_reclaim_stale", interleaved_reclaim)
    with pytest.raises(SubIdLocked):
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.1)
    assert lock.read_text() == fresh
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
def test_a_stale_lock_rewritten_in_place_before_the_reclaim_is_not_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lock keeps its inode and mtime, and only its PID changes. The identity compare sees the
    # PID, so the reclaim leaves the lock alone.
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    lock.write_text(f"{_dead_pid()}\n")
    judged_mtime = os.stat(lock).st_mtime_ns
    real_reclaim = subids._reclaim_stale

    def live_pid_written_in_place(
        lock_path: Path, seen: subids._LockIdentity, deadline: float
    ) -> subids._Inode | None:
        lock_path.write_text(f"{os.getpid()}\n")
        os.utime(lock_path, ns=(judged_mtime, judged_mtime))
        return real_reclaim(lock_path, seen, deadline)

    monkeypatch.setattr(subids, "_reclaim_stale", live_pid_written_in_place)
    with pytest.raises(SubIdLocked):
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.1)
    assert lock.read_text() == f"{os.getpid()}\n"
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
def test_unlink_if_same_removes_only_the_inode_it_was_given(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    lock.write_text("1\n")
    judged = _inode(lock)
    replacement = lock.with_name(f"{lock.name}.other")
    replacement.write_text("2\n")
    os.replace(replacement, lock)
    subids._unlink_if_same(lock, judged)
    assert lock.read_text() == "2\n"
    subids._unlink_if_same(lock, _inode(lock))
    assert not lock.exists()
    subids._unlink_if_same(lock, judged)  # already gone: no error


@pytest.mark.fr("FR-001")
def test_release_does_not_unlink_a_lock_that_another_process_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    real_atomic_write = subids.atomic_write

    def replaced_by_another_process(target: Path, data: bytes, mode: int) -> None:
        staged = lock.with_name(f"{lock.name}.other")
        staged.write_bytes(b"another holder\n")
        os.replace(staged, lock)
        real_atomic_write(target, data, mode)

    monkeypatch.setattr(subids, "atomic_write", replaced_by_another_process)
    ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path)
    assert lock.read_bytes() == b"another holder\n"
    assert _names(lock.parent) == [lock.name]


ROUNDS = 10


@pytest.mark.fr("FR-001")
def test_two_reclaimers_of_one_stale_lock_run_the_critical_section_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both waiters judged the same crashed lock stale. Each takes the reclaim gate before it
    # re-reads, so the second one must see the first one's fresh lock and back off.
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    lock.write_text(f"{_dead_pid()}\n")
    judged = subids._read_lock(lock)
    assert judged is not None
    real_locked = subids._reclaim_locked
    guard = threading.Lock()
    inside = 0
    peak = 0

    def traced(target: Path, seen: subids._LockIdentity) -> subids._Inode | None:
        nonlocal inside, peak
        with guard:
            inside += 1
            peak = max(peak, inside)
        # A pause long enough for an unserialised second reclaimer to enter the section.
        time.sleep(0.05)
        try:
            return real_locked(target, seen)
        finally:
            with guard:
                inside -= 1

    monkeypatch.setattr(subids, "_reclaim_locked", traced)
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []

    def reclaim() -> None:
        barrier.wait()
        outcomes.append(subids._reclaim_stale(lock, judged, time.monotonic() + 10) is not None)

    threads = [threading.Thread(target=reclaim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert peak == 1
    assert sorted(outcomes) == [False, True]
    assert lock.read_text() == f"{os.getpid()}\n"
    assert _mode(lock.with_name(f"{lock.name}.reclaim")) == 0o600


@pytest.mark.fr("FR-001")
def test_threads_entering_the_lock_over_a_stale_lock_never_overlap(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    dead = _dead_pid()
    guard = threading.Lock()
    occupancy = {"now": 0, "peak": 0}
    failures: list[BaseException] = []

    def contend(barrier: threading.Barrier) -> None:
        try:
            barrier.wait()
            with subids._lock(path, tmp_path, 10.0):
                with guard:
                    occupancy["now"] += 1
                    occupancy["peak"] = max(occupancy["peak"], occupancy["now"])
                time.sleep(0.01)
                with guard:
                    occupancy["now"] -= 1
        except BaseException as exc:
            failures.append(exc)

    for _ in range(ROUNDS):
        lock.write_text(f"{dead}\n")
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=contend, args=(barrier,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

    assert failures == []
    assert occupancy["peak"] == 1
    assert not lock.exists()
    # The gate stays by design (see _reclaim_stale). No lock and no temporary file remain.
    assert _names(lock.parent) == [f"{lock.name}.reclaim"]


@pytest.mark.fr("FR-001")
def test_a_symbolic_link_in_place_of_the_reclaim_gate_is_refused(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    lock.write_text(f"{_dead_pid()}\n")
    elsewhere = tmp_path / "elsewhere"
    gate = lock.with_name(f"{lock.name}.reclaim")
    gate.symlink_to(elsewhere)
    with pytest.raises(GinnError) as refused:
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.1)
    assert refused.value.code == "GINN-E-SUBID-LOCK-UNSAFE"
    assert refused.value.exit_code == 77
    assert refused.value.details == {"lock": str(lock), "path": str(gate)}
    cause = refused.value.__cause__
    assert isinstance(cause, OSError)
    assert cause.errno == errno.ELOOP
    assert not elsewhere.exists()
    assert lock.exists()
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("failing", ["write", "fsync"])
def test_a_failed_pid_write_or_fsync_leaves_neither_lock_nor_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    # A failed write or fsync must not publish a lock, and the temporary file must be removed.
    # Otherwise an empty or half-written lock would block every later Host Setup.
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(tmp_path, path)
    real_write = os.write
    pid_line = f"{os.getpid()}\n".encode("ascii")

    def refuse_pid_write(fd: int, data: bytes | memoryview) -> int:
        if bytes(data) == pid_line:
            raise OSError(errno.ENOSPC, "simulated: no space left on device")
        return real_write(fd, data)

    def refuse_fsync(fd: int) -> None:
        raise OSError(errno.EIO, "simulated: input/output error")

    if failing == "write":
        monkeypatch.setattr(os, "write", refuse_pid_write)
        expected = errno.ENOSPC
    else:
        monkeypatch.setattr(os, "fsync", refuse_fsync)
        expected = errno.EIO
    with pytest.raises(OSError) as failed:
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.0)
    assert failed.value.errno == expected
    monkeypatch.undo()
    assert _names(lock.parent) == []
    assert path.read_bytes() == SAMPLE
    change = ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.0)
    assert change.changed is True
    assert _names(path.parent) == ["subuid"]
    assert _names(lock.parent) == []


@pytest.mark.fr("FR-001")
def test_the_lock_is_removed_when_the_edit_fails(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    with pytest.raises(SubIdExhausted):
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, ceiling=200000)
    assert _names(path.parent) == ["subuid"]
    assert _names(_lock_path(tmp_path, path).parent) == []


@pytest.mark.fr("FR-001")
def test_subgid_has_its_own_lock_file(tmp_path: Path) -> None:
    etc = tmp_path / "etc"
    etc.mkdir()
    path = etc / "subgid"
    path.write_bytes(SAMPLE)
    subgid_lock = _lock_path(tmp_path, path)
    subgid_lock.write_text(f"{os.getpid()}\n")
    with pytest.raises(SubIdLocked):
        ensure_range(path, "alice", MIN_SIZE, root_dir=tmp_path, lock_timeout=0.0)
    assert subgid_lock.exists()
    assert not (subgid_lock.parent / "ginn-subuid.lock").exists()
    assert _names(etc) == ["subgid"]


RECORDED = SubIdEntry("alice", 200000, MIN_SIZE)
"""The range Host Setup recorded for alice. It spans 200000 to 724288, end exclusive."""


@pytest.mark.fr("FR-001")
def test_ensure_single_on_a_fresh_file_writes_one_full_size_line(tmp_path: Path) -> None:
    path = _subuid(tmp_path)
    change, entry = ensure_single(path, "alice", root_dir=tmp_path)
    assert change.changed is True
    assert change.before == b""
    assert change.after == b"alice:100000:524288\n"
    assert entry == SubIdEntry("alice", 100000, MIN_SIZE)
    assert change.added == entry
    assert path.read_bytes() == change.after
    assert _mode(path) == 0o644
    assert _names(_lock_path(tmp_path, path).parent) == []


@pytest.mark.fr("FR-002")
def test_ensure_single_adopts_an_existing_single_line_of_full_size(tmp_path: Path) -> None:
    content = b"alice:300000:524288\n"
    path = _subuid(tmp_path, content)
    change, entry = ensure_single(path, "alice", root_dir=tmp_path)
    assert change == Change(before=content, after=content, changed=False)
    assert entry == SubIdEntry("alice", 300000, MIN_SIZE)
    assert path.read_bytes() == content


@pytest.mark.fr("FR-001")
def test_ensure_single_adds_a_full_size_line_because_two_halves_do_not_count(
    tmp_path: Path,
) -> None:
    # ensure_range would call these two halves enough (524288 in total). ensure_single does not.
    content = b"alice:100000:262144\nalice:400000:262144\n"
    path = _subuid(tmp_path, content)
    change, entry = ensure_single(path, "alice", root_dir=tmp_path)
    assert change.changed is True
    assert entry == SubIdEntry("alice", 662144, MIN_SIZE)
    assert change.added == entry
    assert change.after == content + b"alice:662144:524288\n"
    assert path.read_bytes() == change.after
    for old in parse_entries(content.decode()):
        assert entry.start >= old.end


@pytest.mark.fr("FR-001")
def test_ensure_single_docstring_states_that_lines_are_not_summed() -> None:
    doc = ensure_single.__doc__ or ""
    assert "Unlike `ensure_range`" in doc
    assert "sums the counts" in doc
    assert "two half-size lines do not satisfy it" in doc


@pytest.mark.fr("FR-002")
def test_ensure_exact_leaves_a_file_that_already_holds_the_recorded_line(tmp_path: Path) -> None:
    content = b"root:100000:65536\nalice:200000:524288\n"
    path = _subuid(tmp_path, content)
    inode = _inode(path)
    change = ensure_exact(path, RECORDED, root_dir=tmp_path)
    assert change == Change(before=content, after=content, changed=False)
    assert _inode(path) == inode
    assert _names(_lock_path(tmp_path, path).parent) == []


@pytest.mark.fr("FR-001")
def test_ensure_exact_appends_the_recorded_line_when_it_is_missing(tmp_path: Path) -> None:
    content = b"carol:1000000:524288\n"
    path = _subuid(tmp_path, content)
    change = ensure_exact(path, RECORDED, root_dir=tmp_path)
    assert change.changed is True
    assert change.added == RECORDED
    assert change.after == content + b"alice:200000:524288\n"
    assert path.read_bytes() == change.after
    assert _names(path.parent) == ["subuid"]
    assert _names(_lock_path(tmp_path, path).parent) == []


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"alice:100000:1048576\n", id="admin-widened-both-ways"),
        pytest.param(b"alice:200000:524289\n", id="one-id-more-at-the-end"),
        pytest.param(b"alice:199999:524289\n", id="one-id-more-below-the-start"),
    ],
)
def test_ensure_exact_accepts_a_same_name_line_that_covers_the_recorded_range(
    tmp_path: Path, line: bytes
) -> None:
    path = _subuid(tmp_path, line)
    change = ensure_exact(path, RECORDED, root_dir=tmp_path)
    assert change == Change(before=line, after=line, changed=False)
    assert path.read_bytes() == line
    assert _names(_lock_path(tmp_path, path).parent) == []


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"alice:150000:524288\n", id="same-name-partial-overlap"),
        pytest.param(b"dave:150000:65536\n", id="another-user-overlap"),
        pytest.param(
            b"alice:100000:1048576\ndave:300000:1000\n",
            id="another-user-overlap-beside-a-covering-line",
        ),
    ],
)
def test_ensure_exact_refuses_an_overlap_that_is_not_a_covering_same_name_line(
    tmp_path: Path, content: bytes
) -> None:
    path = _subuid(tmp_path, content)
    with pytest.raises(GinnError) as refused:
        ensure_exact(path, RECORDED, root_dir=tmp_path)
    assert refused.value.code == "GINN-E-SUBID-CONFLICT"
    assert refused.value.exit_code == 4
    assert refused.value.details == {
        "start": RECORDED.start,
        "count": RECORDED.count,
        "user": "alice",
        "path": str(path),
    }
    assert path.read_bytes() == content
    assert _names(_lock_path(tmp_path, path).parent) == []
