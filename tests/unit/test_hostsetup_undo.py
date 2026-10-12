# SPDX-License-Identifier: Apache-2.0
"""Host Setup undo for one user under a root prefix (FR-003; ADR-013 Decisions 15, 17, 18 and 20).

Covers the single-user and two-user undo in both orders, the realm refusal, the root and lock
refusals, subordinate ID lines that the admin owned or widened, a changed boot, undo from a pending
record, a crash at each undo step, a service that outlives the wait, the /etc/ginn decision of step
6, and the CLI. The records that setup wrote are the only input. The cgroup rmdir and the controller
write are emulated, because a plain directory with files would not rmdir and cgroupfs does not take
pre-image bytes back. Each test runs under tmp_path. None touches the real /etc, /var, /run, /sys
or /proc.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import shutil
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ginn.cli.main import run as cli_run
from ginn.errors import GinnError
from ginn.hostsetup import record as rec
from ginn.hostsetup import undo as undo_mod
from ginn.hostsetup.setup import SetupResult, UserInfo, run_setup
from ginn.hostsetup.undo import (
    REASON_ADMIN_LINE,
    REASON_BOOT,
    REASON_DIFFERING,
    REASON_NOT_EMPTY,
    REASON_OTHER_LINES,
    REASON_PRE_EXISTING,
    REASON_RECORDS_DIR,
    REASON_SHARED,
    REASON_SHARED_GONE,
    REASON_SYMLINK,
    UNDO_STEPS,
    UndoResult,
    run_undo,
    runsv_running,
    wait_until_stopped,
)

USERS: dict[str, UserInfo] = {
    "alice": UserInfo(uid=1000, gid=1000, home="/home/alice"),
    "bob": UserInfo(uid=1001, gid=1001, home="/home/bob"),
}
NOW = 1_800_000_000.0
BOOT = "6f1d2c3b-0000-4000-8000-000000000001"
OTHER_BOOT = "6f1d2c3b-0000-4000-8000-000000000002"
RECORD = "etc/ginn/setup-alice.json"
RECORDS_DIR = "etc/ginn"
SHARED = rec.SHARED_RELPATH
SUBUID = "etc/subuid"
SUBGID = "etc/subgid"
SUBTREE = "sys/fs/cgroup/cgroup.subtree_control"
LOCK = "run/lock/ginn-host-setup.lock"
BOOT_FILE = "proc/sys/kernel/random/boot_id"
REALMS = "home/alice/.local/state/ginn/realms"
SHARED_LEFT = (
    (RECORDS_DIR, REASON_SHARED),
    (SHARED, REASON_SHARED),
    (SUBTREE, REASON_SHARED),
    ("run/ginn", REASON_SHARED),
    ("sys/fs/cgroup/ginn", REASON_SHARED),
)
OTHER_LINES_LEFT = ((SUBUID, REASON_OTHER_LINES), (SUBGID, REASON_OTHER_LINES))


class _Crash(Exception):
    """Stands in for a crash, such as SIGKILL or power loss, right after one step."""


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    """Keep fixture directories at 0755 whatever the umask, because setup checks the parents."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _crash_after(step: str) -> Callable[[str], None]:
    def hook(done: str) -> None:
        if done == step:
            raise _Crash(done)

    return hook


def _root_owned(path: Path) -> int:
    """The owner check the tests inject: every directory counts as owned by root."""
    return 0


def _common(root: Path) -> None:
    (root / LOCK).write_bytes(b"")
    os.chmod(root / LOCK, 0o600)
    (root / SUBTREE).write_text("cpu io memory\n", encoding="utf-8")
    (root / BOOT_FILE).parent.mkdir(parents=True)
    (root / BOOT_FILE).write_text(BOOT + "\n", encoding="ascii")


def _host(tmp_path: Path) -> Path:
    """A temporary root with the plain-directory layout that setup reads or writes."""
    root = tmp_path / "host"
    for rel in ("etc", "etc/sv", "var/service", "run/lock", "sys/fs/cgroup"):
        (root / rel).mkdir(parents=True)
    _common(root)
    return root


def _void_host(tmp_path: Path) -> Path:
    """A temporary root with the Void /var/service chain, read with readlink and lstat only."""
    root = tmp_path / "host"
    for rel in (
        "etc/sv",
        "etc/runit/runsvdir/default",
        "run/lock",
        "run/runit/runsvdir",
        "sys/fs/cgroup",
        "var",
    ):
        (root / rel).mkdir(parents=True)
    _common(root)
    os.symlink("../run/runit/runsvdir/current", root / "var/service")
    os.symlink("/etc/runit/runsvdir/current", root / "run/runit/runsvdir/current")
    os.symlink("default", root / "etc/runit/runsvdir/current")
    return root


def _setup(
    root: Path, user: str = "alice", *, fault: Callable[[str], None] | None = None
) -> SetupResult:
    return run_setup(
        user,
        root_dir=root,
        euid=0,
        lookup_user=USERS.__getitem__,
        clock=lambda: NOW,
        fault=fault,
        owner_of=_root_owned,
        lock_timeout=5.0,
    )


def _service(root: Path, *users: str, pids: bool = True) -> None:
    """What the run script writes at service start (ADR-003), emulated.

    The root controllers gain `pids`, and each user gets a cgroup directory with a child leaf and
    a /run/ginn/<uid> directory with a file inside.
    """
    if pids:
        (root / SUBTREE).write_text("cpu io memory pids\n", encoding="utf-8")
    for user in users:
        uid = USERS[user].uid
        (root / "sys/fs/cgroup/ginn" / str(uid) / "supervisor").mkdir(parents=True)
        (root / "run/ginn" / str(uid)).mkdir(parents=True)
        (root / "run/ginn" / str(uid) / "state").write_text("up\n", encoding="utf-8")


def _cgroup_rmdir(path: Path) -> None:
    """cgroupfs rmdir: control files go with the directory, and child cgroups block it."""
    if any(child.is_dir() for child in path.iterdir()):
        raise OSError(errno.ENOTEMPTY, os.strerror(errno.ENOTEMPTY), str(path))
    for child in path.iterdir():
        child.unlink()
    os.rmdir(path)


def _cgroup_write(path: Path, text: str) -> None:
    """One cgroupfs controller write: "+ctrl" enables and "-ctrl" disables, keeping the order."""
    op, ctrl = text[0], text[1:]
    tokens = path.read_text(encoding="utf-8").split()
    if op == "-":
        tokens = [token for token in tokens if token != ctrl]
    elif ctrl not in tokens:
        tokens.append(ctrl)
    path.write_text(" ".join(tokens) + "\n", encoding="utf-8")


def _stopped(svdir: Path, seconds: float) -> None:
    """runsv has already exited, so the wait returns at once."""


def _undo(
    root: Path,
    user: str = "alice",
    *,
    fault: Callable[[str], None] | None = None,
    wait: Callable[[Path, float], None] = _stopped,
) -> UndoResult:
    return run_undo(
        user,
        root_dir=root,
        euid=0,
        lookup_user=USERS.__getitem__,
        fault=fault,
        wait_service=wait,
        rmdir_cgroup=_cgroup_rmdir,
        write_control=_cgroup_write,
        lock_timeout=5.0,
    )


def _record(root: Path, user: str = "alice") -> rec.HostSetupRecord:
    loaded = rec.load(root / rec.relpath(user))
    assert loaded is not None
    return loaded


def _shared(root: Path) -> rec.SharedRecord:
    loaded = rec.load_shared(root / SHARED)
    assert loaded is not None
    return loaded


def _left(result: UndoResult) -> list[tuple[str, str]]:
    return [(item.path, item.reason) for item in result.left]


def _snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every entry under root with its kind, mode or target, digest and mtime (never followed)."""
    found: dict[str, tuple[Any, ...]] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as listing:
            children = list(listing)
        for child in children:
            path = Path(child.path)
            rel = path.relative_to(root).as_posix()
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                found[rel] = ("symlink", os.readlink(path), st.st_mtime_ns)
            elif stat.S_ISDIR(st.st_mode):
                found[rel] = ("dir", stat.S_IMODE(st.st_mode), st.st_mtime_ns)
                pending.append(path)
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                found[rel] = ("file", stat.S_IMODE(st.st_mode), digest, st.st_mtime_ns)
    return found


def _content(snapshot: dict[str, tuple[Any, ...]]) -> dict[str, tuple[Any, ...]]:
    """The snapshot without modification times, so that two trees compare by content."""
    return {rel: entry[:-1] for rel, entry in snapshot.items()}


def _without(content: dict[str, tuple[Any, ...]], *rels: str) -> dict[str, tuple[Any, ...]]:
    return {rel: entry for rel, entry in content.items() if rel not in rels}


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize("make_host", [_host, _void_host], ids=["plain", "void"])
def test_undo_of_a_single_user_restores_the_pre_setup_tree_with_nothing_left(
    tmp_path: Path, make_host: Callable[[Path], Path]
) -> None:
    root = make_host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root)
    _service(root, "alice")
    result = _undo(root)
    assert result.status == "undone"
    assert result.left == ()
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_a_subid_line_of_another_user_survives_and_the_tree_is_identical(tmp_path: Path) -> None:
    root = _host(tmp_path)
    (root / SUBUID).write_text("bob:200000:65536\n", encoding="utf-8")
    (root / SUBGID).write_text("bob:200000:65536\n", encoding="utf-8")
    before = _content(_snapshot(root))
    _setup(root)
    _service(root, "alice")
    result = _undo(root)
    assert result.left == ()
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize(("first", "second"), [("alice", "bob"), ("bob", "alice")])
def test_two_users_undone_in_either_order_restore_the_tree_and_report_the_shared_state(
    tmp_path: Path, first: str, second: str
) -> None:
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root, "alice")
    _setup(root, "bob")
    _service(root, "alice", "bob")
    shared_bytes = (root / SHARED).read_bytes()

    result = _undo(root, first)
    assert result.status == "undone"
    assert (root / SHARED).read_bytes() == shared_bytes
    assert (root / SUBTREE).read_text(encoding="utf-8") == "cpu io memory pids\n"
    assert sorted(_left(result)) == sorted((*SHARED_LEFT, *OTHER_LINES_LEFT))

    result = _undo(root, second)
    assert result.left == ()
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_a_user_with_realms_is_refused_and_the_tree_is_byte_and_mtime_identical(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    (root / REALMS).mkdir(parents=True)
    (root / REALMS / "web.json").write_text("{}\n", encoding="utf-8")
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _undo(root)
    assert caught.value.code == "GINN-E-UNDO-REALMS-EXIST"
    assert caught.value.exit_code == 4
    assert _snapshot(root) == before


@pytest.mark.fr("FR-003")
def test_a_non_root_undo_is_refused_before_the_account_or_any_path_is_read(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    before = _snapshot(root)

    def no_lookup(name: str) -> UserInfo:
        raise AssertionError(f"the account {name} must not be read by a non-root run")

    with pytest.raises(GinnError) as caught:
        run_undo("alice", root_dir=root, euid=1000, lookup_user=no_lookup)
    assert caught.value.code == "GINN-E-SETUP-ROOT"
    assert caught.value.exit_code == 77
    assert _snapshot(root) == before


@pytest.mark.fr("FR-003")
def test_undo_without_a_record_says_not_configured_and_changes_nothing(tmp_path: Path) -> None:
    root = _host(tmp_path)
    before = _snapshot(root)
    result = _undo(root)
    assert result.status == "not configured"
    assert result.left == ()
    assert _snapshot(root) == before


@pytest.mark.fr("FR-003")
def test_a_held_global_lock_times_out_with_setup_busy_and_changes_nothing(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    before = _snapshot(root)
    holder = os.open(root / LOCK, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(GinnError) as caught:
            run_undo(
                "alice",
                root_dir=root,
                euid=0,
                lookup_user=USERS.__getitem__,
                lock_timeout=0.2,
            )
    finally:
        os.close(holder)
    assert caught.value.code == "GINN-E-SETUP-BUSY"
    assert caught.value.exit_code == 75
    assert _snapshot(root) == before


@pytest.mark.fr("FR-003")
def test_an_admin_line_present_before_setup_is_kept_and_reported(tmp_path: Path) -> None:
    root = _host(tmp_path)
    (root / SUBUID).write_text("alice:100000:524288\n", encoding="utf-8")
    before = _content(_snapshot(root))
    _setup(root)
    _service(root, "alice")
    result = _undo(root)
    assert _left(result) == [(SUBUID, f"{REASON_ADMIN_LINE}: alice:100000:524288")]
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_an_admin_widened_line_after_setup_is_kept_as_differing_and_reported(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root)
    _service(root, "alice")
    recorded = _record(root).subids
    assert recorded is not None
    widened = f"alice:{recorded.uid.start}:{recorded.uid.count * 2}"
    (root / SUBUID).write_text(widened + "\n", encoding="utf-8")
    result = _undo(root)
    assert _left(result) == [(SUBUID, f"{REASON_DIFFERING}: {widened}")]
    assert (root / SUBUID).read_text(encoding="utf-8") == widened + "\n"
    assert _without(_content(_snapshot(root)), SUBUID) == _without(before, SUBUID)


@pytest.mark.fr("FR-003")
def test_a_changed_boot_leaves_the_root_controllers_and_reports_them(tmp_path: Path) -> None:
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root)
    _service(root, "alice")
    (root / BOOT_FILE).write_text(OTHER_BOOT + "\n", encoding="ascii")
    result = _undo(root)
    assert _left(result) == [(SUBTREE, REASON_BOOT)]
    assert (root / SUBTREE).read_text(encoding="utf-8") == "cpu io memory pids\n"
    assert _without(_content(_snapshot(root)), SUBTREE, BOOT_FILE) == _without(
        before, SUBTREE, BOOT_FILE
    )


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize(
    "step", ["pending", "dirs", "subuid", "subgid", "subids", "sv", "symlink", "complete"]
)
def test_undo_of_a_record_left_pending_restores_the_tree_from_the_pre_images(
    tmp_path: Path, step: str
) -> None:
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    with pytest.raises(_Crash):
        _setup(root, fault=_crash_after(step))
    result = _undo(root)
    assert result.left == ()
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize("step", UNDO_STEPS)
def test_a_crash_at_any_undo_step_is_converged_by_a_rerun(tmp_path: Path, step: str) -> None:
    clean = _host(tmp_path / "clean")
    _setup(clean)
    _service(clean, "alice")
    _undo(clean)
    expected = _content(_snapshot(clean))

    root = _host(tmp_path / "crashed")
    _setup(root)
    _service(root, "alice")
    with pytest.raises(_Crash):
        _undo(root, fault=_crash_after(step))
    rerun = _undo(root)
    final = _content(_snapshot(root))
    if step in ("shared", "record"):
        # Decision 15 (v) step 6 needs shared.json. After an interrupt past its removal, the
        # directory stays, and the rerun reports it (Residual (a) and (b)).
        assert set(final) - set(expected) == {RECORDS_DIR}
        assert final[RECORDS_DIR][0] == "dir"
        final = _without(final, RECORDS_DIR)
        reason = REASON_SHARED_GONE if step == "shared" else REASON_RECORDS_DIR
        assert _left(rerun) == [(RECORDS_DIR, reason)]
    else:
        assert rerun.left == ()
    assert final == expected


@pytest.mark.fr("FR-003")
def test_a_service_that_outlives_the_wait_stops_undo_and_a_rerun_completes(tmp_path: Path) -> None:
    root = _host(tmp_path)
    clean = _host(tmp_path / "clean")
    _setup(clean)
    _service(clean, "alice")
    _undo(clean)
    expected = _content(_snapshot(clean))

    _setup(root)
    _service(root, "alice")

    def still_running(svdir: Path, seconds: float) -> None:
        wait_until_stopped(svdir, 0.0, running=lambda _path: True)

    with pytest.raises(GinnError) as caught:
        _undo(root, wait=still_running)
    assert caught.value.code == "GINN-E-UNDO-SERVICE-RUNNING"
    assert caught.value.exit_code == 75
    assert (root / RECORD).is_file()
    assert (root / "sys/fs/cgroup/ginn/1000/supervisor").is_dir()

    rerun = _undo(root)
    assert rerun.left == ()
    assert _content(_snapshot(root)) == expected


@pytest.mark.fr("FR-003")
def test_a_shared_directory_with_unexpected_content_is_left_and_reported(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    (root / "run/ginn/extra").write_text("x\n", encoding="utf-8")
    result = _undo(root)
    assert _left(result) == [("run/ginn", REASON_NOT_EMPTY)]
    assert (root / "run/ginn/extra").is_file()


@pytest.mark.fr("FR-003")
def test_a_per_user_dir_holding_runsv_state_is_left_and_its_files_are_restored(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    (root / "etc/sv/ginn-alice/supervise").mkdir()
    (root / "etc/sv/ginn-alice/supervise/ok").write_text("", encoding="utf-8")
    result = _undo(root)
    assert _left(result) == [("etc/sv/ginn-alice", REASON_NOT_EMPTY)]
    assert not (root / "etc/sv/ginn-alice/run").exists()
    assert not (root / "etc/sv/ginn-alice/finish").exists()
    assert not (root / "etc/sv/ginn-alice/log").exists()


@pytest.mark.fr("FR-003")
def test_a_symbolic_link_where_a_service_directory_was_is_not_followed(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep\n", encoding="utf-8")
    shutil.rmtree(root / "run/ginn/1000")
    os.symlink(outside, root / "run/ginn/1000")
    result = _undo(root)
    assert sorted(_left(result)) == sorted(
        [("run/ginn", REASON_NOT_EMPTY), ("run/ginn/1000", REASON_SYMLINK)]
    )
    assert (outside / "keep").is_file()
    assert (root / "run/ginn/1000").is_symlink()


@pytest.mark.fr("FR-003")
def test_an_etc_ginn_created_by_setup_is_listed_for_removal_and_removed_last(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root)
    entry = next(item for item in _shared(root).paths if item.path == RECORDS_DIR)
    assert (entry.kind, entry.pre.state) == ("dir", "absent")
    _service(root, "alice")
    result = _undo(root)
    assert result.left == ()
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_a_pre_existing_etc_ginn_is_not_recorded_and_undo_leaves_it_as_pre_existing(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    (root / RECORDS_DIR).mkdir()
    before = _content(_snapshot(root))
    _setup(root)
    listed = {item.path for item in (*_shared(root).paths, *_record(root).paths)}
    assert RECORDS_DIR not in listed
    _service(root, "alice")
    result = _undo(root)
    assert _left(result) == [(RECORDS_DIR, REASON_PRE_EXISTING)]
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_runsv_is_running_while_something_reads_its_supervise_fifo(tmp_path: Path) -> None:
    svdir = tmp_path / "ginn-alice"
    (svdir / "supervise").mkdir(parents=True)
    fifo = svdir / "supervise" / "ok"
    os.mkfifo(fifo)
    assert not runsv_running(svdir)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert runsv_running(svdir)
    finally:
        os.close(reader)
    assert not runsv_running(svdir)


@pytest.mark.fr("FR-003")
def test_the_cli_refuses_a_non_root_undo_with_exit_77_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    before = _snapshot(root)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    status = cli_run(["host-setup", "--undo", "--user", "alice", "--root-dir", str(root)])
    assert status == 77
    assert _snapshot(root) == before


@pytest.mark.fr("FR-003")
def test_the_cli_undo_exits_zero_with_nothing_left_when_the_tree_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root)
    _service(root, "alice", pids=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(undo_mod, "_pwd_lookup", USERS.__getitem__)
    status = cli_run(["host-setup", "--undo", "--user", "alice", "--root-dir", str(root)])
    assert status == 0
    assert "host setup undone; 0 path(s) left in place" in capsys.readouterr().out
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_the_cli_prints_each_left_path_with_its_reason_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _host(tmp_path)
    _setup(root, "alice")
    _setup(root, "bob")
    _service(root, "alice", "bob")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(undo_mod, "_pwd_lookup", USERS.__getitem__)
    status = cli_run(["host-setup", "--undo", "--user", "alice", "--root-dir", str(root)])
    out = capsys.readouterr().out
    assert status == 0
    assert f"left {SHARED}: {REASON_SHARED}" in out
    assert f"left {SUBUID}: {REASON_OTHER_LINES}" in out


@pytest.mark.fr("FR-003")
def test_the_cli_reports_not_configured_with_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _host(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(undo_mod, "_pwd_lookup", USERS.__getitem__)
    status = cli_run(["host-setup", "--undo", "--user", "alice", "--root-dir", str(root)])
    assert status == 0
    assert "not configured; nothing changed" in capsys.readouterr().out


@pytest.mark.fr("FR-003")
def test_the_cli_refuses_a_user_with_realms_with_exit_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    (root / REALMS).mkdir(parents=True)
    (root / REALMS / "web.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(undo_mod, "_pwd_lookup", USERS.__getitem__)
    status = cli_run(["host-setup", "--undo", "--user", "alice", "--root-dir", str(root)])
    assert status == 4

