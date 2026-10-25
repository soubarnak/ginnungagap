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
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ginn.cli.main import run as cli_run
from ginn.errors import GinnError
from ginn.hostsetup import record as rec
from ginn.hostsetup import undo as undo_mod
from ginn.hostsetup._common import supervise_links
from ginn.hostsetup.setup import TMP_DIR, SetupResult, UserInfo, run_setup
from ginn.hostsetup.undo import (
    CGROUP_BUSY_DELAYS,
    CGROUP_BUSY_SECONDS,
    REASON_ADMIN_LINE,
    REASON_BOOT,
    REASON_BOOT_UNKNOWN,
    REASON_DIFFERING,
    REASON_LINK,
    REASON_NOT_EMPTY,
    REASON_ORIGINAL_BYTES,
    REASON_ORIGINAL_GONE,
    REASON_OTHER_LINES,
    REASON_PRE_EXISTING,
    REASON_PRE_EXISTING_LINE,
    REASON_PRE_EXISTING_LINE_ABSENT,
    REASON_PRE_PRESENT,
    REASON_RECORDS_DIR,
    REASON_RECORDS_EXTRA,
    REASON_SHARED,
    REASON_SHARED_BUSY,
    REASON_SHARED_GONE,
    REASON_SYMLINK,
    REASON_TERMINATOR,
    SERVICE_POLL_SECONDS,
    STATE_APPEAR_SECONDS,
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
    (SUBUID, REASON_SHARED),
    (SUBGID, REASON_SHARED),
)
"""What a non-last undo reports (Decision 23): each subordinate ID file once, as shared."""


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


def _script_owner(root: Path) -> Callable[[Path], int]:
    """The owner check that matches the run script (ADR-003, ADR-004), for the two test users.

    `sys/fs/cgroup/ginn/<uid>` and `run/ginn/<uid>` belong to their user, as the script chowns them.
    Every other directory belongs to root. Tests cannot chown, so the host is modelled here.
    """
    owners: dict[str, int] = {}
    for info in USERS.values():
        owners[f"sys/fs/cgroup/ginn/{info.uid}"] = info.uid
        owners[f"run/ginn/{info.uid}"] = info.uid

    def owner(path: Path) -> int:
        return owners.get(path.relative_to(root).as_posix(), 0)

    return owner


def _common(root: Path) -> None:
    (root / LOCK).write_bytes(b"")
    os.chmod(root / LOCK, 0o600)
    (root / SUBTREE).write_text("cpu io memory\n", encoding="utf-8")
    (root / BOOT_FILE).parent.mkdir(parents=True)
    (root / BOOT_FILE).write_text(BOOT + "\n", encoding="ascii")


def _host(tmp_path: Path) -> Path:
    """A temporary root with the plain-directory layout that setup reads or writes."""
    root = tmp_path / "host"
    for rel in ("etc", "etc/sv", "var/service", "run/lock", "run/runit", "sys/fs/cgroup"):
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
        "run/runit",
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
        owner_of=_script_owner(root),
        lock_timeout=5.0,
    )


def _service(root: Path, *users: str, pids: bool = True) -> None:
    """What the run script and runsv write when the service starts, emulated under `root`.

    The root controllers gain `pids`, and each user gets a cgroup directory with a child leaf, a
    /run/ginn/<uid> directory with a file inside, and runsv's state (`_runsv_state`).
    """
    if pids:
        (root / SUBTREE).write_text("cpu io memory pids\n", encoding="utf-8")
    for user in users:
        uid = USERS[user].uid
        (root / "sys/fs/cgroup/ginn" / str(uid) / "supervisor").mkdir(parents=True)
        (root / "run/ginn" / str(uid)).mkdir(parents=True)
        (root / "run/ginn" / str(uid) / "state").write_text("up\n", encoding="utf-8")
        _runsv_state(root, user)


def _runsv_state(root: Path, user: str) -> None:
    """runsv's state, created where the supervise links point, re-rooted under `root`.

    The links hold absolute targets under /run/runit. A test must never create them through the
    link, because the link resolves on the real host, so the state is created at the re-rooted
    target. runsv keeps `ok` and `control` as FIFOs and the rest as files (ADR-013, Decision 21).
    """
    for target in supervise_links(user).values():
        state = root / target.lstrip("/")
        state.mkdir(parents=True)
        for name in ("lock", "pid", "stat", "status"):
            (state / name).write_text("", encoding="utf-8")
        os.mkfifo(state / "ok")
        os.mkfifo(state / "control")


def _cgroup_rmdir(path: Path) -> None:
    """cgroupfs rmdir: control files go with the directory, and child cgroups block it."""
    if any(child.is_dir() for child in path.iterdir()):
        raise OSError(errno.ENOTEMPTY, os.strerror(errno.ENOTEMPTY), str(path))
    for child in path.iterdir():
        child.unlink()
    os.rmdir(path)


def _cgroup_write(path: Path, text: str) -> None:
    """One cgroupfs controller write: "+ctrl" enables and "-ctrl" disables, keeping the order.

    cgroup v2 refuses to disable a controller at the root while a child cgroup still exists (its
    subtree_control may enable it), so a disable with a child directory present fails with EBUSY.
    """
    op, ctrl = text[0], text[1:]
    if op == "-" and any(child.is_dir() for child in path.parent.iterdir()):
        raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), str(path))
    tokens = path.read_text(encoding="utf-8").split()
    if op == "-":
        tokens = [token for token in tokens if token != ctrl]
    elif ctrl not in tokens:
        tokens.append(ctrl)
    path.write_text(" ".join(tokens) + "\n", encoding="utf-8")


def _stopped(svdir: Path, seconds: float) -> None:
    """runsv has already exited, so the wait returns at once."""


class _Time:
    """Virtual time for the undo waits (ADR-013, Decision 26). `sleep` advances `now`.

    `on_sleep` runs after each sleep, with the number of sleeps so far, so a test can make runsv
    start or exit at a given tick. No test waits in real time.
    """

    def __init__(self, on_sleep: Callable[[int], None] | None = None) -> None:
        self.now = 0.0
        self.calls: list[float] = []
        self._on_sleep = on_sleep

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.now += seconds
        if self._on_sleep is not None:
            self._on_sleep(len(self.calls))


def _undo(
    root: Path,
    user: str = "alice",
    *,
    fault: Callable[[str], None] | None = None,
    wait: Callable[[Path, float], None] | None = _stopped,
    lookup: Callable[[str], UserInfo] = USERS.__getitem__,
    owner: Callable[[Path], int] | None = None,
    rmdir: Callable[[Path], None] | None = None,
    timer: _Time | None = None,
) -> UndoResult:
    """One undo. `wait=None` leaves the default waiter, which follows the supervise link.

    `owner` replaces the run-script owner model, and `rmdir` the cgroup rmdir (for EBUSY). `timer`
    times the waits. Without it, each undo gets its own virtual clock, so no test sleeps.
    """
    virtual = timer or _Time()
    return run_undo(
        user,
        root_dir=root,
        euid=0,
        lookup_user=lookup,
        fault=fault,
        wait_service=wait,
        rmdir_cgroup=rmdir or _cgroup_rmdir,
        write_control=_cgroup_write,
        owner_of=owner or _script_owner(root),
        lock_timeout=5.0,
        clock=virtual.clock,
        sleep=virtual.sleep,
    )


def _moved_account(name: str) -> UserInfo:
    """Alice's account after a change of uid, gid and home. Undo must use the record instead."""
    if name != "alice":
        raise KeyError(name)
    return UserInfo(uid=1500, gid=1500, home="/home/alice2")


def _no_account(name: str) -> UserInfo:
    """Alice's account after it was deleted (ADR-013, Decision 24)."""
    raise KeyError(name)


def _admin_lines(root: Path, text: str) -> None:
    """Write the admin's own subordinate ID lines, exactly as `text`, to both files."""
    (root / SUBUID).write_text(text, encoding="utf-8")
    (root / SUBGID).write_text(text, encoding="utf-8")


def _append_admin_line(root: Path, line: str) -> None:
    """Append one line to both subordinate ID files, as an admin edit made after a setup."""
    for rel in (SUBUID, SUBGID):
        path = root / rel
        path.write_text(path.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")


def _differing(before: dict[str, tuple[Any, ...]], after: dict[str, tuple[Any, ...]]) -> set[str]:
    """The paths whose content, kind or mode differ between two content snapshots."""
    return {rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel)}


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
            elif stat.S_ISFIFO(st.st_mode):
                # Reading a FIFO with no writer blocks, so only its kind and mode are recorded.
                found[rel] = ("fifo", stat.S_IMODE(st.st_mode), st.st_mtime_ns)
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
    assert sorted(_left(result)) == sorted(SHARED_LEFT)

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
                owner_of=_script_owner(root),
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


PRE_EXISTING = "alice:100000:65536"
"""A line the user held before setup. Its range is too small for setup, so setup appends another."""


@pytest.mark.fr("FR-003")
def test_a_line_the_user_held_before_setup_is_kept_and_reported_as_pre_existing(
    tmp_path: Path,
) -> None:
    """Setup's line goes, and the user's own line from before setup stays. The report says so, and
    it does not read as a conflict (ADR-013, Decision 15 (i))."""
    root = _host(tmp_path)
    _admin_lines(root, PRE_EXISTING + "\n")
    before = _content(_snapshot(root))
    _setup(root)
    _service(root, "alice")
    recorded = _record(root).subids
    assert recorded is not None
    assert recorded.uid.render() != PRE_EXISTING
    result = _undo(root)
    assert sorted(_left(result)) == sorted(
        [
            (SUBUID, f"{REASON_PRE_EXISTING_LINE}: {PRE_EXISTING}"),
            (SUBGID, f"{REASON_PRE_EXISTING_LINE}: {PRE_EXISTING}"),
        ]
    )
    assert (root / SUBUID).read_text(encoding="utf-8") == PRE_EXISTING + "\n"
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_a_pre_existing_line_is_reported_as_such_when_setups_line_is_already_gone(
    tmp_path: Path,
) -> None:
    """Setup's line was removed by hand before undo, so undo removes nothing and says so."""
    root = _host(tmp_path)
    _admin_lines(root, PRE_EXISTING + "\n")
    _setup(root)
    _service(root, "alice")
    _admin_lines(root, PRE_EXISTING + "\n")
    result = _undo(root)
    assert sorted(_left(result)) == sorted(
        [
            (SUBUID, f"{REASON_PRE_EXISTING_LINE_ABSENT}: {PRE_EXISTING}"),
            (SUBGID, f"{REASON_PRE_EXISTING_LINE_ABSENT}: {PRE_EXISTING}"),
        ]
    )
    assert (root / SUBUID).read_text(encoding="utf-8") == PRE_EXISTING + "\n"


@pytest.mark.fr("FR-003")
def test_a_pre_setup_line_and_an_admin_edit_of_the_same_name_keep_their_own_reasons(
    tmp_path: Path,
) -> None:
    """The user's line from before setup and an admin's later edit of the same name both stay in
    the file. The pre-setup line gets its own reason, and the edit is reported as differing. Setup's
    line is the one that goes (ADR-013 Decision 15 (i))."""
    root = _host(tmp_path)
    _admin_lines(root, PRE_EXISTING + "\n")
    _setup(root)
    _service(root, "alice")
    recorded = _record(root).subids
    assert recorded is not None
    edited = f"alice:{recorded.uid.start + recorded.uid.count * 2}:{recorded.uid.count}"
    _append_admin_line(root, edited)
    result = _undo(root)
    assert sorted(_left(result)) == sorted(
        [
            (SUBUID, f"{REASON_PRE_EXISTING_LINE}: {PRE_EXISTING}"),
            (SUBUID, f"{REASON_DIFFERING}: {edited}"),
            (SUBGID, f"{REASON_PRE_EXISTING_LINE}: {PRE_EXISTING}"),
            (SUBGID, f"{REASON_DIFFERING}: {edited}"),
        ]
    )
    assert (root / SUBUID).read_text(encoding="utf-8") == f"{PRE_EXISTING}\n{edited}\n"
    assert (root / SUBGID).read_text(encoding="utf-8") == f"{PRE_EXISTING}\n{edited}\n"


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
    """A rerun after a crash at any step ends with the same tree and nothing left (NFR-030).

    On the rename path, a crash after the move aside leaves the temporary directory, and the rerun
    sweeps it under the lock, so no /etc/ginn or temporary name is left behind.
    """
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
    assert rerun.left == ()
    assert _content(_snapshot(root)) == expected
    assert not (root / "etc/.ginn.ginn-tmp").exists()


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
def test_a_root_controller_that_a_foreign_child_cgroup_still_uses_is_refused_and_reported(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    (root / "sys/fs/cgroup/ginn/9999").mkdir()
    result = _undo(root)
    assert sorted(_left(result)) == sorted(
        [
            ("sys/fs/cgroup/ginn", REASON_NOT_EMPTY),
            (SUBTREE, f"-pids refused: {os.strerror(errno.EBUSY)}"),
        ]
    )
    assert (root / SUBTREE).read_text(encoding="utf-8") == "cpu io memory pids\n"


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
def test_runsv_state_through_the_supervise_links_is_removed_and_the_links_are_restored(
    tmp_path: Path,
) -> None:
    """The real runsv shape (ADR-013, Decision 21): the links, and runit's names in /run/runit."""
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root)
    _service(root, "alice")
    assert (root / "etc/sv/ginn-alice/supervise").is_symlink()
    assert (root / "etc/sv/ginn-alice/log/supervise").is_symlink()
    result = _undo(root)
    assert _left(result) == []
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_the_supervise_links_stay_in_place_through_the_wait_and_go_after_it(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    seen: list[bool] = []

    def waiter(svdir: Path, seconds: float) -> None:
        seen.append((root / "etc/sv/ginn-alice/supervise").is_symlink())
        seen.append((root / "run/runit/supervise.ginn-alice/ok").exists())

    _undo(root, wait=waiter)
    assert seen == [True, True]
    assert not (root / "run/runit/supervise.ginn-alice").exists()
    assert not (root / "etc/sv/ginn-alice").exists()


@pytest.mark.fr("FR-003")
def test_a_runsv_state_dir_holding_a_name_runit_does_not_keep_is_left_and_named(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    (root / "run/runit/supervise.ginn-alice/extra").write_text("x\n", encoding="utf-8")
    result = _undo(root)
    assert _left(result) == [
        ("run/runit/supervise.ginn-alice", f"{undo_mod.REASON_RUNSV_EXTRA}: extra"),
    ]
    assert (root / "run/runit/supervise.ginn-alice/extra").is_file()
    assert not (root / "etc/sv/ginn-alice/supervise").exists()


@pytest.mark.fr("FR-003")
def test_a_regular_file_at_supervise_ok_is_refused_with_a_catalogue_error_and_the_record_kept(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    ok = root / "run/runit/supervise.ginn-alice/ok"
    ok.unlink()
    ok.write_text("", encoding="utf-8")
    # The waiter reads a service directory whose supervise link points at the re-rooted state.
    svc = tmp_path / "svc"
    svc.mkdir()
    os.symlink(root / "run/runit/supervise.ginn-alice", svc / "supervise")
    with pytest.raises(GinnError) as caught:
        _undo(root, wait=lambda _svdir, seconds: wait_until_stopped(svc, seconds))
    assert caught.value.code == "GINN-E-UNDO-SUPERVISE-STATE"
    assert caught.value.exit_code == 77
    assert "regular file" in caught.value.message
    assert "sudo ls -l" in caught.value.remedy
    assert (root / RECORD).is_file()


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
    monkeypatch.setattr(undo_mod, "_owner_uid", _script_owner(root))
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
    monkeypatch.setattr(undo_mod, "_owner_uid", _script_owner(root))
    status = cli_run(["host-setup", "--undo", "--user", "alice", "--root-dir", str(root)])
    out = capsys.readouterr().out
    assert status == 0
    assert f"left {SHARED}: {REASON_SHARED}" in out
    assert f"left {SUBUID}: {REASON_SHARED}" in out


@pytest.mark.fr("FR-003")
def test_the_cli_reports_not_configured_with_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _host(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(undo_mod, "_pwd_lookup", USERS.__getitem__)
    monkeypatch.setattr(undo_mod, "_owner_uid", _script_owner(root))
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
    monkeypatch.setattr(undo_mod, "_owner_uid", _script_owner(root))
    status = cli_run(["host-setup", "--undo", "--user", "alice", "--root-dir", str(root)])
    assert status == 4


ADMIN = "carol:200000:65536"
"""An admin's own range in both subordinate ID files, present before any setup."""
CAROL_LATER = "carol:300000:65536"
"""An admin's range that is added after the first setup."""
TERMINATOR_LEFT = ((SUBUID, REASON_TERMINATOR), (SUBGID, REASON_TERMINATOR))


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize("first_setup", ["alice", "bob"])
@pytest.mark.parametrize("first_undo", ["alice", "bob"])
def test_two_users_and_an_admin_line_undo_to_the_pre_setup_tree_in_either_order(
    tmp_path: Path, first_setup: str, first_undo: str
) -> None:
    root = _host(tmp_path)
    _admin_lines(root, ADMIN + "\n")
    before = _content(_snapshot(root))
    _setup(root, first_setup)
    _setup(root, "bob" if first_setup == "alice" else "alice")
    _service(root, "alice", "bob")
    second_undo = "bob" if first_undo == "alice" else "alice"

    first = _undo(root, first_undo)
    assert first.status == "undone"
    assert sorted(_left(first)) == sorted(SHARED_LEFT)

    last = _undo(root, second_undo)
    assert last.left == ()
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize("first_setup", ["alice", "bob"])
@pytest.mark.parametrize("first_undo", ["alice", "bob"])
def test_an_admin_line_without_a_trailing_newline_is_reported_as_a_terminator_difference(
    tmp_path: Path, first_setup: str, first_undo: str
) -> None:
    """Setup appends a newline after the admin's last line. Undo removes its own lines and never
    writes bytes back, so both files differ from the original only in that terminator. The last
    undo reports both files, and they are the only paths that differ (ADR-013, Decision 15 (i))."""
    root = _host(tmp_path)
    _admin_lines(root, ADMIN)
    before = _content(_snapshot(root))
    _setup(root, first_setup)
    _setup(root, "bob" if first_setup == "alice" else "alice")
    _service(root, "alice", "bob")
    second_undo = "bob" if first_undo == "alice" else "alice"

    first = _undo(root, first_undo)
    assert sorted(_left(first)) == sorted(SHARED_LEFT)

    last = _undo(root, second_undo)
    assert sorted(_left(last)) == sorted(TERMINATOR_LEFT)
    assert _differing(before, _content(_snapshot(root))) == {SUBUID, SUBGID}


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize("first_setup", ["alice", "bob"])
@pytest.mark.parametrize("first_undo", ["alice", "bob"])
def test_an_admin_line_added_after_the_first_setup_is_reported_once_by_the_last_undo(
    tmp_path: Path, first_setup: str, first_undo: str
) -> None:
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root, first_setup)
    _append_admin_line(root, CAROL_LATER)
    _setup(root, "bob" if first_setup == "alice" else "alice")
    _service(root, "alice", "bob")
    second_undo = "bob" if first_undo == "alice" else "alice"

    first = _undo(root, first_undo)
    assert sorted(_left(first)) == sorted(SHARED_LEFT)

    last = _undo(root, second_undo)
    assert sorted(_left(last)) == sorted(
        [
            (SUBUID, f"{REASON_OTHER_LINES}: {CAROL_LATER}"),
            (SUBGID, f"{REASON_OTHER_LINES}: {CAROL_LATER}"),
        ]
    )
    assert _differing(before, _content(_snapshot(root))) == {SUBUID, SUBGID}


@pytest.mark.fr("FR-003")
def test_an_admin_line_of_the_users_own_range_is_reported_once_as_an_admin_line(
    tmp_path: Path,
) -> None:
    """The admin adds alice's line after bob's setup, and alice's setup then takes that range. The
    line is in alice's pre-image but not in the original, so it must be reported once."""
    root = _host(tmp_path)
    _setup(root, "bob")
    _append_admin_line(root, "alice:700000:524288")
    _setup(root, "alice")
    _service(root, "alice", "bob")
    _undo(root, "bob")

    result = _undo(root, "alice")
    assert sorted(_left(result)) == sorted(
        [
            (SUBUID, f"{REASON_ADMIN_LINE}: alice:700000:524288"),
            (SUBGID, f"{REASON_ADMIN_LINE}: alice:700000:524288"),
        ]
    )
    assert (root / SUBUID).read_text(encoding="utf-8") == "alice:700000:524288\n"


@pytest.mark.fr("FR-003")
def test_a_comment_added_to_a_subordinate_id_file_is_kept_and_reported_as_original_bytes(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _admin_lines(root, ADMIN + "\n")
    _setup(root)
    _service(root, "alice")
    (root / SUBUID).write_text(
        (root / SUBUID).read_text(encoding="utf-8") + "# note\n", encoding="utf-8"
    )
    result = _undo(root)
    assert _left(result) == [(SUBUID, REASON_ORIGINAL_BYTES)]
    assert (root / SUBUID).read_text(encoding="utf-8") == ADMIN + "\n# note\n"


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize("step", UNDO_STEPS)
@pytest.mark.parametrize("crashed_undo", ["first", "second"])
def test_a_crash_at_any_step_of_either_undo_is_converged_by_a_rerun_in_two_users_with_an_admin_line(
    tmp_path: Path, step: str, crashed_undo: str
) -> None:
    """Every fault point fires on every undo, the non-last one included, so each crash is real.

    The rerun of the crashed undo, and then the other undo, end with the pre-setup tree (NFR-030),
    and the last undo leaves nothing. The delegated directories belong to their users, as the run
    script leaves them.
    """
    root = _host(tmp_path)
    _admin_lines(root, ADMIN + "\n")
    before = _content(_snapshot(root))
    _setup(root, "alice")
    _setup(root, "bob")
    _service(root, "alice", "bob")
    if crashed_undo == "first":
        with pytest.raises(_Crash):
            _undo(root, "alice", fault=_crash_after(step))
        _undo(root, "alice")
        last = _undo(root, "bob")
    else:
        _undo(root, "alice")
        with pytest.raises(_Crash):
            _undo(root, "bob", fault=_crash_after(step))
        last = _undo(root, "bob")
    assert last.left == ()
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_a_deleted_account_is_noted_and_its_record_is_undone_with_the_recorded_identity(
    tmp_path: Path,
) -> None:
    """Decision 24: undo takes uid, gid and home from the record, so a missing account undoes."""
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root)
    _service(root, "alice")
    result = _undo(root, lookup=_no_account)
    assert result.status == "undone"
    assert _left(result) == []
    assert result.notes == (
        "account alice does not exist now; undo used the uid, gid and home of the record",
    )
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_a_changed_uid_is_warned_and_undo_removes_the_recorded_uid_and_keeps_the_new_one(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice", pids=False)
    (root / "run/ginn/1500").mkdir()
    (root / "run/ginn/1500/state").write_text("another user\n", encoding="utf-8")
    (root / "sys/fs/cgroup/ginn/1500/supervisor").mkdir(parents=True)
    result = _undo(root, lookup=_moved_account)
    assert len(result.notes) == 1
    assert result.notes[0].startswith(
        "warning: account alice now has uid 1500, gid 1500 and home /home/alice2"
    )
    assert result.notes[0].endswith("undo used the record")
    assert sorted(_left(result)) == sorted(
        [("run/ginn", REASON_NOT_EMPTY), ("sys/fs/cgroup/ginn", REASON_NOT_EMPTY)]
    )
    assert not (root / "run/ginn/1000").exists()
    assert not (root / "sys/fs/cgroup/ginn/1000").exists()
    assert (root / "run/ginn/1500/state").is_file()
    assert (root / "sys/fs/cgroup/ginn/1500/supervisor").is_dir()


@pytest.mark.fr("FR-003")
def test_realms_under_the_recorded_home_refuse_undo_after_the_account_moved(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    (root / REALMS).mkdir(parents=True)
    (root / REALMS / "web.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(GinnError) as caught:
        _undo(root, lookup=_moved_account)
    assert caught.value.code == "GINN-E-UNDO-REALMS-EXIST"


@pytest.mark.fr("FR-003")
def test_realms_under_a_new_home_do_not_block_undo_of_the_recorded_home(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    new_realms = root / "home/alice2/.local/state/ginn/realms"
    new_realms.mkdir(parents=True)
    (new_realms / "web.json").write_text("{}\n", encoding="utf-8")
    result = _undo(root, lookup=_moved_account)
    assert result.status == "undone"


@pytest.mark.fr("FR-003")
def test_a_supervise_link_that_points_elsewhere_is_left_and_reported(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    link = root / "etc/sv/ginn-alice/supervise"
    link.unlink()
    os.symlink("/run/runit/elsewhere", link)
    result = _undo(root)
    assert sorted(_left(result)) == sorted(
        [("etc/sv/ginn-alice", REASON_NOT_EMPTY), ("etc/sv/ginn-alice/supervise", REASON_LINK)]
    )
    assert os.readlink(link) == "/run/runit/elsewhere"
    assert not (root / "etc/sv/ginn-alice/log/supervise").is_symlink()


@pytest.mark.fr("FR-003")
def test_a_supervise_link_whose_pre_image_was_a_symlink_gets_its_target_back(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    (root / "etc/sv/ginn-alice").mkdir()
    os.symlink("/elsewhere", root / "etc/sv/ginn-alice/supervise")
    before = _content(_snapshot(root))
    _setup(root)
    assert os.readlink(root / "etc/sv/ginn-alice/supervise") == "/run/runit/supervise.ginn-alice"
    _service(root, "alice")
    result = _undo(root)
    assert _left(result) == [("etc/sv/ginn-alice", REASON_PRE_PRESENT)]
    assert os.readlink(root / "etc/sv/ginn-alice/supervise") == "/elsewhere"
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_an_extra_name_in_etc_ginn_keeps_the_directory_and_removes_the_two_records(
    tmp_path: Path,
) -> None:
    """Decision 25, the fallback path: the rename needs a directory that holds only Decision 16's
    names, so undo removes shared.json and the own record, and reports the directory."""
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    (root / RECORDS_DIR / "extra").write_text("x\n", encoding="utf-8")
    result = _undo(root)
    assert _left(result) == [(RECORDS_DIR, REASON_RECORDS_EXTRA)]
    assert not (root / SHARED).exists()
    assert not (root / RECORD).exists()
    assert (root / RECORDS_DIR / "extra").is_file()


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize("planted_at", ["settle", "shared"])
def test_a_name_that_appears_in_etc_ginn_after_the_plan_is_caught_by_the_recheck(
    tmp_path: Path, planted_at: str
) -> None:
    """Decision 25 rechecks the directory right before the rename, and after the move undo removes
    only the four names of Decision 16. A name planted before the rename takes the fallback path,
    and a name planted after the move is reported and kept in the temporary directory."""
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")

    def plant(done: str) -> None:
        if done == planted_at:
            target = root / RECORDS_DIR if planted_at == "settle" else root / TMP_DIR
            (target / "extra").write_text("x\n", encoding="utf-8")

    result = _undo(root, fault=plant)
    if planted_at == "settle":
        assert _left(result) == [(RECORDS_DIR, REASON_RECORDS_EXTRA)]
        assert not (root / SHARED).exists()
        assert not (root / RECORD).exists()
        assert (root / RECORDS_DIR / "extra").is_file()
    else:
        assert _left(result) == [(f"{TMP_DIR}/extra", REASON_RECORDS_EXTRA)]
        assert not (root / RECORD).exists()
        assert (root / TMP_DIR / "extra").is_file()


@pytest.mark.fr("FR-003")
def test_a_last_undo_without_shared_json_keeps_etc_ginn_and_reports_the_subid_files(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    (root / SHARED).unlink()
    result = _undo(root)
    assert result.notes == (
        "shared.json is already gone; an earlier undo applied the shared steps",
    )
    assert sorted(_left(result)) == sorted(
        [
            (RECORDS_DIR, REASON_SHARED_GONE),
            (SUBUID, REASON_ORIGINAL_GONE),
            (SUBGID, REASON_ORIGINAL_GONE),
        ]
    )
    assert not (root / RECORD).exists()


@pytest.mark.fr("FR-003")
def test_a_shared_json_without_the_subordinate_id_original_keeps_the_file_and_reports_it(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _admin_lines(root, ADMIN + "\n")
    _setup(root)
    _service(root, "alice")
    shared = _shared(root)
    pruned = replace(shared, paths=tuple(entry for entry in shared.paths if entry.path != SUBUID))
    rec.save_shared(root / SHARED, pruned)
    result = _undo(root)
    assert (SUBUID, REASON_ORIGINAL_GONE) in _left(result)
    assert (root / SUBUID).read_text(encoding="utf-8") == ADMIN + "\n"


@pytest.mark.fr("FR-003")
def test_an_empty_etc_ginn_without_records_is_reported_and_not_removed(tmp_path: Path) -> None:
    root = _host(tmp_path)
    (root / RECORDS_DIR).mkdir()
    before = _snapshot(root)
    result = _undo(root)
    assert result.status == "not configured"
    assert _left(result) == [(RECORDS_DIR, REASON_RECORDS_DIR)]
    assert _snapshot(root) == before


@pytest.mark.fr("FR-003")
def test_without_a_boot_id_the_root_controllers_are_left_and_reported_as_boot_unknown(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    (root / BOOT_FILE).unlink()
    result = _undo(root)
    assert _left(result) == [(SUBTREE, REASON_BOOT_UNKNOWN)]
    assert (root / SUBTREE).read_text(encoding="utf-8") == "cpu io memory pids\n"


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize("name", ["../alice", "alice/..", "Alice", ""])
def test_an_invalid_user_name_is_refused_before_any_path_is_built_from_it(
    tmp_path: Path, name: str
) -> None:
    """The name passes the setup rule (`rec.USER_NAME`) before it builds a record path, so
    `../alice` cannot reach the parent of the records directory."""
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        run_undo(
            name,
            root_dir=root,
            euid=0,
            lookup_user=USERS.__getitem__,
            owner_of=_script_owner(root),
            lock_timeout=5.0,
        )
    assert caught.value.code == "GINN-E-NO-SUCH-USER"
    assert caught.value.exit_code == 2
    assert _snapshot(root) == before


@pytest.mark.fr("FR-003")
def test_a_record_that_names_another_user_is_refused_and_nothing_is_changed(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root, "alice")
    shutil.copy2(root / RECORD, root / "etc/ginn/setup-bob.json")
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _undo(root, "bob")
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert caught.value.exit_code == 77
    assert "holds the record of user alice, not bob" in caught.value.message
    assert _snapshot(root) == before


@pytest.mark.fr("FR-003")
def test_the_default_waiter_follows_the_supervise_link_under_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The waiter is not injected here. It re-roots the link's /run/runit target under the root, so
    a reader of the FIFO under the root keeps the wait running (ADR-013, Decision 22)."""
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root)
    _service(root, "alice")
    monkeypatch.setattr(undo_mod, "SERVICE_WAIT_SECONDS", 0.0)
    reader = os.open(root / "run/runit/supervise.ginn-alice/ok", os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(GinnError) as caught:
            _undo(root, wait=None)
        assert caught.value.code == "GINN-E-UNDO-SERVICE-RUNNING"
    finally:
        os.close(reader)
    assert (root / "etc/sv/ginn-alice/supervise").is_symlink()
    assert (root / RECORD).is_file()

    result = _undo(root, wait=None)
    assert result.left == ()
    assert _content(_snapshot(root)) == before


@pytest.mark.fr("FR-003")
def test_the_default_waiter_refuses_a_state_path_that_leaves_the_root(
    tmp_path: Path,
) -> None:
    """A symbolic link on the way to the runsv state is refused, so the waiter never reads outside
    the root. The record is kept and nothing outside is touched."""
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    outside = tmp_path / "outside"
    state = outside / "supervise.ginn-alice"
    state.mkdir(parents=True)
    os.mkfifo(state / "ok")
    shutil.rmtree(root / "run/runit")
    os.symlink(outside, root / "run/runit")
    with pytest.raises(GinnError) as caught:
        _undo(root, wait=None)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert "is a symlink" in caught.value.message
    assert (root / RECORD).is_file()
    assert sorted(os.listdir(state)) == ["ok"]


@pytest.mark.fr("FR-003")
def test_a_widened_line_of_the_user_is_warned_on_a_non_last_undo_too(tmp_path: Path) -> None:
    """Decision 15 (i) warns per line on every undo. Here alice's undo is not the last one, so the
    shared-file report of Decision 23 is additional to the per-line warning, and the last undo still
    reports the widened line as one that is not in the original."""
    root = _host(tmp_path)
    _setup(root, "alice")
    _setup(root, "bob")
    _service(root, "alice", "bob")
    recorded = _record(root, "alice").subids
    assert recorded is not None
    widened = f"alice:{recorded.uid.start}:{recorded.uid.count * 2}"
    (root / SUBUID).write_text(
        (root / SUBUID).read_text(encoding="utf-8").replace(recorded.uid.render(), widened),
        encoding="utf-8",
    )
    first = _undo(root, "alice")
    assert (SUBUID, f"{REASON_DIFFERING}: {widened}") in _left(first)
    assert (SUBUID, REASON_SHARED) in _left(first)

    last = _undo(root, "bob")
    assert _left(last) == [(SUBUID, f"{REASON_OTHER_LINES}: {widened}")]
    assert (root / SUBUID).read_text(encoding="utf-8") == widened + "\n"


@pytest.mark.fr("FR-003")
def test_a_delegated_directory_owned_by_another_uid_than_the_records_is_refused(
    tmp_path: Path,
) -> None:
    """ADR-003 accepts the record's uid as owner of `ginn/<uid>`, and no other uid (Decision 27)."""
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    script = _script_owner(root)

    def bobs_directory(path: Path) -> int:
        if path.relative_to(root).as_posix() == "sys/fs/cgroup/ginn/1000":
            return USERS["bob"].uid
        return script(path)

    with pytest.raises(GinnError) as caught:
        _undo(root, owner=bobs_directory)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert (
        "is owned by uid 1001, but uid 1000, the uid of the Host Setup Record"
        in caught.value.message
    )
    assert (root / RECORD).is_file()
    assert (root / "sys/fs/cgroup/ginn/1000/supervisor").is_dir()


@pytest.mark.fr("FR-003")
def test_a_delegated_directory_writable_by_its_group_is_refused_even_with_the_right_owner(
    tmp_path: Path,
) -> None:
    """The owner exemption does not relax the mode rule: the run script's directory is 0755."""
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    os.chmod(root / "sys/fs/cgroup/ginn/1000", 0o775)  # noqa: S103 - a group-writable delegated dir
    with pytest.raises(GinnError) as caught:
        _undo(root)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert "writable by group or others" in caught.value.message
    assert (root / RECORD).is_file()


@pytest.mark.fr("FR-003")
def test_a_user_owned_directory_outside_the_delegation_is_refused(tmp_path: Path) -> None:
    """Only the two delegated directories may be owned by the user. `etc/sv/ginn-<user>` may not."""
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    script = _script_owner(root)

    def user_owns_the_service_directory(path: Path) -> int:
        if path.relative_to(root).as_posix() == "etc/sv/ginn-alice":
            return USERS["alice"].uid
        return script(path)

    with pytest.raises(GinnError) as caught:
        _undo(root, owner=user_owns_the_service_directory)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert "is owned by uid 1000, but uid 0 must own it" in caught.value.message
    assert (root / RECORD).is_file()


@pytest.mark.fr("FR-003")
def test_a_cgroup_directory_busy_once_is_retried_and_the_undo_completes(tmp_path: Path) -> None:
    """One EBUSY from a cgroup rmdir costs one pause, and the undo ends with the pre-setup tree."""
    clean = _host(tmp_path / "clean")
    _setup(clean)
    _service(clean, "alice")
    _undo(clean)
    expected = _content(_snapshot(clean))

    root = _host(tmp_path / "busy")
    _setup(root)
    _service(root, "alice")
    tries: list[Path] = []

    def busy_once(path: Path) -> None:
        tries.append(path)
        if len(tries) == 1:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), str(path))
        _cgroup_rmdir(path)

    timer = _Time()
    result = _undo(root, rmdir=busy_once, timer=timer)
    assert result.left == ()
    assert timer.calls == [CGROUP_BUSY_DELAYS[0]]
    assert len(tries) == 4
    assert not (root / RECORD).exists()
    assert _content(_snapshot(root)) == expected


@pytest.mark.fr("FR-003")
def test_a_per_user_cgroup_directory_busy_to_the_bound_stops_undo_and_keeps_the_record(
    tmp_path: Path,
) -> None:
    """EBUSY that lasts past the retries stops undo with GINN-E-UNDO-CGROUP-BUSY (exit 75). The
    record stays, so a rerun, with the process gone, completes the undo (ADR-013, Decision 26)."""
    clean = _host(tmp_path / "clean")
    _setup(clean)
    _service(clean, "alice")
    _undo(clean)
    expected = _content(_snapshot(clean))

    root = _host(tmp_path / "held")
    _setup(root)
    _service(root, "alice")

    def busy(path: Path) -> None:
        raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), str(path))

    timer = _Time()
    with pytest.raises(GinnError) as caught:
        _undo(root, rmdir=busy, timer=timer)
    assert caught.value.code == "GINN-E-UNDO-CGROUP-BUSY"
    assert caught.value.exit_code == 75
    assert "sys/fs/cgroup/ginn/1000/supervisor" in caught.value.message
    assert "cgroup.procs" in caught.value.remedy
    assert timer.calls == list(CGROUP_BUSY_DELAYS)
    assert sum(timer.calls) == pytest.approx(CGROUP_BUSY_SECONDS)
    assert (root / RECORD).is_file()
    assert (root / SHARED).is_file()
    assert (root / "sys/fs/cgroup/ginn/1000/supervisor").is_dir()

    rerun = _undo(root)
    assert rerun.left == ()
    assert not (root / RECORD).exists()
    assert _content(_snapshot(root)) == expected


@pytest.mark.fr("FR-003")
def test_setup_after_a_busy_undo_is_not_refused_by_the_user_owned_leftover(tmp_path: Path) -> None:
    """The leftover `ginn/<uid>` is the user's delegated directory (Decision 27). A busy undo keeps
    the record, and a later setup reads that directory without refusing it."""

    def busy(path: Path) -> None:
        raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), str(path))

    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    with pytest.raises(GinnError) as caught:
        _undo(root, rmdir=busy)
    assert caught.value.code == "GINN-E-UNDO-CGROUP-BUSY"

    again = _setup(root)
    assert again.status == "configured"
    assert (root / RECORD).is_file()
    assert (root / "var/service/ginn-alice").is_symlink()


@pytest.mark.fr("FR-003")
def test_a_shared_cgroup_directory_that_stays_busy_is_reported_and_the_undo_completes(
    tmp_path: Path,
) -> None:
    """A shared cgroup directory that stays busy is retried, then reported with its remedy, as
    Decision 15 (iii) allows for a directory another party uses. Undo goes on and removes the
    record (ADR-013, Decision 26)."""
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")

    def busy_shared(path: Path) -> None:
        if path.name == "ginn" and path.parent.name == "cgroup":
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), str(path))
        _cgroup_rmdir(path)

    timer = _Time()
    result = _undo(root, rmdir=busy_shared, timer=timer)
    assert ("sys/fs/cgroup/ginn", REASON_SHARED_BUSY) in _left(result)
    assert timer.calls == list(CGROUP_BUSY_DELAYS)
    assert (root / "sys/fs/cgroup/ginn").is_dir()
    assert not (root / RECORD).exists()


@pytest.mark.fr("FR-003")
def test_a_runsv_state_that_appears_after_undo_starts_is_waited_for(tmp_path: Path) -> None:
    """runsvdir can start runsv after undo has begun, so the state directory appears late. The
    waiter polls for it, and then waits for runsv to exit before the cleanup (ADR-013, Decision 26).
    Without the poll, the wait is skipped and the cleanup runs under a live runsv."""
    clean = _host(tmp_path / "clean")
    _setup(clean)
    _service(clean, "alice")
    _undo(clean)
    expected = _content(_snapshot(clean))

    root = _host(tmp_path / "late")
    _setup(root)
    _service(root, "alice")
    for target in supervise_links("alice").values():
        shutil.rmtree(root / target.lstrip("/"))
    readers: list[int] = []

    def runsv_starts(call: int) -> None:
        if call == 1:
            _runsv_state(root, "alice")
            main = root / supervise_links("alice")["etc/sv/ginn-alice/supervise"].lstrip("/")
            readers.append(os.open(main / "ok", os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC))
        if call == 3:
            for fd in readers:
                os.close(fd)
            readers.clear()

    timer = _Time(on_sleep=runsv_starts)
    try:
        result = _undo(root, wait=None, timer=timer)
    finally:
        for fd in readers:
            os.close(fd)
    assert result.left == ()
    assert timer.calls == [SERVICE_POLL_SECONDS] * 3
    assert not (root / RECORD).exists()
    assert _content(_snapshot(root)) == expected


@pytest.mark.fr("FR-003")
def test_a_runsv_state_that_never_appears_costs_the_appear_window_and_undo_completes(
    tmp_path: Path,
) -> None:
    """When runsv never starts, the waiter polls for STATE_APPEAR_SECONDS and then goes on."""
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    for target in supervise_links("alice").values():
        shutil.rmtree(root / target.lstrip("/"))

    timer = _Time()
    result = _undo(root, wait=None, timer=timer)
    assert result.left == ()
    assert not (root / RECORD).exists()
    assert timer.now == pytest.approx(STATE_APPEAR_SECONDS, abs=SERVICE_POLL_SECONDS)


@pytest.mark.fr("FR-003")
def test_without_a_supervise_link_the_waiter_does_not_poll_for_a_state_directory(
    tmp_path: Path,
) -> None:
    """A service that was never linked has no state to wait for, so undo does not stall."""
    root = _host(tmp_path)
    _setup(root)
    _service(root, "alice")
    os.unlink(root / "etc/sv/ginn-alice/supervise")
    for target in supervise_links("alice").values():
        shutil.rmtree(root / target.lstrip("/"))

    timer = _Time()
    result = _undo(root, wait=None, timer=timer)
    assert result.left == ()
    assert timer.calls == []
