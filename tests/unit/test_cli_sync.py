# SPDX-License-Identifier: Apache-2.0
"""`ginn _sync --from-watch`: the watcher's run skips a sync that ginn's own transaction made
redundant, before any lock (FR-044, FR-045, FR-123, NFR-009, ADR-016)."""

from __future__ import annotations

import json

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.domain import RealmName, RealmState
from ginn.export.icons import ICON_LIST
from ginn.export.sync import DESKTOP_LIST
from ginn.export.watch import (
    STAMP_NAME,
    STAMP_RELATIVE,
    SYNC_SKIP_STATUS,
    read_sync_mark,
    stamp_snapshot,
    write_sync_mark,
)
from ginn.ops._mutating import OpEnv, mutating
from ginn.podman.models import ExecResult
from tests.unit.realm_cli_support import World, make_realm

ENTRY = b"[Desktop Entry]\nType=Application\nName=Firefox\nExec=firefox %u\n"
NAME = RealmName("arch")


@pytest.fixture(autouse=True)
def _pinned_program(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_context, "ginn_program", lambda *_args: "/usr/bin/ginn")


def _setup(world: World) -> None:
    """A running realm with one app and a stamp."""
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    path = "/usr/share/applications/firefox.desktop"
    world.fake.files[path] = ENTRY
    world.fake.exec_results[DESKTOP_LIST] = ExecResult(0, f"{len(ENTRY)} 1 {path}\n", "")
    world.fake.exec_results[ICON_LIST] = ExecResult(0, "", "")
    directory = world.paths.homes / "arch"
    for part in STAMP_RELATIVE:
        directory = directory / part
    directory.mkdir(parents=True, exist_ok=True)
    (directory / STAMP_NAME).write_bytes(b"")


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-123")
@pytest.mark.fr("NFR-009")
def test_from_watch_skips_with_no_lock_when_a_x(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(world)
    write_sync_mark(world.paths, NAME, stamp_snapshot(world.paths, NAME))
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host)
    with mutating(env, "install", NAME):  # ginn install is still inside its lock
        world.fake.calls.clear()
        assert main(["_sync", "arch", "--from-watch"]) == SYNC_SKIP_STATUS
    assert capsys.readouterr().out == ""
    assert world.fake.calls == []
    assert not (world.paths.applications_dir / "ginn-arch-firefox.desktop").exists()


@pytest.mark.fr("FR-123")
def test_from_watch_with_a_newer_stamp_takes_the_lock_and_is_refused_when_busy(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(world)
    write_sync_mark(world.paths, NAME, (1, 1))
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host)
    with mutating(env, "install", NAME):
        assert main(["_sync", "arch", "--from-watch"]) == 75
    assert "GINN-E-REALM-BUSY" in capsys.readouterr().err


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-045")
def test_from_watch_runs_when_the_stamp_is_newer_or_there_is_no_record(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(world)
    assert main(["_sync", "arch", "--from-watch", "--json"]) == 0  # no record
    assert json.loads(capsys.readouterr().out)["added"] == ["firefox"]
    assert read_sync_mark(world.paths, NAME) == stamp_snapshot(world.paths, NAME)
    write_sync_mark(world.paths, NAME, (1, 1))  # the stamp changed since
    (world.paths.applications_dir / "ginn-arch-firefox.desktop").unlink()
    assert main(["_sync", "arch", "--from-watch", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == ["firefox"]


@pytest.mark.fr("FR-044")
def test_a_manual_sync_never_skips(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    _setup(world)
    write_sync_mark(world.paths, NAME, stamp_snapshot(world.paths, NAME))
    assert main(["_sync", "arch", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == ["firefox"]
