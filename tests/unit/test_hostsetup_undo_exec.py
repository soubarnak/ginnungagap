# SPDX-License-Identifier: Apache-2.0
"""Host Setup undo removes the relay service and its runtime files (FR-003, CR-006, ADR-014).

Setup does not list the relay service `_exec`, its sync lock, its socket or its lock directory,
because ginn installs them after setup. So undo finds them by their fixed places: `_exec`, the
dotted leftovers and the sync lock under the user's state root (`<home>/.local/state/ginn/sv`), and
`exec.sock` and `exec/` under the user's runtime tree.

Both trees belong to the user, so undo runs no sv at all, and every removal is anchored to a
descriptor opened with O_NOFOLLOW. The tests put links inside those trees that point at files
outside them, and check that undo removes each link as a link and leaves its target alone. A real
undo of a record that setup wrote runs here, under tmp_path, with the cgroup and wait seams of
test_hostsetup_undo.py. Any process that undo starts fails the test.
"""

from __future__ import annotations

import errno
import os
import resource
import stat
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ginn.hostsetup.undo import UNDONE, UndoResult, _remove_at, run_undo
from tests.unit.test_hostsetup_undo import (
    USERS,
    _cgroup_rmdir,
    _cgroup_write,
    _Crash,
    _crash_after,
    _host,
    _script_owner,
    _service,
    _setup,
    _stopped,
    _Time,
)

USER = "alice"
UID = USERS[USER].uid


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    """Keep fixture directories at 0755 whatever the umask, because setup checks the parents."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture(autouse=True)
def no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if undo starts a process. Undo runs no sv, as root or as anyone (ADR-014)."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"undo started a process: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def _relay_service(root: Path) -> Path:
    """The relay's service directory under the user's home, as runit_system SV_DIR lays it out."""
    return root / "home" / USER / ".local" / "state" / "ginn" / "sv" / "_exec"


def _supervision(root: Path) -> Path:
    return _relay_service(root).parent


def _runtime(root: Path) -> Path:
    return root / "run" / "ginn" / str(UID)


def _outside(tmp_path: Path) -> Path:
    """A directory outside every tree that undo may touch, which a link inside a tree points at."""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    return outside


def _make_relay_files(root: Path) -> None:
    """What ginn leaves after the relay is installed, a listener has run and a sync was made."""
    service = _relay_service(root)
    service.mkdir(parents=True)
    (service / "run").write_text("#!/bin/sh\n", encoding="utf-8")
    (service / "down").write_text("", encoding="utf-8")
    temp = _supervision(root) / "._exec.a1b2c3d4"
    temp.mkdir()
    (temp / "run").write_text("#!/bin/sh\n", encoding="utf-8")
    (_supervision(root) / ".exec-relay.lock").write_text("", encoding="utf-8")
    runtime = _runtime(root)
    runtime.mkdir(parents=True, exist_ok=True)
    os.mknod(runtime / "exec.sock", 0o600 | stat.S_IFSOCK)
    lock_dir = runtime / "exec"
    lock_dir.mkdir()
    (lock_dir / "lab.lock").write_text("", encoding="utf-8")


def _runsv_reader(root: Path) -> int:
    """A FIFO `supervise/ok` held open for reading, as runsv holds it while it runs (runsv(8))."""
    ok = _relay_service(root) / "supervise" / "ok"
    ok.parent.mkdir(parents=True)
    os.mkfifo(ok)
    return os.open(ok, os.O_RDONLY | os.O_NONBLOCK)


def _undo(
    root: Path,
    *,
    fault: Callable[[str], None] | None = None,
) -> UndoResult:
    """One undo of alice, as test_hostsetup_undo._undo runs it."""
    virtual = _Time()
    return run_undo(
        USER,
        root_dir=root,
        euid=0,
        lookup_user=USERS.__getitem__,
        fault=fault,
        wait_service=_stopped,
        rmdir_cgroup=_cgroup_rmdir,
        write_control=_cgroup_write,
        owner_of=_script_owner(root),
        lock_timeout=5.0,
        clock=virtual.clock,
        sleep=virtual.sleep,
    )


def _relay_left(result: UndoResult) -> list[str]:
    return [item.path for item in result.left if "exec" in item.path]


@pytest.mark.fr("FR-003")
@pytest.mark.fr("DR-010")
def test_undo_removes_the_relay_service_its_temporaries_its_lock_and_its_runtime_files(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, USER)
    _make_relay_files(root)

    result = _undo(root)

    assert result.status == UNDONE
    assert not _relay_service(root).exists()
    assert not (_supervision(root) / "._exec.a1b2c3d4").exists()
    assert not (_supervision(root) / ".exec-relay.lock").exists()
    assert not (_runtime(root) / "exec.sock").exists()
    assert not (_runtime(root) / "exec").exists()
    assert _relay_left(result) == []


@pytest.mark.fr("FR-003")
def test_undo_without_a_relay_service_runs_no_process_and_leaves_no_relay_path(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, USER)

    result = _undo(root)

    assert result.status == UNDONE
    assert not _relay_service(root).exists()


@pytest.mark.fr("FR-003")
def test_the_relay_step_runs_after_the_wait_when_the_user_runsvdir_has_exited(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, USER)
    _make_relay_files(root)
    steps: list[str] = []

    result = _undo(root, fault=steps.append)

    assert result.status == UNDONE
    assert steps.index("wait") < steps.index("exec") < steps.index("service")
    assert not _relay_service(root).exists()


@pytest.mark.fr("FR-003")
def test_a_runsv_that_still_reads_the_relay_service_keeps_it_and_undo_goes_on(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, USER)
    _make_relay_files(root)
    reader = _runsv_reader(root)
    try:
        result = _undo(root)
    finally:
        os.close(reader)

    assert result.status == UNDONE
    assert _relay_service(root).is_dir(), "a runsv still reads the service, so it is kept"
    assert any(item.path.endswith("/.local/state/ginn/sv/_exec") for item in result.left)
    assert not (_runtime(root) / "exec.sock").exists(), "the socket is removed regardless"


@pytest.mark.fr("CR-006")
@pytest.mark.fr("FR-003")
def test_a_control_link_in_the_relay_service_is_removed_and_its_target_is_untouched(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, USER)
    _make_relay_files(root)
    target = _outside(tmp_path) / "control-target"
    target.write_text("keep\n", encoding="utf-8")
    target.chmod(0o644)
    supervise = _relay_service(root) / "supervise"
    supervise.mkdir()
    (supervise / "control").symlink_to(target)

    result = _undo(root)

    assert result.status == UNDONE
    assert not _relay_service(root).exists()
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert _relay_left(result) == []


@pytest.mark.fr("CR-006")
@pytest.mark.fr("FR-003")
def test_a_supervise_link_to_an_outside_directory_is_removed_and_that_directory_is_untouched(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, USER)
    _make_relay_files(root)
    outside = _outside(tmp_path) / "elsewhere"
    outside.mkdir()
    (outside / "ok").write_text("keep\n", encoding="utf-8")
    (_relay_service(root) / "supervise").symlink_to(outside, target_is_directory=True)

    result = _undo(root)

    assert result.status == UNDONE
    assert not _relay_service(root).exists()
    assert sorted(path.name for path in outside.iterdir()) == ["ok"]
    assert (outside / "ok").read_text(encoding="utf-8") == "keep\n"
    assert _relay_left(result) == []


@pytest.mark.fr("CR-006")
@pytest.mark.fr("FR-003")
def test_a_relay_service_that_is_a_link_is_kept_and_reported_and_its_target_is_untouched(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, USER)
    target = _outside(tmp_path) / "service-target"
    target.mkdir()
    (target / "run").write_text("keep\n", encoding="utf-8")
    _relay_service(root).parent.mkdir(parents=True)
    _relay_service(root).symlink_to(target, target_is_directory=True)

    result = _undo(root)

    assert result.status == UNDONE
    assert _relay_service(root).is_symlink()
    assert (target / "run").read_text(encoding="utf-8") == "keep\n"
    assert any(item.path.endswith("/.local/state/ginn/sv/_exec") for item in result.left)


@pytest.mark.fr("CR-006")
@pytest.mark.fr("DR-010")
def test_a_socket_path_that_is_a_link_is_not_followed_and_its_target_is_untouched(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, USER)
    _make_relay_files(root)
    (_runtime(root) / "exec.sock").unlink()
    target = _outside(tmp_path) / "socket-target"
    target.write_text("keep\n", encoding="utf-8")
    (_runtime(root) / "exec.sock").symlink_to(target)

    result = _undo(root)

    assert result.status == UNDONE
    assert target.read_text(encoding="utf-8") == "keep\n", "the link is not followed"


@pytest.mark.fr("FR-003")
def test_a_crash_after_the_relay_step_leaves_a_rerun_that_finishes_the_removal(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, USER)
    _make_relay_files(root)

    with pytest.raises(_Crash):
        _undo(root, fault=_crash_after("exec"))
    assert not _relay_service(root).exists(), "the crash comes after the relay step"

    result = _undo(root)

    assert result.status == UNDONE
    assert not (_runtime(root) / "exec.sock").exists()
    assert not (_runtime(root) / "exec").exists()


DEEP_LEVELS = 2000
"""The depth of the deep tree. It is above the recursion limit, so a recursive walk would fail."""


def _build_deep_tree(base: Path, levels: int, outside: Path) -> None:
    """Make `base/d0/.../d<levels-1>` with a file and a link to `outside` at the bottom.

    Each level is opened from the one above it, so the path is never built as one string, and the
    descriptors held here stay at two.
    """
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for level in range(levels):
            os.mkdir(f"d{level}", 0o755, dir_fd=fd)
            child = os.open(
                f"d{level}",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = child
        leaf = os.open(
            "leaf", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=fd
        )
        os.close(leaf)
        os.symlink(str(outside), "link", dir_fd=fd)
    finally:
        os.close(fd)


@pytest.mark.fr("CR-006")
@pytest.mark.fr("FR-003")
def test_a_tree_two_thousand_levels_deep_is_removed_without_recursion(tmp_path: Path) -> None:
    """The walk holds one descriptor per level, so the test raises the soft descriptor limit."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = DEEP_LEVELS + 256
    if hard != resource.RLIM_INFINITY and hard < want:
        pytest.skip("the descriptor limit is below what a 2000-level walk needs")
    outside = tmp_path / "outside"
    outside.write_text("keep\n", encoding="utf-8")
    base = tmp_path / "deep"
    base.mkdir()
    open_before = len(os.listdir("/proc/self/fd"))
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, want), hard))
    try:
        _build_deep_tree(base, DEEP_LEVELS, outside)
        parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            _remove_at(parent, "deep")
        finally:
            os.close(parent)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    assert not os.path.lexists(base)
    assert outside.read_text(encoding="utf-8") == "keep\n", "the link at the bottom is not followed"
    assert len(os.listdir("/proc/self/fd")) == open_before, "every descriptor is closed"


@pytest.mark.parametrize("kind", ["link", "file"])
@pytest.mark.fr("FR-003")
@pytest.mark.fr("CR-006")
def test_an_sv_directory_that_is_a_link_or_a_file_is_left_and_undo_finishes(
    tmp_path: Path, kind: str
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, USER)
    state = root / "home" / USER / ".local" / "state" / "ginn"
    state.mkdir(parents=True, exist_ok=True)
    outside = _outside(tmp_path) / "sv-target"
    if kind == "link":
        outside.mkdir()
        (outside / "keep").write_text("keep\n", encoding="utf-8")
        (state / "sv").symlink_to(outside, target_is_directory=True)
    else:
        (state / "sv").write_text("keep\n", encoding="utf-8")

    result = _undo(root)

    assert result.status == UNDONE, "a component that cannot be walked must not stop undo"
    assert any(item.path.endswith("/.local/state/ginn/sv") for item in result.left)
    if kind == "link":
        assert (outside / "keep").read_text(encoding="utf-8") == "keep\n"
    else:
        assert (state / "sv").read_text(encoding="utf-8") == "keep\n"


@pytest.mark.fr("FR-003")
def test_a_child_that_vanishes_during_the_walk_is_skipped_and_the_rest_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "tree"
    (tree / "a").mkdir(parents=True)
    (tree / "a" / "victim").write_text("gone\n", encoding="utf-8")
    (tree / "b").write_text("removed too\n", encoding="utf-8")
    real_unlink = os.unlink

    def racing_unlink(path: str, *, dir_fd: int | None = None) -> None:
        if path == "victim":
            real_unlink(path, dir_fd=dir_fd)  # another process removes it first
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", racing_unlink)
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _remove_at(parent, "tree")
    finally:
        os.close(parent)

    assert not os.path.lexists(tree)


@pytest.mark.fr("FR-003")
def test_a_missing_top_level_entry_raises_for_the_caller_which_treats_it_as_absent(
    tmp_path: Path,
) -> None:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        with pytest.raises(FileNotFoundError):
            _remove_at(parent, "missing")
    finally:
        os.close(parent)
