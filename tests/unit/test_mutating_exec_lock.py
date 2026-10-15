# SPDX-License-Identifier: Apache-2.0
"""The exec lock check and the orphan marker at the choke point (FR-123, FR-124, ADR-014).

A mutating exec holds `<runtime root>/exec/<realm>.lock` while it runs. `mutating()` refuses a user
operation on that realm while the lock is held, names the exec, and writes nothing. The operations
that ADR-014 exempts run regardless. A token kill that could not finish leaves `<realm>.orphan`,
which the next operation audits once and removes. These tests hold the lock with flock in this
process. The check opens its own descriptor, and flock conflicts between two open file descriptions
even in one process.
"""

from __future__ import annotations

import errno
import fcntl
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ginn.domain import RealmName
from ginn.errors import GinnError
from ginn.ops import _mutating
from ginn.ops._mutating import OpEnv, mutating, read_orphans
from ginn.services._exec_lock import (
    exec_lock_busy,
    exec_lock_dir,
    exec_lock_path,
    exec_orphan_path,
)
from ginn.store.audit import AuditEntry
from ginn.store.paths import OwnedPaths
from tests.fakes.clock import FakeClock

USER = "tester"
ARCH = RealmName("arch")
EXEC_HEX = "0123456789abcdef0123456789abcdef"
ORPHAN_OP = "exec-orphan"
EXITED_PID = 2147483646  # no process has this pid, so an os.kill(pid, 0) finds nothing
MARKER_TEXT = f"token {EXEC_HEX}\ntime 1700000000\n"


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    """Owned Paths rooted in tmp_path. Every XDG variable is pinned so no real host path is used."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    yield resolved


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A host root under tmp_path, outside the Owned Paths, so ginn's own writes are not seen."""
    root = tmp_path / "host"
    for scope in ("etc", "var/db/xbps", "var/service", "usr/local", "opt"):
        (root / scope).mkdir(parents=True)
    (root / "etc" / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    return root


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def env(paths: OwnedPaths, host: Path, clock: FakeClock) -> OpEnv:
    return OpEnv.for_paths(paths, USER, root_dir=host, clock=clock)


def _holder_line(run: int) -> str:
    """The holder line that ginn-exec-run writes after its fork: `run <pid> podman <pid>`."""
    return f"run {run} podman 4321"


@contextmanager
def _exec_running(paths: OwnedPaths, holder: str | None) -> Iterator[None]:
    """Hold the realm's exec lock as a running mutating exec does, with the holder line written."""
    exec_lock_dir(paths).mkdir(mode=0o700, exist_ok=True)
    fd = os.open(exec_lock_path(paths, ARCH), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if holder is not None:
            os.pwrite(fd, holder.encode("utf-8"), 0)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_marker(paths: OwnedPaths, text: str) -> Path:
    """Write a realm's orphan marker as the token kill would, owned by the test's user."""
    exec_lock_dir(paths).mkdir(mode=0o700, exist_ok=True)
    marker = exec_orphan_path(paths, ARCH)
    marker.write_text(text, encoding="utf-8")
    return marker


def _orphans(env: OpEnv) -> list[AuditEntry]:
    return [entry for entry in env.audit.read() if entry.op == ORPHAN_OP]


@pytest.mark.fr("FR-123")
def test_user_operation_is_refused_while_an_exec_holds_the_lock(
    paths: OwnedPaths, env: OpEnv
) -> None:
    ran: list[bool] = []
    with _exec_running(paths, _holder_line(os.getpid())):
        with pytest.raises(GinnError) as refused:
            with mutating(env, "install", ARCH):
                ran.append(True)

    error = refused.value
    assert error.code == "GINN-E-REALM-BUSY"
    assert error.exit_code == 75
    assert f"ginn-exec-run pid {os.getpid()}, podman pid 4321" in error.message
    assert ran == []
    assert list(env.audit.read()) == []
    assert env.journal.pending() == []


@pytest.mark.fr("FR-123")
def test_user_operation_is_allowed_after_the_exec_lock_is_released(
    paths: OwnedPaths, env: OpEnv
) -> None:
    with _exec_running(paths, _holder_line(os.getpid())):
        pass
    with mutating(env, "install", ARCH):
        pass
    outcomes = [(entry.op, entry.outcome) for entry in env.audit.read(realm=ARCH)]
    assert outcomes == [("install", "ok")]


@pytest.mark.fr("FR-123")
@pytest.mark.parametrize(
    "holder",
    ["", f"run {EXITED_PID} podman 4321", "unrecognised text"],
    ids=["empty", "run-pid-gone", "unparsed"],
)
def test_a_refusal_says_the_holder_is_not_yet_recorded_when_the_line_is_empty_or_stale(
    paths: OwnedPaths, env: OpEnv, holder: str
) -> None:
    with _exec_running(paths, holder):
        with pytest.raises(GinnError) as refused:
            with mutating(env, "install", ARCH):
                pass
    assert refused.value.code == "GINN-E-REALM-BUSY"
    assert "holder not yet recorded" in refused.value.message


@pytest.mark.fr("FR-123")
@pytest.mark.parametrize("make_dir", [False, True], ids=["no-exec-dir", "exec-dir-no-lock"])
def test_a_missing_lock_file_is_not_busy_and_nothing_is_created(
    paths: OwnedPaths, env: OpEnv, make_dir: bool
) -> None:
    if make_dir:
        exec_lock_dir(paths).mkdir(mode=0o700)
    with mutating(env, "install", ARCH):
        pass
    assert not exec_lock_path(paths, ARCH).exists()
    assert exec_lock_dir(paths).exists() is make_dir
    assert not exec_orphan_path(paths, ARCH).exists()


@pytest.mark.fr("FR-123")
@pytest.mark.parametrize("op", ["start", "stop", "service-exit", "crash", "idle-stop"])
def test_exempt_operations_run_while_an_exec_holds_the_lock(
    paths: OwnedPaths, env: OpEnv, op: str
) -> None:
    ran: list[str] = []
    with _exec_running(paths, _holder_line(os.getpid())):
        with mutating(env, op, ARCH):
            ran.append(op)
    assert ran == [op]


@pytest.mark.fr("FR-123")
def test_an_exec_that_starts_after_the_first_check_is_refused_under_the_realm_lock(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def busy_from_the_second_check(paths_: OwnedPaths, name: RealmName) -> bool:
        calls.append(1)
        return len(calls) >= 2

    monkeypatch.setattr(_mutating, "exec_lock_busy", busy_from_the_second_check)
    ran: list[bool] = []
    with pytest.raises(GinnError) as refused:
        with mutating(env, "install", ARCH):
            ran.append(True)

    assert refused.value.code == "GINN-E-REALM-BUSY"
    assert ran == []
    assert len(calls) == 2


@pytest.mark.fr("FR-124")
@pytest.mark.parametrize(
    ("marker", "pids", "unknown", "join_failed", "kill_ran"),
    [
        (f"{MARKER_TEXT}pid 111\npid 222\n", [111, 222], [], False, True),
        (f"{MARKER_TEXT}kill not run\n", [], [], False, False),
        (f"{MARKER_TEXT}unknown 333\n", [], [333], False, True),
        (f"{MARKER_TEXT}unknown join\n", [], [], True, True),
    ],
    ids=["survivors", "kill-not-run", "unknown", "unknown-join"],
)
def test_an_orphan_marker_is_audited_once_and_removed(
    paths: OwnedPaths,
    env: OpEnv,
    marker: str,
    pids: list[int],
    unknown: list[int],
    join_failed: bool,
    kill_ran: bool,
) -> None:
    _write_marker(paths, marker)
    with mutating(env, "install", ARCH):
        pass

    orphans = _orphans(env)
    assert len(orphans) == 1
    entry = orphans[0]
    assert entry.realm == ARCH.value
    assert entry.user == USER
    assert entry.error_code == "GINN-W-RELAY-ORPHAN"
    assert entry.params == {
        "token": EXEC_HEX,
        "pids": pids,
        "unknown": unknown,
        "join_failed": join_failed,
        "kill_ran": kill_ran,
        "truncated": False,
    }
    assert entry.ts is not None and entry.ts.startswith("2023-11-14T22:13:20")
    assert not exec_orphan_path(paths, ARCH).exists()

    with mutating(env, "install", ARCH):
        pass
    assert len(_orphans(env)) == 1


@pytest.mark.fr("FR-124")
def test_an_orphan_marker_waits_while_an_exec_holds_the_lock(paths: OwnedPaths, env: OpEnv) -> None:
    marker = _write_marker(paths, f"{MARKER_TEXT}pid 111\n")
    with _exec_running(paths, _holder_line(os.getpid())):
        with mutating(env, "stop", ARCH):
            pass
        assert marker.exists()
        assert _orphans(env) == []

    with mutating(env, "install", ARCH):
        pass
    assert not marker.exists()
    assert len(_orphans(env)) == 1


@pytest.mark.fr("FR-124")
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_marker_that_is_not_a_regular_file_is_never_followed_or_read(
    paths: OwnedPaths, env: OpEnv, tmp_path: Path, kind: str
) -> None:
    marker = exec_orphan_path(paths, ARCH)
    exec_lock_dir(paths).mkdir(mode=0o700, exist_ok=True)
    target = tmp_path / "elsewhere.txt"
    target.write_text(f"{MARKER_TEXT}pid 999\n", encoding="utf-8")
    if kind == "symlink":
        marker.symlink_to(target)
    else:
        marker.mkdir()

    with mutating(env, "install", ARCH):
        pass

    assert _orphans(env) == []
    assert target.read_text(encoding="utf-8") == f"{MARKER_TEXT}pid 999\n"
    if kind == "symlink":
        assert marker.is_symlink()
    else:
        assert marker.is_dir()


@pytest.mark.fr("FR-123")
def test_a_symlinked_exec_lock_is_a_ginn_error_and_is_not_followed(
    paths: OwnedPaths, env: OpEnv, tmp_path: Path
) -> None:
    exec_lock_dir(paths).mkdir(mode=0o700, exist_ok=True)
    target = tmp_path / "elsewhere.lock"
    target.write_text(_holder_line(os.getpid()), encoding="utf-8")
    lock = exec_lock_path(paths, ARCH)
    lock.symlink_to(target)

    with pytest.raises(GinnError) as refused:
        exec_lock_busy(paths, ARCH)
    assert refused.value.code == "GINN-E-INTERNAL"
    assert refused.value.exit_code == 70
    assert str(lock) in refused.value.message
    assert "symbolic link, not a regular file" in refused.value.message

    ran: list[bool] = []
    with pytest.raises(GinnError) as blocked:
        with mutating(env, "install", ARCH):
            ran.append(True)
    assert blocked.value.code == "GINN-E-INTERNAL"
    assert ran == []
    assert lock.is_symlink()
    assert target.read_text(encoding="utf-8") == _holder_line(os.getpid())


@pytest.mark.fr("FR-123")
def test_a_directory_at_the_exec_lock_path_is_a_ginn_error(paths: OwnedPaths) -> None:
    exec_lock_dir(paths).mkdir(mode=0o700, exist_ok=True)
    lock = exec_lock_path(paths, ARCH)
    lock.mkdir()

    with pytest.raises(GinnError) as refused:
        exec_lock_busy(paths, ARCH)
    assert refused.value.code == "GINN-E-INTERNAL"
    assert str(lock) in refused.value.message
    assert "is not a regular file" in refused.value.message
    assert lock.is_dir()


@pytest.mark.fr("FR-124")
def test_a_marker_longer_than_the_read_bound_is_read_only_up_to_the_bound(
    paths: OwnedPaths,
) -> None:
    _write_marker(paths, MARKER_TEXT + "pid 7\n" * (_mutating.ORPHAN_MAX_BYTES // 6 + 10))

    records = read_orphans(paths, ARCH)

    assert len(records) == 1
    assert records[0].truncated is True
    assert records[0].token == EXEC_HEX


@pytest.mark.fr("FR-123")
def test_exec_lock_busy_refuses_a_fifo_without_waiting_for_a_writer(paths: OwnedPaths) -> None:
    exec_lock_dir(paths).mkdir(mode=0o700, exist_ok=True)
    os.mkfifo(exec_lock_path(paths, ARCH))

    with pytest.raises(GinnError) as refused:
        _within(5, lambda: exec_lock_busy(paths, ARCH))
    assert refused.value.code == "GINN-E-INTERNAL"
    assert "is not a regular file" in refused.value.message


def _faulty_lock(paths: OwnedPaths, kind: str, tmp_path: Path) -> Path:
    """Put a path that is not a regular lock file where the realm's exec lock belongs."""
    exec_lock_dir(paths).mkdir(mode=0o700, exist_ok=True)
    lock = exec_lock_path(paths, ARCH)
    if kind == "symlink":
        target = tmp_path / "elsewhere.lock"
        target.write_text(_holder_line(os.getpid()), encoding="utf-8")
        lock.symlink_to(target)
    elif kind == "directory":
        lock.mkdir()
    else:
        os.mkfifo(lock)
    return lock


def _within(seconds: float, call: Callable[[], object]) -> object:
    """Run `call` in a thread and fail if it has not returned in `seconds`.

    A FIFO that an open waits on would hang the test forever, so the check is a timeout (ADR-014).
    """
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = call()
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "the call did not return: it waited on the lock path"
    error = outcome.get("error")
    if isinstance(error, Exception):
        raise error
    return outcome.get("value")


@pytest.mark.fr("FR-123")
@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_a_faulty_lock_path_refuses_a_user_operation_without_hanging(
    paths: OwnedPaths, env: OpEnv, tmp_path: Path, kind: str
) -> None:
    _faulty_lock(paths, kind, tmp_path)
    ran: list[bool] = []

    def attempt() -> None:
        with mutating(env, "install", ARCH):
            ran.append(True)

    with pytest.raises(GinnError) as refused:
        _within(5, attempt)
    assert refused.value.code == "GINN-E-INTERNAL"
    assert str(exec_lock_path(paths, ARCH)) in refused.value.message
    assert ran == []
    assert env.journal.pending() == []


@pytest.mark.fr("FR-123")
@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_an_exempt_operation_goes_on_past_a_faulty_lock_path_and_leaves_the_marker(
    paths: OwnedPaths, env: OpEnv, tmp_path: Path, kind: str
) -> None:
    _faulty_lock(paths, kind, tmp_path)
    marker = _write_marker(paths, MARKER_TEXT + "pid 111\n")
    ran: list[str] = []

    def attempt() -> None:
        with mutating(env, "stop", ARCH):
            ran.append("stop")

    _within(5, attempt)
    assert ran == ["stop"]
    assert _orphans(env) == []
    assert marker.exists()


@pytest.mark.fr("FR-124")
def test_two_records_in_one_marker_are_audited_one_entry_each(
    paths: OwnedPaths, env: OpEnv
) -> None:
    other = "1" * 32
    _write_marker(paths, MARKER_TEXT + f"pid 111\ntoken {other}\ntime 1700000100\nkill not run\n")

    with mutating(env, "install", ARCH):
        pass

    assert [entry.params["token"] for entry in _orphans(env)] == [EXEC_HEX, other]
    assert not exec_orphan_path(paths, ARCH).exists()


@pytest.mark.fr("FR-124")
def test_a_record_the_audit_log_already_holds_is_not_audited_again(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _write_marker(paths, MARKER_TEXT + "pid 111\n")
    with mutating(env, "install", ARCH):
        pass
    assert len(_orphans(env)) == 1

    # A crash after the append and before the removal leaves the same record behind.
    _write_marker(paths, MARKER_TEXT + "pid 111\n")
    with mutating(env, "install", ARCH):
        pass

    assert len(_orphans(env)) == 1
    assert not exec_orphan_path(paths, ARCH).exists()


@pytest.mark.fr("FR-124")
def test_a_settled_record_is_a_warning_on_the_operation(paths: OwnedPaths, env: OpEnv) -> None:
    _write_marker(paths, MARKER_TEXT + "pid 111\n")

    with mutating(env, "install", ARCH) as mutation:
        pass

    assert [w.code for w in mutation.warnings] == ["GINN-W-RELAY-ORPHAN"]
    assert mutation.warnings[0].message == (
        "a command in realm 'arch' left processes that the token kill could not stop: "
        "pids 111 survived the token kill"
    )


@pytest.mark.fr("FR-124")
def test_an_audit_failure_keeps_the_marker_and_never_fails_the_operation(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _write_marker(paths, MARKER_TEXT + "pid 111\n")
    real_append = env.audit.append

    def append_but_not_orphans(entry: AuditEntry) -> AuditEntry:
        if entry.op == ORPHAN_OP:
            raise OSError(errno.EIO, "the audit log is not writable")
        return real_append(entry)

    monkeypatch.setattr(env.audit, "append", append_but_not_orphans)
    ran: list[bool] = []

    with mutating(env, "install", ARCH) as mutation:
        ran.append(True)

    assert ran == [True]
    assert [w.code for w in mutation.warnings] == ["GINN-W-RELAY-ORPHAN", "GINN-W-ORPHAN-UNSETTLED"]
    assert "the audit log is not writable" in mutation.warnings[1].message
    assert marker.exists()


@pytest.mark.fr("FR-124")
@pytest.mark.parametrize("tail", ["pid 1", "token 0123"], ids=["torn-pid", "torn-token"])
def test_a_torn_last_line_is_not_read(paths: OwnedPaths, tail: str) -> None:
    """A crash can leave the last line without its newline. That line is dropped, and the records
    before it are read as they are (AuditLog._read_file does the same for the audit log)."""
    _write_marker(paths, MARKER_TEXT + f"pid 111\n{tail}")

    records = read_orphans(paths, ARCH)

    assert len(records) == 1
    assert records[0].token == EXEC_HEX
    assert records[0].pids == (111,)


@pytest.mark.fr("FR-124")
def test_an_oversize_marker_is_audited_within_the_bound_and_kept(
    paths: OwnedPaths, env: OpEnv
) -> None:
    """Records past the read bound are not read, so the marker is not removed: removing it would
    lose them. The records read are audited once, and the operation says the marker is kept."""
    marker = _write_marker(paths, MARKER_TEXT + "pid 7\n" * (_mutating.ORPHAN_MAX_BYTES // 6 + 10))

    with mutating(env, "install", ARCH) as mutation:
        pass

    assert [w.code for w in mutation.warnings] == ["GINN-W-RELAY-ORPHAN", "GINN-W-ORPHAN-UNSETTLED"]
    assert "later records are not read" in mutation.warnings[1].message
    assert marker.exists()
    orphans = _orphans(env)
    assert len(orphans) == 1
    assert orphans[0].params["token"] == EXEC_HEX
    assert orphans[0].params["truncated"] is True

    # The next operation finds the same record already audited, so it audits nothing new.
    with mutating(env, "install", ARCH):
        pass
    assert len(_orphans(env)) == 1
    assert marker.exists()


@pytest.mark.fr("FR-124")
def test_an_unknown_pid_is_named_apart_from_a_survivor_and_is_never_a_no_pid_record() -> None:
    """A record with only `unknown` lines says so: the kill left those pids alone, it found none."""
    (only,) = _mutating.parse_orphans(f"{MARKER_TEXT}unknown 333\n".encode())
    assert (only.pids, only.unknown, only.kill_ran) == ((), (333,), True)
    assert only.pids_text() == "unknown pids (token not readable): 333"
    (mixed,) = _mutating.parse_orphans(f"{MARKER_TEXT}pid 111\nunknown 333\n".encode())
    assert mixed.pids_text() == (
        "pids 111 survived the token kill; unknown pids (token not readable): 333"
    )
    (joined,) = _mutating.parse_orphans(f"{MARKER_TEXT}unknown join\n".encode())
    assert joined.join_failed and joined.pids_text() == "processes unknown (token not readable)"
