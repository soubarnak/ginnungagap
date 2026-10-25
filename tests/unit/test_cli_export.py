# SPDX-License-Identifier: Apache-2.0
"""The CLI of export, unexport and the hidden `_sync` (IR-010, IR-011, FR-044..047).

`main` runs the real command modules on the pinned host of tests/unit/realm_cli_support.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.domain import RealmState
from ginn.export.icons import ICON_LIST
from ginn.export.sync import DESKTOP_LIST
from ginn.podman.models import ExecResult
from tests.unit.realm_cli_support import World, make_realm

PROGRAM = "/usr/bin/ginn"
ENTRY = b"[Desktop Entry]\nType=Application\nName=Firefox\nExec=firefox %u\n"


@pytest.fixture(autouse=True)
def _pinned_program(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_context, "ginn_program", lambda *_args: PROGRAM)


@pytest.fixture
def path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def _bin_dir(world: World) -> Path:
    return world.paths.bin


@pytest.mark.fr("FR-047")
def test_export_writes_the_launcher_and_says_when_the_bin_dir_is_not_on_path(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert main(["export", "--bin", "debian", "rg"]) == 0
    out = capsys.readouterr().out
    assert "exported rg from realm debian" in out
    assert f"{_bin_dir(world)} is not on PATH" in out
    assert (_bin_dir(world) / "rg").read_text(encoding="utf-8").startswith("#!/bin/sh\n")


@pytest.mark.fr("FR-047")
def test_export_says_nothing_about_path_when_the_bin_dir_is_on_path(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    monkeypatch.setenv("PATH", f"/usr/bin:{_bin_dir(world)}")
    assert main(["export", "--bin", "debian", "rg"]) == 0
    assert "not on PATH" not in capsys.readouterr().out


@pytest.mark.fr("FR-047")
def test_export_json_reports_the_launcher_and_whether_it_is_on_path(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert main(["export", "--bin", "debian", "rg", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["realm"] == "debian"
    assert payload["binary"] == "rg"
    assert payload["launcher"] == str(_bin_dir(world) / "rg")
    assert payload["changed"] is True
    assert payload["on_path"] is False


@pytest.mark.fr("FR-047")
def test_export_without_the_bin_flag_is_a_usage_error(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    assert main(["export", "debian", "rg"]) == 2
    assert not (_bin_dir(world) / "rg").exists()


@pytest.mark.fr("FR-047")
def test_export_with_a_bad_binary_name_is_refused_with_exit_2(world: World) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    assert main(["export", "--bin", "debian", "../rg"]) == 2
    assert list(_bin_dir(world).iterdir()) == []


@pytest.mark.fr("FR-047")
def test_unexport_removes_the_launcher(
    world: World, capsys: pytest.CaptureFixture[str], path_env: None
) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    assert main(["export", "--bin", "debian", "rg"]) == 0
    capsys.readouterr()
    assert main(["unexport", "--bin", "debian", "rg"]) == 0
    assert "removed launcher of rg from realm debian" in capsys.readouterr().out
    assert not (_bin_dir(world) / "rg").exists()


@pytest.mark.fr("FR-047")
def test_unexport_of_a_binary_that_is_not_exported_exits_3(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["unexport", "--bin", "debian", "rg"]) == 3


@pytest.mark.fr("FR-044")
def test_the_sync_command_is_hidden_from_the_top_level_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--help"]) == 0
    text = capsys.readouterr().out
    assert "_sync" not in text
    assert "export" in text and "unexport" in text


@pytest.mark.fr("FR-044")
def test_the_hidden_sync_command_still_answers_its_own_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["_sync", "--help"]) == 0
    assert "REALM" in capsys.readouterr().out


@pytest.mark.fr("FR-044")
def test_sync_runs_and_reports_the_entries_it_added(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    path = "/usr/share/applications/firefox.desktop"
    world.fake.files[path] = ENTRY
    world.fake.exec_results[DESKTOP_LIST] = ExecResult(0, f"{len(ENTRY)} 1 {path}\n", "")
    world.fake.exec_results[ICON_LIST] = ExecResult(0, "", "")
    assert main(["_sync", "arch", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["added"] == ["firefox"]
    assert payload["running"] is True
    entry = world.paths.applications_dir / "ginn-arch-firefox.desktop"
    assert "Name=Firefox (arch)" in entry.read_text(encoding="utf-8")


@pytest.mark.fr("FR-044")
def test_sync_of_a_stopped_realm_says_so(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    assert main(["_sync", "arch"]) == 0
    assert "does not run" in capsys.readouterr().out
