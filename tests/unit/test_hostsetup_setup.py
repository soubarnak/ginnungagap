# SPDX-License-Identifier: Apache-2.0
"""Host Setup for one user under a root prefix: root check, rerun, lock, chain, records and resume.

Covers FR-001 (AC-2, the root check; the subid, service and link writes; the Void service chain;
the global lock; the parent checks), FR-002 (AC-1, a rerun that changes no byte or mtime and reports
"already configured"; mode drift is repaired), FR-003 (the pre-images that undo needs), DR-009 (the
per-user record and the shared record /etc/ginn/shared.json, schema 3), NFR-030 (a crash after any
step resumes from the stored pre-images) and NFR-040 (refusals carry a catalogue code).

Each test runs under tmp_path, with a root_dir that mirrors the host layout. None of them touches
the real /etc, /var, /run or /sys. The clock, the user lookup, the owner check and the lock timeout
are injected, and the root check takes an explicit euid. The umask is 022 for every test, because
setup refuses a parent that is writable by group or others. On the Void fixture, /var/service is a
symbolic link to a relative target, so the tests never reach it through the operating system: they
read it with readlink or check it with lstat only.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import pwd
import shutil
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ginn.cli.main import run as cli_run
from ginn.errors import GinnError
from ginn.hostsetup import record as rec
from ginn.hostsetup import runit_system, subids
from ginn.hostsetup import setup as setup_mod
from ginn.hostsetup.setup import TMP_DIR, SetupResult, UserInfo, run_setup

USERS: dict[str, UserInfo] = {
    "alice": UserInfo(uid=1000, gid=1000, home="/home/alice"),
    "bob": UserInfo(uid=1001, gid=1001, home="/home/bob"),
}
NAMED_USERS: dict[str, UserInfo] = {
    "ginn": UserInfo(uid=1001, gid=1001, home="/home/ginn"),
    "sv": UserInfo(uid=1002, gid=1002, home="/home/sv"),
    "run": UserInfo(uid=1003, gid=1003, home="/home/run"),
}
"""Accounts whose names equal a path component of the host layout (ADR-013, Decision 12).

`ginn` has the uid the task names, which bob also has. No test sets up both in one root.
"""
NOW = 1_800_000_000.0
BOOT = "6f1d2c3b-0000-4000-8000-000000000001"
RECORD = "etc/ginn/setup-alice.json"
BOB_RECORD = "etc/ginn/setup-bob.json"
SHARED = rec.SHARED_RELPATH
RUN = "etc/sv/ginn-alice/run"
FINISH = "etc/sv/ginn-alice/finish"
LOG_RUN = "etc/sv/ginn-alice/log/run"
LINK = "var/service/ginn-alice"
RESOLVED_LINK = "etc/runit/runsvdir/default/ginn-alice"
SUBUID = "etc/subuid"
SUBGID = "etc/subgid"
SUBTREE = "sys/fs/cgroup/cgroup.subtree_control"
LOCK = "run/lock/ginn-host-setup.lock"
BOOT_FILE = "proc/sys/kernel/random/boot_id"
ALICE_RANGE = subids.SubIdEntry("alice", 100000, 524288)
SHARED_SERVICE = (SUBTREE, "run/ginn", "sys/fs/cgroup/ginn")
"""The shared service-written paths, sorted as the shared record lists them."""
FIRST_STEPS = [
    "tmpdir",
    "pending",
    "dirs",
    "subuid",
    "subgid",
    "subids",
    "sv",
    "symlink",
    "complete",
]
SECOND_STEPS = [
    "shared",
    "pending",
    "dirs",
    "subuid",
    "subgid",
    "subids",
    "sv",
    "symlink",
    "complete",
]
OLD_REASON = "written by a ginn build before F8a/F9a; remove it by hand after checking"


class _Crash(Exception):
    """Stands in for a crash, such as SIGKILL or power loss, right after one setup step."""


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


def _host(tmp_path: Path, *, boot: bool = True) -> Path:
    """A temporary root that mirrors the plain-directory host layout that setup reads or writes."""
    root = tmp_path / "host"
    for rel in ("etc", "etc/sv", "var/service", "run/lock", "sys/fs/cgroup"):
        (root / rel).mkdir(parents=True)
    _common(root, boot=boot)
    return root


def _void_host(tmp_path: Path) -> Path:
    """A temporary root with the Void /var/service chain, as checked on the reference host.

    /var/service -> ../run/runit/runsvdir/current -> /etc/runit/runsvdir/current -> default, and
    /etc/runit/runsvdir/default is a real directory.
    """
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
    _common(root, boot=True)
    os.symlink("../run/runit/runsvdir/current", root / "var/service")
    os.symlink("/etc/runit/runsvdir/current", root / "run/runit/runsvdir/current")
    os.symlink("default", root / "etc/runit/runsvdir/current")
    return root


def _common(root: Path, *, boot: bool) -> None:
    (root / LOCK).write_bytes(b"")
    os.chmod(root / LOCK, 0o600)
    (root / SUBTREE).write_text("cpu io memory\n", encoding="utf-8")
    if boot:
        (root / BOOT_FILE).parent.mkdir(parents=True)
        (root / BOOT_FILE).write_text(BOOT + "\n", encoding="ascii")


def _setup(
    root: Path,
    user: str = "alice",
    *,
    clock: float = NOW,
    fault: Callable[[str], None] | None = None,
    owner_of: Callable[[Path], int] = _root_owned,
    lock_timeout: float = 5.0,
) -> SetupResult:
    return run_setup(
        user,
        root_dir=root,
        euid=0,
        lookup_user=_account,
        clock=lambda: clock,
        fault=fault,
        owner_of=owner_of,
        lock_timeout=lock_timeout,
    )


def _account(name: str) -> UserInfo:
    """The account for `name` from USERS or NAMED_USERS. KeyError when unknown, as getpwnam does."""
    if name in NAMED_USERS:
        return NAMED_USERS[name]
    return USERS[name]


def _passwd(name: str) -> Any:
    """Stands in for pwd.getpwnam with the fields that Host Setup reads. KeyError when unknown."""
    info = _account(name)
    return SimpleNamespace(pw_uid=info.uid, pw_gid=info.gid, pw_dir=info.home)


def _rendered(user: str = "alice") -> dict[str, str]:
    info = _account(user)
    home = info.home
    xdg = {
        "XDG_STATE_HOME": f"{home}/.local/state",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_CONFIG_HOME": f"{home}/.config",
    }
    return runit_system.render(user, info.uid, info.gid, home, xdg)


def _record(root: Path, user: str = "alice") -> rec.HostSetupRecord:
    loaded = rec.load(root / rec.relpath(user))
    assert loaded is not None
    return loaded


def _shared(root: Path) -> rec.SharedRecord:
    loaded = rec.load_shared(root / SHARED)
    assert loaded is not None
    return loaded


def _entry(record: rec.HostSetupRecord | rec.SharedRecord, path: str) -> rec.PathEntry:
    for entry in (*record.paths, *record.service_paths):
        if entry.path == path:
            return entry
    raise AssertionError(f"{path} is not in the record")


def _pre_images(record: rec.HostSetupRecord | rec.SharedRecord) -> dict[str, rec.Image]:
    return {entry.path: entry.pre for entry in (*record.paths, *record.service_paths)}


def _snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every entry under root with its kind, mode or target, file digest and mtime_ns.

    Symbolic links are recorded with lstat and never followed, so the snapshot is safe on the
    Void fixture.
    """
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
    """The snapshot without modification times, so that a rerun can be compared by content."""
    return {rel: entry[:-1] for rel, entry in snapshot.items()}


def _content_without(root: Path, *rels: str) -> dict[str, tuple[Any, ...]]:
    skip = set(rels)
    return {path: entry for path, entry in _content(_snapshot(root)).items() if path not in skip}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.fr("FR-001")
def test_a_non_root_run_is_refused_and_the_host_is_not_touched(tmp_path: Path) -> None:
    root = _host(tmp_path)
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        run_setup("alice", root_dir=root, euid=1000, lookup_user=USERS.__getitem__)
    assert caught.value.code == "GINN-E-SETUP-ROOT"
    assert caught.value.exit_code == 77
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_the_cli_refuses_a_non_root_run_with_exit_77_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _host(tmp_path)
    before = _snapshot(root)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    status = cli_run(["host-setup", "--user", "alice", "--root-dir", str(root)])
    assert status == 77
    assert "GINN-E-SETUP-ROOT" in capsys.readouterr().err
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_a_non_root_run_without_a_user_is_the_root_error_and_not_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _host(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    status = cli_run(["host-setup", "--root-dir", str(root)])
    assert status == 77
    assert "GINN-E-SETUP-ROOT" in capsys.readouterr().err


@pytest.mark.fr("FR-001")
def test_a_root_run_without_a_user_is_a_usage_error_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _host(tmp_path)
    before = _snapshot(root)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    status = cli_run(["host-setup", "--root-dir", str(root)])
    assert status == 2
    err = capsys.readouterr().err
    assert "GINN-E-USAGE" in err
    assert "--user" in err
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_a_first_run_writes_the_subid_ranges_the_service_and_the_link(tmp_path: Path) -> None:
    root = _host(tmp_path)
    result = _setup(root)
    assert result.status == "configured"
    assert ALICE_RANGE.render() in (root / SUBUID).read_text(encoding="utf-8").splitlines()
    assert ALICE_RANGE.render() in (root / SUBGID).read_text(encoding="utf-8").splitlines()
    for rel, text in _rendered("alice").items():
        assert (root / rel).read_text(encoding="utf-8") == text
        assert stat.S_IMODE(os.stat(root / rel).st_mode) == 0o755
    assert os.readlink(root / LINK) == "/etc/sv/ginn-alice"
    record = _record(root)
    assert record.status == "complete"
    assert record.subids == rec.SubIds(uid=ALICE_RANGE, gid=ALICE_RANGE)
    assert _entry(record, SUBUID).kind == "append-lines"
    assert _entry(record, SUBGID).kind == "append-lines"


@pytest.mark.fr("FR-001")
def test_the_scripts_keep_host_paths_and_setup_does_not_need_a_runtime_dir(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    text = (root / RUN).read_text(encoding="utf-8")
    assert "RUNTIME_DIR=/run/ginn/1000" in text
    assert str(root) not in text
    assert not (root / "run" / "ginn").exists()


@pytest.mark.fr("DR-009")
def test_the_records_list_every_path_that_differs_after_the_run(tmp_path: Path) -> None:
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root)
    after = _content(_snapshot(root))
    differing = {rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel)}
    record = _record(root)
    shared = _shared(root)
    # The fixture creates the global lock file, and it is never recorded (ADR-013, Decision 11).
    assert differing == {entry.path for entry in record.paths} | {
        entry.path for entry in shared.paths
    }
    service = {entry.path for entry in (*record.service_paths, *shared.service_paths)}
    assert differing.isdisjoint(service)
    assert _entry(record, RECORD).pre == rec.Image("absent")
    assert _entry(record, RECORD).post == rec.Image("self")
    assert _entry(shared, SHARED).pre == rec.Image("absent")


@pytest.mark.fr("DR-009")
def test_the_service_written_paths_carry_pre_images_and_no_post_image(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    shared = _shared(root)
    subtree = _entry(shared, SUBTREE)
    assert subtree.kind == "file"
    assert subtree.pre.data == b"cpu io memory\n"
    assert subtree.post is None
    ginn_dir = _entry(shared, "sys/fs/cgroup/ginn")
    assert ginn_dir.pre == rec.Image("absent")
    assert ginn_dir.post is None
    assert not (root / "sys/fs/cgroup/ginn").exists()


@pytest.mark.fr("DR-009")
def test_the_service_written_lists_split_into_the_shared_record_and_the_user_record(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    record = _record(root)
    assert [entry.path for entry in record.service_paths] == [
        "run/ginn/1000",
        "sys/fs/cgroup/ginn/1000",
        "sys/fs/cgroup/ginn/1000/supervisor",
    ]
    assert [entry.path for entry in _shared(root).service_paths] == sorted(SHARED_SERVICE)
    assert all(entry.post is None for entry in record.service_paths)
    for path in ("run/ginn/1000", "sys/fs/cgroup/ginn/1000/supervisor"):
        assert _entry(record, path).pre == rec.Image("absent")


@pytest.mark.fr("DR-009")
def test_the_shared_entries_are_marked_and_the_per_user_entries_are_not(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    record = _record(root)
    shared = _shared(root)
    shared_paths = {entry.path for entry in (*shared.paths, *shared.service_paths)}
    assert shared_paths == {"etc/ginn", SHARED, SUBTREE, "sys/fs/cgroup/ginn", "run/ginn"}
    assert all(entry.shared for entry in (*shared.paths, *shared.service_paths))
    per_user = (*record.paths, *record.service_paths)
    per_user_shared = {entry.path for entry in per_user if entry.shared}
    assert per_user_shared == {SUBUID, SUBGID}
    assert _entry(record, "etc/sv/ginn-alice").shared is False
    assert _entry(record, RECORD).shared is False


@pytest.mark.fr("DR-009")
def test_the_records_carry_the_boot_id_read_when_the_pre_images_were_captured(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    assert _record(root).boot_id == BOOT
    assert _shared(root).boot_id == BOOT
    assert json.loads((root / RECORD).read_text(encoding="utf-8"))["boot_id"] == BOOT
    assert json.loads((root / SHARED).read_text(encoding="utf-8"))["boot_id"] == BOOT


@pytest.mark.fr("DR-009")
def test_without_a_boot_id_file_the_records_say_null(tmp_path: Path) -> None:
    root = _host(tmp_path, boot=False)
    _setup(root)
    assert _record(root).boot_id is None
    assert _shared(root).boot_id is None
    assert json.loads((root / RECORD).read_text(encoding="utf-8"))["boot_id"] is None


@pytest.mark.fr("DR-009")
def test_the_record_files_are_root_only_mode_0600(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    assert stat.S_IMODE(os.stat(root / RECORD).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(root / SHARED).st_mode) == 0o600


@pytest.mark.fr("DR-009")
def test_etc_ginn_is_a_shared_directory_in_shared_json_with_its_pre_image_absent(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    entry = _entry(_shared(root), "etc/ginn")
    assert entry.kind == "dir"
    assert entry.pre == rec.Image("absent")
    assert entry.shared is True
    assert entry.post is None
    assert "etc/ginn" not in {item.path for item in _record(root).paths}
    assert not (root / TMP_DIR).exists()


@pytest.mark.fr("DR-009")
def test_the_first_setup_writes_both_records_into_the_temporary_dir_before_the_rename(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    seen: list[str] = []

    def hook(step: str) -> None:
        seen.append(step)
        if step == "tmpdir":
            tmp = root / TMP_DIR
            names = sorted(path.name for path in tmp.iterdir())
            assert names == ["setup-alice.json", "shared.json"]
            assert not (root / "etc/ginn").exists()

    _setup(root, fault=hook)
    assert seen == FIRST_STEPS


@pytest.mark.fr("FR-001")
@pytest.mark.fr("DR-009")
def test_etc_ginn_never_exists_without_shared_json_at_any_step_of_the_first_setup(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)

    def watch(step: str) -> None:
        if (root / "etc/ginn").exists():
            assert (root / SHARED).is_file(), f"etc/ginn exists without shared.json after {step}"

    _setup(root, fault=watch)
    assert _record(root).status == "complete"


@pytest.mark.fr("FR-002")
def test_a_second_run_reports_already_configured_and_changes_no_byte_or_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _host(tmp_path)
    _setup(root)
    before = _snapshot(root)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", _passwd)
    monkeypatch.setattr(setup_mod, "_owner_uid", _root_owned)
    status = cli_run(["host-setup", "--user", "alice", "--root-dir", str(root)])
    assert status == 0
    assert "already configured" in capsys.readouterr().out
    assert _snapshot(root) == before


@pytest.mark.fr("FR-002")
def test_an_already_configured_host_does_not_wait_for_a_held_lock(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    before = _snapshot(root)
    holder = os.open(root / LOCK, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result = _setup(root, clock=NOW + 60, lock_timeout=0.2)
    finally:
        os.close(holder)
    assert result.status == "already configured"
    assert _snapshot(root) == before


@pytest.mark.fr("FR-002")
def test_a_drifted_script_is_restored_and_the_pre_images_are_kept(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    pre_before = _pre_images(_record(root))
    (root / RUN).write_text("tampered\n", encoding="utf-8")
    result = _setup(root, clock=NOW + 60)
    assert result.status == "configured"
    expected = _rendered("alice")[RUN]
    assert (root / RUN).read_text(encoding="utf-8") == expected
    record = _record(root)
    assert record.status == "complete"
    assert _pre_images(record) == pre_before
    assert _entry(record, RUN).post == rec.Image("file", mode=0o755, sha256=_sha(expected))


@pytest.mark.fr("FR-002")
def test_a_mode_drift_on_a_script_is_not_already_configured_and_the_repair_restores_it(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    os.chmod(root / RUN, 0o644)
    result = _setup(root, clock=NOW + 60)
    assert result.status == "configured"
    assert stat.S_IMODE(os.stat(root / RUN).st_mode) == 0o755
    assert _entry(_record(root), RUN).post == rec.Image(
        "file", mode=0o755, sha256=_sha(_rendered("alice")[RUN])
    )


@pytest.mark.fr("FR-002")
def test_a_mode_drift_on_a_created_directory_is_repaired(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    os.chmod(root / "etc/sv/ginn-alice", 0o700)
    result = _setup(root, clock=NOW + 60)
    assert result.status == "configured"
    assert stat.S_IMODE(os.stat(root / "etc/sv/ginn-alice").st_mode) == 0o755
    assert _entry(_record(root), "etc/sv/ginn-alice").post == rec.Image("dir", mode=0o755)


@pytest.mark.fr("FR-002")
def test_a_second_users_setup_does_not_make_the_first_users_rerun_report_drift(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root, "alice")
    _setup(root, "bob")
    assert "bob:" in (root / SUBUID).read_text(encoding="utf-8")
    assert _setup(root, "alice").status == "already configured"


@pytest.mark.fr("FR-002")
@pytest.mark.fr("DR-009")
def test_a_second_setup_leaves_shared_json_byte_and_mtime_identical_and_adds_no_shared_entries(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root, "alice")
    shared_bytes = (root / SHARED).read_bytes()
    shared_mtime = os.stat(root / SHARED).st_mtime_ns
    _setup(root, "bob")
    assert (root / SHARED).read_bytes() == shared_bytes
    assert os.stat(root / SHARED).st_mtime_ns == shared_mtime
    bob = _record(root, "bob")
    shared_names = {entry.path for entry in (*_shared(root).paths, *_shared(root).service_paths)}
    bob_names = {entry.path for entry in (*bob.paths, *bob.service_paths)}
    assert not (bob_names & (shared_names - {SUBUID, SUBGID}))
    assert not {entry.path for entry in (*bob.paths, *bob.service_paths) if entry.shared} - {
        SUBUID,
        SUBGID,
    }


@pytest.mark.fr("FR-003")
@pytest.mark.fr("DR-009")
def test_shared_json_keeps_the_pre_alice_subtree_control_and_the_absent_pre_images(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root, "alice")
    # The service changes the root controllers after alice's setup, so bob must not capture them.
    (root / SUBTREE).write_text("cpu io memory cpuset\n", encoding="utf-8")
    _setup(root, "bob")
    shared = _shared(root)
    assert _entry(shared, SUBTREE).pre.data == b"cpu io memory\n"
    assert _entry(shared, "etc/ginn").pre == rec.Image("absent")
    assert _entry(shared, "sys/fs/cgroup/ginn").pre == rec.Image("absent")
    assert _entry(shared, "run/ginn").pre == rec.Image("absent")
    bob = _record(root, "bob")
    assert SUBTREE not in {entry.path for entry in (*bob.paths, *bob.service_paths)}
    assert _entry(bob, "run/ginn/1001").pre == rec.Image("absent")


@pytest.mark.fr("FR-001")
@pytest.mark.fr("DR-009")
def test_a_later_user_adds_a_missing_shared_directory_to_shared_json_with_its_pre_image_absent(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root, "alice")
    shutil.rmtree(root / "etc/sv")
    before_bytes = (root / SHARED).read_bytes()
    _setup(root, "bob")
    shared = _shared(root)
    assert (root / SHARED).read_bytes() != before_bytes
    assert _entry(shared, "etc/sv").pre == rec.Image("absent")
    assert _entry(shared, "etc/sv").shared is True
    assert (root / "etc/sv/ginn-bob").is_dir()
    bob = _record(root, "bob")
    assert "etc/sv" not in {entry.path for entry in (*bob.paths, *bob.service_paths)}
    assert "etc/sv/ginn-bob" in {entry.path for entry in bob.paths}


@pytest.mark.fr("DR-009")
def test_a_missing_shared_json_with_a_user_record_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root, "alice")
    (root / SHARED).unlink()
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root, "alice", clock=NOW + 60)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert caught.value.exit_code == 77
    assert "is missing" in caught.value.message
    assert "do not let setup recreate it" in caught.value.message
    assert _snapshot(root) == before
    with pytest.raises(GinnError) as caught:
        _setup(root, "bob", clock=NOW + 60)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert _snapshot(root) == before
    assert not (root / SHARED).exists()


@pytest.mark.fr("DR-009")
def test_a_schema_2_record_is_refused_with_the_reason_that_names_the_pre_f8a_build(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    body = json.loads((root / RECORD).read_text(encoding="utf-8"))
    body["schema"] = 2
    body.pop("kind")
    (root / RECORD).write_text(json.dumps(body), encoding="utf-8")
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root, clock=NOW + 60)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert caught.value.exit_code == 77
    assert OLD_REASON in caught.value.message
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_a_group_writable_parent_is_refused_before_any_write(tmp_path: Path) -> None:
    root = _host(tmp_path)
    os.chmod(root / "etc", 0o775)  # noqa: S103 - the test makes a parent group-writable on purpose
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert caught.value.exit_code == 77
    assert "writable by group or others (mode 0775)" in caught.value.message
    assert "correct the mode by hand" in caught.value.message
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_a_parent_that_is_not_owned_by_root_is_refused_before_any_write(tmp_path: Path) -> None:
    root = _host(tmp_path)
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root, owner_of=lambda path: 1000 if path.name == "etc" else 0)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert "is owned by uid 1000, but uid 0 must own it" in caught.value.message
    assert _snapshot(root) == before


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("DR-009")
@pytest.mark.parametrize("step", FIRST_STEPS)
def test_a_crash_at_any_step_of_the_first_setup_resumes_with_the_first_pre_images(
    tmp_path: Path, step: str
) -> None:
    clean = _host(tmp_path / "clean")
    _setup(clean)
    crashed = _host(tmp_path / "crashed")
    with pytest.raises(_Crash):
        _setup(crashed, fault=_crash_after(step))
    if step == "tmpdir":
        assert not (crashed / "etc/ginn").exists()
        names = sorted(path.name for path in (crashed / TMP_DIR).iterdir())
        assert names == ["setup-alice.json", "shared.json"]
    else:
        interrupted = _record(crashed)
        assert interrupted.status == ("complete" if step == "complete" else "pending")
        assert _pre_images(interrupted) == _pre_images(_record(clean))
        assert _pre_images(_shared(crashed)) == _pre_images(_shared(clean))
        assert not (crashed / TMP_DIR).exists()
    _setup(crashed, clock=NOW + 60)
    assert replace(_record(crashed), timestamp="") == replace(_record(clean), timestamp="")
    # A crash in the rename window commits nothing, so the resumed run captures again and stamps
    # shared.json with its own time. Its pre-images still equal the clean run's.
    assert _pre_images(_shared(crashed)) == _pre_images(_shared(clean))
    assert replace(_shared(crashed), timestamp="") == replace(_shared(clean), timestamp="")
    assert _content_without(crashed, RECORD, SHARED) == _content_without(clean, RECORD, SHARED)


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("DR-009")
@pytest.mark.parametrize("step", SECOND_STEPS)
def test_a_crash_at_any_step_of_a_second_setup_resumes_with_the_first_pre_images(
    tmp_path: Path, step: str
) -> None:
    clean = _host(tmp_path / "clean")
    _setup(clean, "alice")
    shutil.rmtree(clean / "etc/sv")
    _setup(clean, "bob")
    crashed = _host(tmp_path / "crashed")
    _setup(crashed, "alice")
    shutil.rmtree(crashed / "etc/sv")
    with pytest.raises(_Crash):
        _setup(crashed, "bob", fault=_crash_after(step))
    assert _shared(crashed) == _shared(clean)
    interrupted = rec.load(crashed / BOB_RECORD)
    if step == "shared":
        assert interrupted is None
    else:
        assert interrupted is not None
        assert interrupted.status == ("complete" if step == "complete" else "pending")
        assert _pre_images(interrupted) == _pre_images(_record(clean, "bob"))
    assert not (crashed / TMP_DIR).exists()
    _setup(crashed, "bob", clock=NOW + 60)
    assert replace(_record(crashed, "bob"), timestamp="") == replace(
        _record(clean, "bob"), timestamp=""
    )
    assert _shared(crashed) == _shared(clean)
    assert _content_without(crashed, BOB_RECORD) == _content_without(clean, BOB_RECORD)


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("DR-009")
def test_a_resumed_run_keeps_the_pre_images_the_crashed_run_had_overwritten(tmp_path: Path) -> None:
    root = _host(tmp_path)
    (root / SUBUID).write_text("bob:200000:65536\n", encoding="utf-8")
    os.chmod(root / SUBUID, 0o640)
    (root / RUN).parent.mkdir(parents=True)
    (root / RUN).write_text("old supervisor\n", encoding="utf-8")
    os.chmod(root / RUN, 0o600)
    with pytest.raises(_Crash):
        _setup(root, fault=_crash_after("sv"))
    assert (root / RUN).read_text(encoding="utf-8") == _rendered("alice")[RUN]
    _setup(root, clock=NOW + 60)
    record = _record(root)
    assert _entry(record, RUN).pre == rec.Image("file", mode=0o600, data=b"old supervisor\n")
    assert _entry(record, SUBUID).pre == rec.Image("file", mode=0o640, data=b"bob:200000:65536\n")


@pytest.mark.fr("NFR-030")
def test_a_stale_temporary_name_from_a_crash_is_removed_on_the_next_run(tmp_path: Path) -> None:
    root = _host(tmp_path)
    (root / TMP_DIR).mkdir()
    (root / TMP_DIR / ".setup-alice.json.ginn-tmp").write_text("partial", encoding="utf-8")
    (root / "etc/.subuid.ginn-tmp").write_text("junk\n", encoding="utf-8")
    _setup(root)
    assert not (root / TMP_DIR).exists()
    assert not (root / "etc/.subuid.ginn-tmp").exists()
    shared = _shared(root)
    assert TMP_DIR not in {entry.path for entry in shared.paths}
    assert _entry(shared, "etc/ginn").pre == rec.Image("absent")


@pytest.mark.fr("NFR-030")
def test_a_stale_temporary_name_of_shared_json_is_removed_by_a_later_setup(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root, "alice")
    (root / "etc/ginn/.shared.json.ginn-tmp").write_text("junk\n", encoding="utf-8")
    _setup(root, "bob")
    assert not (root / "etc/ginn/.shared.json.ginn-tmp").exists()


@pytest.mark.fr("NFR-030")
def test_a_repair_removes_a_stale_temporary_name_beside_each_path_it_writes(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    (root / "etc/sv/ginn-alice/.run.ginn-tmp").write_text("junk\n", encoding="utf-8")
    os.chmod(root / RUN, 0o644)
    _setup(root, clock=NOW + 60)
    assert not (root / "etc/sv/ginn-alice/.run.ginn-tmp").exists()
    assert stat.S_IMODE(os.stat(root / RUN).st_mode) == 0o755


@pytest.mark.fr("FR-002")
def test_a_planned_path_missing_from_the_record_gets_a_fresh_pre_image_before_it_is_written(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    record = _record(root)
    kept = tuple(entry for entry in record.paths if entry.path != FINISH)
    rec.save(root / RECORD, replace(record, paths=kept))
    (root / FINISH).write_text("stale\n", encoding="utf-8")
    os.chmod(root / FINISH, 0o644)
    result = _setup(root, clock=NOW + 60)
    assert result.status == "configured"
    merged = _entry(_record(root), FINISH)
    assert merged.pre == rec.Image("file", mode=0o644, data=b"stale\n")
    assert (root / FINISH).read_text(encoding="utf-8") == _rendered("alice")[FINISH]
    assert merged.post == rec.Image("file", mode=0o755, sha256=_sha(_rendered("alice")[FINISH]))


@pytest.mark.fr("FR-001")
def test_an_unknown_user_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    root = _host(tmp_path)
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root, user="carol")
    assert caught.value.code == "GINN-E-NO-SUCH-USER"
    assert caught.value.exit_code == 2
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_a_relative_root_dir_is_refused_as_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(GinnError) as caught:
        run_setup("alice", root_dir=Path("relative"), euid=0, lookup_user=USERS.__getitem__)
    assert caught.value.code == "GINN-E-USAGE"
    assert caught.value.exit_code == 2


@pytest.mark.fr("FR-001")
def test_a_planned_path_of_the_wrong_kind_is_refused_before_the_first_write(tmp_path: Path) -> None:
    root = _host(tmp_path)
    (root / RUN).mkdir(parents=True)
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert caught.value.exit_code == 77
    assert _snapshot(root) == before


@pytest.mark.fr("DR-009")
def test_an_unreadable_record_is_refused_and_is_never_taken_as_absent(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    (root / RECORD).write_text("{not json", encoding="utf-8")
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root, clock=NOW + 60)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_a_repair_reuses_the_recorded_subid_range_and_never_picks_another(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    (root / SUBUID).write_text("carol:1000000:524288\n", encoding="utf-8")
    _setup(root, clock=NOW + 60)
    lines = (root / SUBUID).read_text(encoding="utf-8").splitlines()
    assert lines == ["carol:1000000:524288", ALICE_RANGE.render()]
    assert _record(root).subids is not None
    assert _record(root).subids.uid == ALICE_RANGE


@pytest.mark.fr("FR-001")
def test_a_repair_refuses_when_the_recorded_range_is_no_longer_free(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    (root / SUBUID).write_text("dave:150000:65536\n", encoding="utf-8")
    before = (root / SUBUID).read_bytes()
    with pytest.raises(GinnError) as caught:
        _setup(root, clock=NOW + 60)
    assert caught.value.code == "GINN-E-SUBID-CONFLICT"
    assert caught.value.exit_code == 4
    assert (root / SUBUID).read_bytes() == before


@pytest.mark.fr("FR-001")
def test_an_existing_full_size_range_is_recorded_and_not_duplicated(tmp_path: Path) -> None:
    root = _host(tmp_path)
    for rel in (SUBUID, SUBGID):
        (root / rel).write_text("alice:300000:524288\n", encoding="utf-8")
    _setup(root)
    assert (root / SUBUID).read_text(encoding="utf-8") == "alice:300000:524288\n"
    assert (root / SUBGID).read_text(encoding="utf-8") == "alice:300000:524288\n"
    assert _record(root).subids is not None
    assert _record(root).subids.uid == subids.SubIdEntry("alice", 300000, 524288)


@pytest.mark.fr("FR-001")
def test_the_cli_configures_a_host_and_reports_the_record_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _host(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", _passwd)
    monkeypatch.setattr(setup_mod, "_owner_uid", _root_owned)
    status = cli_run(["--json", "host-setup", "--user", "alice", "--root-dir", str(root)])
    assert status == 0
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "configured"
    assert body["user"] == "alice"
    assert body["record"] == str(root / RECORD)
    assert body["paths"] == 9


@pytest.mark.fr("FR-001")
def test_the_global_lock_creates_run_lock_when_it_is_missing_and_records_it(tmp_path: Path) -> None:
    root = _host(tmp_path)
    shutil.rmtree(root / "run")
    _setup(root)
    shared = _shared(root)
    for path in ("run", "run/lock"):
        entry = _entry(shared, path)
        assert (entry.kind, entry.pre, entry.shared) == ("dir", rec.Image("absent"), True)
    assert (root / LOCK).is_file()
    assert LOCK not in {entry.path for entry in (*shared.paths, *_record(root).paths)}


@pytest.mark.fr("FR-001")
def test_a_run_blocked_on_the_held_lock_times_out_with_setup_busy_and_writes_nothing(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    before = _snapshot(root)
    holder = os.open(root / LOCK, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(GinnError) as caught:
            _setup(root, lock_timeout=0.2)
    finally:
        os.close(holder)
    assert caught.value.code == "GINN-E-SETUP-BUSY"
    assert caught.value.exit_code == 75
    assert "gave up after 0.2 s" in caught.value.message
    assert "--undo" in caught.value.remedy
    assert "/run/lock/ginn-host-setup.lock" in caught.value.remedy
    assert "flock" in caught.value.remedy
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_two_users_set_up_at_once_under_the_global_lock_and_both_records_are_right(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    original = b"carol:200000:65536\n"
    (root / SUBUID).write_bytes(original)
    (root / SUBGID).write_bytes(original)
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def run(name: str) -> None:
        try:
            barrier.wait()
            _setup(root, name)
        except BaseException as exc:  # collected and re-raised by the test thread
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("alice", "bob")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    alice, bob = _record(root, "alice"), _record(root, "bob")
    assert alice.status == bob.status == "complete"
    final = (root / SUBUID).read_bytes()
    assert final.count(b"alice:") == 1
    assert final.count(b"bob:") == 1
    assert alice.subids is not None and bob.subids is not None
    assert alice.subids.uid.render().encode() + b"\n" in final
    assert bob.subids.uid.render().encode() + b"\n" in final
    assert alice.subids.uid.end <= bob.subids.uid.start or bob.subids.uid.end <= (
        alice.subids.uid.start
    )
    # Each pre-image is the file as it stood when that run captured it: either the original
    # bytes, or the original plus the other user's line, if that run went second.
    alice_pre = _entry(alice, SUBUID).pre.data
    bob_pre = _entry(bob, SUBUID).pre.data
    assert alice_pre is not None and bob_pre is not None
    if alice_pre == original:
        assert bob_pre == original + alice.subids.uid.render().encode() + b"\n"
    else:
        assert bob_pre == original
        assert alice_pre == original + bob.subids.uid.render().encode() + b"\n"
    for own, other in ((alice, bob), (bob, alice)):
        assert _entry(own, f"etc/sv/ginn-{own.user}").kind == "dir"
        assert all(f"ginn-{other.user}" not in entry.path for entry in own.paths)
    assert _shared(root).paths[0].path == "etc/ginn"
    assert _setup(root, "alice").status == "already configured"
    assert _setup(root, "bob").status == "already configured"


@pytest.mark.fr("FR-001")
def test_a_directory_that_a_racing_process_made_first_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _host(tmp_path)
    real_mkdir = os.mkdir
    target = str(root / "etc/sv/ginn-alice")

    def racing_mkdir(path: Any, mode: int = 0o777, *args: Any, **kwargs: Any) -> None:
        real_mkdir(path, mode, *args, **kwargs)
        if str(path) == target:
            raise FileExistsError(errno.EEXIST, "made by a racing run", str(path))

    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    assert _setup(root).status == "configured"
    assert os.path.isdir(root / "etc/sv/ginn-alice")


@pytest.mark.fr("FR-001")
def test_a_link_that_a_racing_process_made_first_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _host(tmp_path)
    real_symlink = os.symlink
    target = str(root / LINK)

    def racing_symlink(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        real_symlink(src, dst, *args, **kwargs)
        if str(dst) == target:
            raise FileExistsError(errno.EEXIST, "made by a racing run", str(dst))

    monkeypatch.setattr(os, "symlink", racing_symlink)
    assert _setup(root).status == "configured"
    assert os.readlink(root / LINK) == "/etc/sv/ginn-alice"


@pytest.mark.fr("FR-001")
def test_on_void_the_service_link_goes_into_the_directory_var_service_resolves_to(
    tmp_path: Path,
) -> None:
    root = _void_host(tmp_path)
    result = _setup(root)
    assert result.status == "configured"
    assert os.readlink(root / "var/service") == "../run/runit/runsvdir/current"
    assert os.readlink(root / "run/runit/runsvdir/current") == "/etc/runit/runsvdir/current"
    assert os.readlink(root / "etc/runit/runsvdir/current") == "default"
    link = root / RESOLVED_LINK
    assert link.is_symlink()
    assert os.readlink(link) == "/etc/sv/ginn-alice"
    record = _record(root)
    entry = _entry(record, RESOLVED_LINK)
    assert entry.kind == "symlink"
    assert entry.pre == rec.Image("absent")
    assert entry.post == rec.Image("symlink", target="/etc/sv/ginn-alice")
    assert entry.shared is False
    assert all(not item.path.startswith("var/") for item in record.paths)
    assert (root / RUN).is_file()
    assert (root / "etc/sv/ginn-alice/log/run").is_file()


@pytest.mark.fr("FR-002")
def test_on_void_a_second_run_is_already_configured_and_the_tree_is_byte_and_mtime_identical(
    tmp_path: Path,
) -> None:
    root = _void_host(tmp_path)
    _setup(root)
    before = _snapshot(root)
    result = _setup(root, clock=NOW + 60)
    assert result.status == "already configured"
    assert _snapshot(root) == before


def _relink_service(root: Path, target: str) -> None:
    (root / "var/service").unlink()
    os.symlink(target, root / "var/service")


@pytest.mark.fr("FR-001")
def test_a_chain_that_leaves_the_root_is_refused_before_any_write_with_an_inspect_remedy(
    tmp_path: Path,
) -> None:
    root = _void_host(tmp_path)
    _relink_service(root, "../../../escape")
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert caught.value.exit_code == 77
    assert "leaves the root" in caught.value.message
    assert "inspect" in caught.value.remedy
    assert "aside" not in caught.value.remedy
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_a_chain_that_loops_is_refused_after_forty_hops(tmp_path: Path) -> None:
    root = _void_host(tmp_path)
    _relink_service(root, "../var/service")
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert "40 hops" in caught.value.message
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_a_chain_that_ends_at_a_file_is_refused(tmp_path: Path) -> None:
    root = _void_host(tmp_path)
    (root / "etc/notadir").write_text("not a directory\n", encoding="utf-8")
    _relink_service(root, "../etc/notadir")
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert "is a file in the var/service chain" in caught.value.message
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_a_chain_whose_end_is_not_owned_by_root_is_refused(tmp_path: Path) -> None:
    root = _void_host(tmp_path)
    before = _snapshot(root)
    chain_end = root / "etc/runit/runsvdir/default"
    with pytest.raises(GinnError) as caught:
        _setup(root, owner_of=lambda path: 1000 if path == chain_end else 0)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert "uid 0" in caught.value.message
    assert _snapshot(root) == before


@pytest.mark.fr("FR-001")
def test_a_symbolic_link_anywhere_else_on_the_path_is_refused_strictly(tmp_path: Path) -> None:
    root = _void_host(tmp_path)
    os.rmdir(root / "etc/sv")
    os.symlink("runit", root / "etc/sv")
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert "is a symlink" in caught.value.message
    assert "aside" not in caught.value.remedy
    assert _snapshot(root) == before


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-001")
def test_on_void_a_crash_at_every_step_resumes_to_the_same_record(tmp_path: Path) -> None:
    clean = _void_host(tmp_path / "clean")
    _setup(clean)
    crashed = _void_host(tmp_path / "crashed")
    with pytest.raises(_Crash):
        _setup(crashed, fault=_crash_after("symlink"))
    _setup(crashed, clock=NOW + 60)
    assert replace(_record(crashed), timestamp="") == replace(_record(clean), timestamp="")
    assert _shared(crashed) == _shared(clean)
    assert _content_without(crashed, RECORD) == _content_without(clean, RECORD)


@pytest.mark.fr("NFR-040")
def test_the_remedy_for_a_system_path_refusal_names_the_inspection_and_no_move(
    tmp_path: Path,
) -> None:
    root = _void_host(tmp_path)
    _relink_service(root, "../../../escape")
    with pytest.raises(GinnError) as caught:
        _setup(root)
    remedy = caught.value.remedy
    assert remedy.startswith("inspect ")
    assert "'sudo ls -ld " in remedy
    assert remedy.endswith("then run 'sudo ginn host-setup --user <name>' again")
    assert "move" not in remedy


@pytest.mark.fr("FR-003")
def test_the_pre_images_of_a_void_host_hold_the_true_state_before_the_link(tmp_path: Path) -> None:
    root = _void_host(tmp_path)
    _setup(root)
    record = _record(root)
    shared = _shared(root)
    assert _entry(record, RESOLVED_LINK).pre == rec.Image("absent")
    # The resolved directory existed before setup, so setup records nothing about it.
    recorded = {entry.path for entry in (*record.paths, *shared.paths)}
    assert "etc/runit/runsvdir/default" not in recorded
    assert _entry(shared, SUBTREE).pre.data == b"cpu io memory\n"


@pytest.mark.fr("FR-002")
def test_a_rerun_refuses_to_write_through_a_parent_that_became_a_symbolic_link(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root)
    decoy = root / "decoy"
    decoy.mkdir()
    shutil.rmtree(root / "etc/sv")
    os.symlink("../decoy", root / "etc/sv")
    before = _snapshot(root)
    with pytest.raises(GinnError) as caught:
        _setup(root, clock=NOW + 60)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert list(decoy.iterdir()) == []
    assert _snapshot(root) == before


@pytest.mark.fr("DR-009")
def test_a_shared_directory_already_in_shared_json_is_recreated_without_changing_shared_json(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    shutil.rmtree(root / "etc/sv")
    _setup(root, "alice")
    assert _entry(_shared(root), "etc/sv").pre == rec.Image("absent")
    shutil.rmtree(root / "etc/sv")
    shared_bytes = (root / SHARED).read_bytes()
    _setup(root, "bob")
    assert (root / SHARED).read_bytes() == shared_bytes
    assert (root / "etc/sv/ginn-bob").is_dir()
    assert stat.S_IMODE(os.stat(root / "etc/sv").st_mode) == 0o755


@pytest.mark.fr("DR-009")
def test_an_existing_etc_ginn_with_no_records_gets_a_shared_json_and_does_not_record_itself(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    (root / "etc/ginn").mkdir()
    _setup(root, "alice")
    shared = _shared(root)
    assert "etc/ginn" not in {entry.path for entry in shared.paths}
    assert _record(root).status == "complete"


CLASSIFIED: tuple[tuple[str, int, str, bool], ...] = (
    ("ginn", 1001, "etc/ginn", True),
    ("ginn", 1001, "etc/ginn/shared.json", True),
    ("ginn", 1001, "etc/ginn/setup-ginn.json", False),
    ("ginn", 1001, "sys/fs/cgroup/ginn", True),
    ("ginn", 1001, "sys/fs/cgroup/ginn/1001", False),
    ("ginn", 1001, "sys/fs/cgroup/ginn/1001/supervisor", False),
    ("ginn", 1001, "run/ginn", True),
    ("ginn", 1001, "run/ginn/1001", False),
    ("ginn", 1001, "run/ginn/10010", True),
    ("ginn", 1001, "etc/sv", True),
    ("ginn", 1001, "etc/sv/ginn-ginn", False),
    ("ginn", 1001, "etc/sv/ginn-ginn/log/run", False),
    ("ginn", 1001, "var/service/ginn-ginn", False),
    ("ginn", 1001, "etc/subuid", True),
    ("sv", 1002, "etc/sv", True),
    ("sv", 1002, "etc/sv/ginn-sv/run", False),
    ("sv", 1002, "etc/ginn", True),
    ("run", 1003, "run", True),
    ("run", 1003, "run/lock", True),
    ("run", 1003, "run/ginn", True),
    ("run", 1003, "run/ginn/1003", False),
    ("run", 1003, "sys/fs/cgroup/ginn/1003", False),
)
"""(user, uid, path, shared) rows for `_is_shared`. `etc/subuid` is shared by path, and its
entries are kept per-user by kind (`_is_user_entry`)."""


@pytest.mark.fr("DR-009")
@pytest.mark.parametrize(("user", "uid", "rel", "shared"), CLASSIFIED)
def test_a_path_is_per_user_only_when_a_whole_component_is_the_uid_or_the_user_named_ones(
    user: str, uid: int, rel: str, shared: bool
) -> None:
    target = setup_mod._Target(user=user, uid=uid, gid=uid, rendered={}, link_rel="")
    assert setup_mod._is_shared(rel, target) is shared


@pytest.mark.fr("FR-001")
@pytest.mark.fr("DR-009")
@pytest.mark.parametrize("name", list(NAMED_USERS))
def test_a_user_named_like_a_shared_path_component_leaves_that_path_shared(
    tmp_path: Path, name: str
) -> None:
    root = _host(tmp_path)
    shutil.rmtree(root / "etc/sv")
    _setup(root, name)
    uid = NAMED_USERS[name].uid
    record = _record(root, name)
    shared = _shared(root)
    assert {entry.path for entry in shared.paths} == {"etc/ginn", "etc/sv", SHARED}
    assert _entry(shared, "etc/sv").pre == rec.Image("absent")
    assert {entry.path for entry in shared.service_paths} == set(SHARED_SERVICE)
    assert all(entry.shared for entry in (*shared.paths, *shared.service_paths))
    per_user = {entry.path for entry in (*record.paths, *record.service_paths)}
    assert per_user.isdisjoint({"etc/ginn", "etc/sv", SHARED, "run", *SHARED_SERVICE})
    assert {entry.path for entry in record.paths if entry.shared} == {SUBUID, SUBGID}
    assert _entry(record, f"etc/ginn/setup-{name}.json").shared is False
    assert _entry(record, f"etc/sv/ginn-{name}/run").kind == "file"
    assert _entry(record, f"etc/sv/ginn-{name}").shared is False
    assert {
        f"run/ginn/{uid}",
        f"sys/fs/cgroup/ginn/{uid}",
        f"sys/fs/cgroup/ginn/{uid}/supervisor",
    } <= per_user
    before = _snapshot(root)
    assert _setup(root, name, clock=NOW + 60).status == "already configured"
    assert _snapshot(root) == before


@pytest.mark.fr("FR-002")
@pytest.mark.fr("DR-009")
def test_a_rerun_does_not_check_the_mode_of_a_shared_directory(tmp_path: Path) -> None:
    """ADR-013, Decision 18: shared entries carry pre-images only, so a rerun leaves their modes."""
    root = _host(tmp_path)
    shutil.rmtree(root / "etc/sv")
    _setup(root)
    assert _entry(_shared(root), "etc/sv").post is None
    os.chmod(root / "etc/sv", 0o700)
    before = _snapshot(root)
    assert _setup(root, clock=NOW + 60).status == "already configured"
    assert _snapshot(root) == before
    assert stat.S_IMODE(os.stat(root / "etc/sv").st_mode) == 0o700


@pytest.mark.fr("DR-009")
def test_shared_json_carries_a_post_image_only_for_itself(tmp_path: Path) -> None:
    """ADR-013, Decision 18 and Decision 9: the self entry is the only one with a post-image."""
    root = _host(tmp_path)
    shutil.rmtree(root / "etc/sv")
    _setup(root)
    shared = _shared(root)
    assert [entry.path for entry in shared.paths if entry.post is not None] == [SHARED]
    assert all(entry.post is None for entry in shared.service_paths)


@pytest.mark.fr("NFR-040")
def test_two_path_refusals_with_different_reasons_carry_the_same_general_remedy(
    tmp_path: Path,
) -> None:
    """ADR-013, Decision 19: the catalogue remedy is one text, and the case is in the reason."""
    root = _host(tmp_path)
    os.chmod(root / "etc", 0o775)  # noqa: S103 - the test makes a parent group-writable on purpose
    with pytest.raises(GinnError) as mode:
        _setup(root)
    os.chmod(root / "etc", 0o755)  # noqa: S103 - restores the mode the test changed above
    with pytest.raises(GinnError) as owner:
        _setup(root, owner_of=lambda path: 1000 if path.name == "etc" else 0)
    assert mode.value.code == owner.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert "writable by group or others" in mode.value.message
    assert "is owned by uid 1000" in owner.value.message
    assert mode.value.remedy == owner.value.remedy
    assert mode.value.remedy.startswith("inspect ")


@pytest.mark.fr("FR-001")
@pytest.mark.fr("NFR-030")
def test_a_crash_after_run_lock_is_created_leaves_it_unrecorded(tmp_path: Path) -> None:
    """ADR-013, Residuals (d). The fault after the first records stands in for a SIGKILL."""
    root = _host(tmp_path)
    shutil.rmtree(root / "run")
    with pytest.raises(_Crash):
        _setup(root, fault=_crash_after("tmpdir"))
    assert (root / "run/lock").is_dir()
    assert not (root / "etc/ginn").exists()
    _setup(root, clock=NOW + 60)
    shared = _shared(root)
    assert {"run", "run/lock"}.isdisjoint({entry.path for entry in shared.paths})
    assert (root / "run/lock").is_dir()
