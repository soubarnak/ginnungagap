# SPDX-License-Identifier: Apache-2.0
"""Host Setup for one user under a root prefix: root check, rerun, pre-images and resume.

Covers FR-001 (AC-2, the root check; the subid, service and link writes), FR-002 (AC-1, a rerun
that changes no byte or mtime and reports "already configured"), DR-009 (the record lists every
path written, with its pre-image), NFR-030 (a crash after any step resumes from the stored
pre-images) and NFR-040 (refusals carry a catalogue code). Each test runs under tmp_path, with a
root_dir that mirrors the host layout. None of them touches the real /etc, /var/service, /run or
/sys. The clock and the user lookup are injected, and the root check takes an explicit euid.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ginn.cli.main import run as cli_run
from ginn.errors import GinnError
from ginn.hostsetup import record as rec
from ginn.hostsetup import runit_system, subids
from ginn.hostsetup.setup import SetupResult, UserInfo, run_setup

USERS: dict[str, UserInfo] = {
    "alice": UserInfo(uid=1000, gid=1000, home="/home/alice"),
    "bob": UserInfo(uid=1001, gid=1001, home="/home/bob"),
}
NOW = 1_800_000_000.0
RECORD = "etc/ginn/setup-alice.json"
RUN = "etc/sv/ginn-alice/run"
FINISH = "etc/sv/ginn-alice/finish"
LOG_RUN = "etc/sv/ginn-alice/log/run"
LINK = "var/service/ginn-alice"
SUBUID = "etc/subuid"
SUBGID = "etc/subgid"
SUBTREE = "sys/fs/cgroup/cgroup.subtree_control"
ALICE_RANGE = subids.SubIdEntry("alice", 100000, 524288)


class _Crash(Exception):
    """Stands in for a crash, such as SIGKILL or power loss, right after one setup step."""


def _crash_after(step: str) -> Callable[[str], None]:
    def hook(done: str) -> None:
        if done == step:
            raise _Crash(done)

    return hook


def _host(tmp_path: Path) -> Path:
    """A temporary root that mirrors the host paths that Host Setup reads or writes."""
    root = tmp_path / "host"
    for rel in ("etc", "etc/sv", "var/service", "run/lock", "sys/fs/cgroup"):
        (root / rel).mkdir(parents=True)
    (root / SUBTREE).write_text("cpu io memory\n", encoding="utf-8")
    return root


def _setup(
    root: Path,
    user: str = "alice",
    *,
    clock: float = NOW,
    fault: Callable[[str], None] | None = None,
) -> SetupResult:
    return run_setup(
        user,
        root_dir=root,
        euid=0,
        lookup_user=USERS.__getitem__,
        clock=lambda: clock,
        fault=fault,
    )


def _passwd(name: str) -> Any:
    """Stands in for pwd.getpwnam with the fields that Host Setup reads. KeyError when unknown."""
    info = USERS[name]
    return SimpleNamespace(pw_uid=info.uid, pw_gid=info.gid, pw_dir=info.home)


def _rendered(user: str = "alice") -> dict[str, str]:
    info = USERS[user]
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


def _entry(record: rec.HostSetupRecord, path: str) -> rec.PathEntry:
    for entry in (*record.paths, *record.service_paths):
        if entry.path == path:
            return entry
    raise AssertionError(f"{path} is not in the record")


def _pre_images(record: rec.HostSetupRecord) -> dict[str, rec.Image]:
    return {entry.path: entry.pre for entry in (*record.paths, *record.service_paths)}


def _snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every entry under root with its kind, mode or target, file digest and mtime_ns."""
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


@pytest.mark.fr("FR-001")
def test_the_scripts_keep_host_paths_and_setup_does_not_need_a_runtime_dir(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    text = (root / RUN).read_text(encoding="utf-8")
    assert "RUNTIME_DIR=/run/ginn/1000" in text
    assert str(root) not in text
    assert not (root / "run" / "ginn").exists()


@pytest.mark.fr("DR-009")
def test_the_record_lists_every_path_that_differs_after_the_run(tmp_path: Path) -> None:
    root = _host(tmp_path)
    before = _content(_snapshot(root))
    _setup(root)
    after = _content(_snapshot(root))
    differing = {rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel)}
    record = _record(root)
    assert differing == {entry.path for entry in record.paths}
    assert differing.isdisjoint({entry.path for entry in record.service_paths})
    assert _entry(record, RECORD).pre == rec.Image("absent")
    assert _entry(record, RECORD).post == rec.Image("self")


@pytest.mark.fr("DR-009")
def test_the_service_written_paths_carry_pre_images_and_no_post_image(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _setup(root)
    record = _record(root)
    subtree = _entry(record, SUBTREE)
    assert subtree.kind == "file"
    assert subtree.pre.data == b"cpu io memory\n"
    assert subtree.post is None
    ginn_dir = _entry(record, "sys/fs/cgroup/ginn")
    assert ginn_dir.pre == rec.Image("absent")
    assert ginn_dir.post is None
    assert not (root / "sys/fs/cgroup/ginn").exists()


@pytest.mark.fr("FR-002")
def test_a_second_run_reports_already_configured_and_changes_no_byte_or_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _host(tmp_path)
    _setup(root)
    before = _snapshot(root)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", _passwd)
    status = cli_run(["host-setup", "--user", "alice", "--root-dir", str(root)])
    assert status == 0
    assert "already configured" in capsys.readouterr().out
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
    digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert _entry(record, RUN).post == rec.Image("file", sha256=digest)


@pytest.mark.fr("FR-002")
def test_a_second_users_setup_does_not_make_the_first_users_rerun_report_drift(
    tmp_path: Path,
) -> None:
    root = _host(tmp_path)
    _setup(root, "alice")
    _setup(root, "bob")
    assert "bob:" in (root / SUBUID).read_text(encoding="utf-8")
    assert _setup(root, "alice").status == "already configured"


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("DR-009")
@pytest.mark.parametrize("step", ["pending", "subids", "sv", "symlink", "complete"])
def test_a_crash_after_any_step_resumes_with_the_first_pre_images(
    tmp_path: Path, step: str
) -> None:
    clean = _host(tmp_path / "clean")
    _setup(clean)
    crashed = _host(tmp_path / "crashed")
    with pytest.raises(_Crash):
        _setup(crashed, fault=_crash_after(step))
    interrupted = _record(crashed)
    assert interrupted.status == ("complete" if step == "complete" else "pending")
    assert _pre_images(interrupted) == _pre_images(_record(clean))
    _setup(crashed, clock=NOW + 60)
    assert replace(_record(crashed), timestamp="") == replace(_record(clean), timestamp="")
    assert _content_without(crashed, RECORD) == _content_without(clean, RECORD)


def _content_without(root: Path, rel: str) -> dict[str, tuple[Any, ...]]:
    return {path: entry for path, entry in _content(_snapshot(root)).items() if path != rel}


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
    assert _entry(record, SUBUID).pre == rec.Image(
        "file", mode=0o640, data=b"bob:200000:65536\n"
    )


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
        run_setup(
            "alice", root_dir=Path("relative"), euid=0, lookup_user=USERS.__getitem__
        )
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
    status = cli_run(["--json", "host-setup", "--user", "alice", "--root-dir", str(root)])
    assert status == 0
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "configured"
    assert body["user"] == "alice"
    assert body["record"] == str(root / RECORD)
    assert body["paths"] == 10
