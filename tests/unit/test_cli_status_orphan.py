# SPDX-License-Identifier: Apache-2.0
"""`ginn status` shows a realm's orphan marker as GINN-W-RELAY-ORPHAN, read only (FR-124, ADR-014).

The marker is audited and removed by the realm's next mutating operation (test_mutating_exec_lock).
Status only reads it, so these tests check that status shows it, and leaves the marker and the
Audit Log exactly as they were.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ginn.__main__ import main
from ginn.cli.commands import status as status_cmd
from ginn.domain import RealmName, RealmState
from ginn.services.exec_relay import exec_lock_dir, exec_lock_path, exec_orphan_path
from ginn.store.audit import AuditLog
from tests.unit.realm_cli_support import World, make_realm

EXEC_HEX = "0123456789abcdef0123456789abcdef"
ARCH = RealmName("arch")
MARKER = f"token {EXEC_HEX}\ntime 1700000000\npid 111\npid 222\n"
SURVIVED = (
    "a command in realm 'arch' left processes that the token kill could not stop: "
    "pids 111, 222 survived the token kill"
)
KILL_NOT_RUN = (
    "a command in realm 'arch' left processes that the token kill could not stop: "
    "the token kill did not run, so no pid is recorded"
)
UNKNOWN_PIDS = (
    "the token kill could not read pids 333 of a command in realm 'arch'; they may still run"
)
UNKNOWN_PROCESSES = (
    "the token kill could not read the processes of a command in realm 'arch'; they may still run"
)
UNKNOWN_PIDS_AND_OTHERS = (
    "the token kill could not read pids 333, and other processes of a command in realm 'arch'; "
    "they may still run"
)


def _marker(world: World, text: str) -> Path:
    """Write the realm's orphan marker as the token kill would, under the runtime root."""
    exec_lock_dir(world.paths).mkdir(mode=0o700, exist_ok=True)
    path = exec_orphan_path(world.paths, ARCH)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.fr("FR-124")
@pytest.mark.fr("FR-020")
def test_status_shows_an_orphan_marker_as_a_warning_in_text(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    _marker(world, MARKER)

    assert main(["status", "arch"]) == 0
    assert f"GINN-W-RELAY-ORPHAN: {SURVIVED}" in capsys.readouterr().err


@pytest.mark.fr("FR-124")
def test_status_json_carries_the_orphan_as_a_warning(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    _marker(world, MARKER)

    assert main(["--json", "status", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "arch"
    assert [(w["code"], w["message"]) for w in payload["warnings"]] == [
        ("GINN-W-RELAY-ORPHAN", SURVIVED)
    ]


@pytest.mark.fr("FR-124")
def test_status_lists_every_record_of_the_marker(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    _marker(world, MARKER + f"token {'1' * 32}\ntime 1700000100\nkill not run\n")

    assert main(["--json", "status", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [w["message"] for w in payload["warnings"]] == [SURVIVED, KILL_NOT_RUN]


@pytest.mark.fr("FR-124")
def test_a_kill_that_did_not_run_is_said_so_and_names_no_pid(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    _marker(world, f"token {EXEC_HEX}\ntime 1700000000\nkill not run\n")

    assert main(["status", "arch"]) == 0
    assert f"GINN-W-RELAY-ORPHAN: {KILL_NOT_RUN}" in capsys.readouterr().err


@pytest.mark.fr("FR-124")
@pytest.mark.parametrize(
    ("record", "codes", "unknown"),
    [
        ("unknown 333\n", ["GINN-W-ORPHAN-UNKNOWN"], UNKNOWN_PIDS),
        ("unknown join\n", ["GINN-W-ORPHAN-UNKNOWN"], UNKNOWN_PROCESSES),
        (
            "pid 111\nunknown 333\n",
            ["GINN-W-RELAY-ORPHAN", "GINN-W-ORPHAN-UNKNOWN"],
            UNKNOWN_PIDS,
        ),
        (
            "unknown join\nunknown 333\n",
            ["GINN-W-ORPHAN-UNKNOWN"],
            UNKNOWN_PIDS_AND_OTHERS,
        ),
    ],
    ids=["unknown-pids", "unknown-join", "survivors-and-unknown-pids", "join-and-unknown-pids"],
)
def test_status_shows_unreadable_processes_as_their_own_warning(
    world: World,
    capsys: pytest.CaptureFixture[str],
    record: str,
    codes: list[str],
    unknown: str,
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    _marker(world, f"token {EXEC_HEX}\ntime 1700000000\n{record}")

    assert main(["--json", "status", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [w["code"] for w in payload["warnings"]] == codes
    assert payload["warnings"][-1]["message"] == unknown


@pytest.mark.fr("FR-124")
def test_status_shows_no_unknown_warning_for_survivors_alone(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    _marker(world, MARKER)

    assert main(["--json", "status", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [w["code"] for w in payload["warnings"]] == ["GINN-W-RELAY-ORPHAN"]


@pytest.mark.fr("FR-020")
def test_status_without_a_marker_has_no_warning(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)

    assert main(["--json", "status", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "warnings" not in payload


@pytest.mark.fr("FR-124")
def test_status_leaves_the_marker_and_the_audit_log_as_they_are(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    marker = _marker(world, MARKER)

    assert main(["status", "arch"]) == 0
    capsys.readouterr()
    assert marker.read_text(encoding="utf-8") == MARKER
    assert [entry for entry in AuditLog(world.paths).read() if entry.op == "exec-orphan"] == []


@pytest.mark.fr("FR-124")
def test_status_does_not_follow_a_symlinked_marker(
    world: World, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    exec_lock_dir(world.paths).mkdir(mode=0o700, exist_ok=True)
    target = tmp_path / "elsewhere.txt"
    target.write_text(MARKER, encoding="utf-8")
    link = exec_orphan_path(world.paths, ARCH)
    link.symlink_to(target)

    assert main(["--json", "status", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "warnings" not in payload
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == MARKER


@contextmanager
def _exec_running(world: World) -> Iterator[None]:
    """Hold the realm's exec lock as a running mutating exec does (flock, LOCK_EX)."""
    exec_lock_dir(world.paths).mkdir(mode=0o700, exist_ok=True)
    fd = os.open(exec_lock_path(world.paths, ARCH), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.mark.fr("FR-124")
def test_status_lists_no_orphan_while_the_exec_lock_is_busy(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A busy lock's exec may still be writing its marker, so status does not read it then."""
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    _marker(world, MARKER)

    with _exec_running(world):
        assert main(["--json", "status", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "warnings" not in payload


@pytest.mark.fr("FR-124")
def test_status_lists_no_orphan_past_a_faulty_lock_path_and_still_succeeds(
    world: World, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A symlink where the lock belongs is GINN-E-INTERNAL for the operations. Status goes on."""
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    exec_lock_dir(world.paths).mkdir(mode=0o700, exist_ok=True)
    target = tmp_path / "elsewhere.lock"
    target.write_text("run 1 podman 2", encoding="utf-8")
    exec_lock_path(world.paths, ARCH).symlink_to(target)
    _marker(world, MARKER)

    assert main(["--json", "status", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "warnings" not in payload


@pytest.mark.fr("FR-124")
def test_an_unreadable_marker_is_a_warning_and_status_still_succeeds(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)

    def unreadable(paths: object, name: object) -> tuple[()]:
        raise PermissionError(13, "the marker directory is not readable")

    monkeypatch.setattr(status_cmd, "read_orphans", unreadable)

    assert main(["--json", "status", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [(w["code"], w["message"]) for w in payload["warnings"]] == [
        (
            "GINN-W-ORPHAN-UNSETTLED",
            "the orphan marker of realm 'arch' could not be settled, so it is kept: "
            "PermissionError: [Errno 13] the marker directory is not readable",
        )
    ]
