# SPDX-License-Identifier: Apache-2.0
"""Subordinate ID ranges: parsing, allocation above existing ranges, idempotent append.

Covers FR-001 (ranges written for the user), FR-002 (second run changes nothing) and AS-005
(at least 524,288 IDs per user). Each test runs under tmp_path, never on the real /etc.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ginn.hostsetup import subids
from ginn.hostsetup.subids import (
    DEFAULT_CEILING,
    MIN_SIZE,
    Change,
    SubIdEntry,
    SubIdExhausted,
    SubIdLocked,
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


def _lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def _age(path: Path, seconds: float) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


def _dead_pid() -> int:
    """Return the PID of a child that has exited and been reaped, so no process has it now."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    child.wait()
    return child.pid


@pytest.mark.fr("FR-001")
def test_lock_is_held_during_the_edit_and_removed_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _subuid(tmp_path, SAMPLE)
    real_atomic_write = subids.atomic_write
    probed: list[str] = []

    def probe(target: Path, data: bytes, mode: int) -> None:
        # Another caller must not get in while this one replaces the file, and the lock names us.
        assert _lock_path(target).read_text() == f"{os.getpid()}\n"
        with pytest.raises(SubIdLocked):
            ensure_range(target, "bob", MIN_SIZE, lock_timeout=0.0)
        probed.append("refused")
        real_atomic_write(target, data, mode)

    monkeypatch.setattr(subids, "atomic_write", probe)
    ensure_range(path, "alice", MIN_SIZE)
    assert probed == ["refused"]
    assert _names(path.parent) == ["subuid"]


@pytest.mark.fr("FR-001")
def test_a_lock_held_by_someone_else_is_waited_for_then_refused_and_left_in_place(
    tmp_path: Path,
) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(path)
    lock.write_text("held by another Host Setup\n")
    with pytest.raises(SubIdLocked, match="held by another process"):
        ensure_range(path, "alice", MIN_SIZE, lock_timeout=0.1)
    assert lock.read_text() == "held by another Host Setup\n"
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
def test_a_caller_proceeds_once_the_other_holder_releases_the_lock(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(path)
    lock.write_text("")
    release = threading.Timer(0.2, lock.unlink)
    release.start()
    try:
        change = ensure_range(path, "alice", MIN_SIZE, lock_timeout=10.0)
    finally:
        release.join()
    assert change.changed is True
    assert _names(path.parent) == ["subuid"]


@pytest.mark.fr("FR-001")
def test_a_lock_left_by_a_dead_process_is_reclaimed_without_waiting(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(path)
    lock.write_text(f"{_dead_pid()}\n")
    _age(lock, 60)
    change = ensure_range(path, "alice", MIN_SIZE, lock_timeout=0.0)
    assert change.changed is True
    # The reclaim gate is left in place by design; it is an empty, harmless file.
    assert _names(path.parent) == ["subuid", "subuid.lock.reclaim"]


@pytest.mark.fr("FR-001")
def test_a_lock_of_a_dead_process_is_kept_while_it_is_still_fresh(tmp_path: Path) -> None:
    # A creator writes its PID a moment after it creates the file, so a fresh lock is not reclaimed.
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(path)
    lock.write_text(f"{_dead_pid()}\n")
    with pytest.raises(SubIdLocked):
        ensure_range(path, "alice", MIN_SIZE, lock_timeout=0.1)
    assert lock.exists()
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
def test_a_lock_held_by_a_live_pid_times_out_and_names_that_pid(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(path)
    lock.write_text(f"{os.getpid()}\n")
    _age(lock, 60)
    with pytest.raises(SubIdLocked, match=re.escape(f"(pid {os.getpid()})")):
        ensure_range(path, "alice", MIN_SIZE, lock_timeout=0.1)
    assert lock.read_text() == f"{os.getpid()}\n"
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
def test_a_lock_naming_more_than_ten_digits_is_not_a_pid_and_is_not_reclaimed(
    tmp_path: Path,
) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(path)
    lock.write_text("12345678901\n")
    _age(lock, 60)
    with pytest.raises(SubIdLocked) as refused:
        ensure_range(path, "alice", MIN_SIZE, lock_timeout=0.1)
    assert "(pid" not in str(refused.value)
    assert lock.read_text() == "12345678901\n"


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("successor", ["live", "dead-pid"])
def test_a_waiter_that_judged_a_lock_stale_does_not_remove_its_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, successor: str
) -> None:
    # Two waiters judge the same crashed lock stale. The first reclaims it and a new holder takes
    # the path. The second reaches the reclaim gate only after that, and must leave the new lock.
    # "dead-pid" writes the same bytes as the crashed lock, so only the inode and mtime differ.
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(path)
    dead = _dead_pid()
    lock.write_text(f"{dead}\n")
    _age(lock, 60)
    fresh = f"{os.getpid()}\n" if successor == "live" else f"{dead}\n"
    real_reclaim = subids._reclaim_stale

    def interleaved_reclaim(target: Path, seen: subids._LockIdentity) -> bool:
        staged = lock.with_name(f"{lock.name}.first")
        staged.write_text(fresh)
        os.replace(staged, lock)
        return real_reclaim(target, seen)

    monkeypatch.setattr(subids, "_reclaim_stale", interleaved_reclaim)
    with pytest.raises(SubIdLocked):
        ensure_range(path, "alice", MIN_SIZE, lock_timeout=0.1)
    assert lock.read_text() == fresh
    assert path.read_bytes() == SAMPLE


ROUNDS = 10


@pytest.mark.fr("FR-001")
def test_two_reclaimers_of_one_stale_lock_run_the_critical_section_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both waiters judged the same crashed lock stale. Each takes the reclaim gate before it
    # re-reads, so the second one must see the first one's fresh lock and back off.
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(path)
    lock.write_text(f"{_dead_pid()}\n")
    _age(lock, 60)
    judged = subids._read_lock(lock)
    assert judged is not None
    real_locked = subids._reclaim_locked
    guard = threading.Lock()
    inside = 0
    peak = 0

    def traced(target: Path, seen: subids._LockIdentity) -> bool:
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
        outcomes.append(subids._reclaim_stale(lock, judged))

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
    lock = _lock_path(path)
    dead = _dead_pid()
    guard = threading.Lock()
    occupancy = {"now": 0, "peak": 0}
    failures: list[BaseException] = []

    def contend(barrier: threading.Barrier) -> None:
        try:
            barrier.wait()
            with subids._lock(path, 10.0):
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
        _age(lock, 60)
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=contend, args=(barrier,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

    assert failures == []
    assert occupancy["peak"] == 1
    assert not lock.exists()


@pytest.mark.fr("FR-001")
def test_a_symbolic_link_in_place_of_the_reclaim_gate_is_refused(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    lock = _lock_path(path)
    lock.write_text(f"{_dead_pid()}\n")
    _age(lock, 60)
    elsewhere = tmp_path / "elsewhere"
    gate = lock.with_name(f"{lock.name}.reclaim")
    gate.symlink_to(elsewhere)
    with pytest.raises(SubIdLocked, match="symbolic link") as refused:
        ensure_range(path, "alice", MIN_SIZE, lock_timeout=0.1)
    assert str(gate) in str(refused.value)
    cause = refused.value.__cause__
    assert isinstance(cause, OSError)
    assert cause.errno == errno.ELOOP
    assert not elsewhere.exists()
    assert lock.exists()
    assert path.read_bytes() == SAMPLE


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("failing", ["write", "fsync"])
def test_a_failed_pid_write_or_fsync_removes_the_lock_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    # A lock with no PID is never stale, so one left behind would block every later Host Setup.
    path = _subuid(tmp_path, SAMPLE)
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
        ensure_range(path, "alice", MIN_SIZE, lock_timeout=0.0)
    assert failed.value.errno == expected
    monkeypatch.undo()
    assert _names(path.parent) == ["subuid"]
    assert path.read_bytes() == SAMPLE
    change = ensure_range(path, "alice", MIN_SIZE, lock_timeout=0.0)
    assert change.changed is True
    assert _names(path.parent) == ["subuid"]


@pytest.mark.fr("FR-001")
def test_the_lock_is_removed_when_the_edit_fails(tmp_path: Path) -> None:
    path = _subuid(tmp_path, SAMPLE)
    with pytest.raises(SubIdExhausted):
        ensure_range(path, "alice", MIN_SIZE, ceiling=200000)
    assert _names(path.parent) == ["subuid"]


@pytest.mark.fr("FR-001")
def test_subgid_has_its_own_lock_file(tmp_path: Path) -> None:
    etc = tmp_path / "etc"
    etc.mkdir()
    path = etc / "subgid"
    path.write_bytes(SAMPLE)
    _lock_path(path).write_text("")
    with pytest.raises(SubIdLocked):
        ensure_range(path, "alice", MIN_SIZE, lock_timeout=0.0)
    assert _lock_path(path).exists()
    assert not (etc / "subuid.lock").exists()
