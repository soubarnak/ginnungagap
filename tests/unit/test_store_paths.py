# SPDX-License-Identifier: Apache-2.0
"""Owned Paths from XDG variables, the runtime directory choice and directory creation (DR-010)."""

from pathlib import Path

import pytest

from ginn.errors import GinnError
from ginn.store.paths import OwnedPaths


def _base_env(home: Path) -> dict[str, str]:
    return {"HOME": str(home)}


@pytest.mark.fr("DR-010")
def test_defaults_follow_xdg_base_directories_under_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = OwnedPaths.from_env(_base_env(home), uid=1000, system_run=tmp_path / "no-run")
    assert paths.data == home / ".local" / "share" / "ginn"
    assert paths.config == home / ".config" / "ginn"
    assert paths.state == home / ".local" / "state" / "ginn"
    assert paths.runtime == home / ".local" / "state" / "ginn" / "run"
    assert paths.applications_dir == home / ".local" / "share" / "applications"


@pytest.mark.fr("DR-010")
def test_xdg_variables_set_the_roots_through_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-runtime"))
    monkeypatch.delenv("GINN_RUNTIME_DIR", raising=False)

    paths = OwnedPaths.from_env(uid=1000)

    assert paths.data == tmp_path / "xdg-data" / "ginn"
    assert paths.config == tmp_path / "xdg-config" / "ginn"
    assert paths.state == tmp_path / "xdg-state" / "ginn"
    assert paths.runtime == tmp_path / "xdg-runtime" / "ginn"
    assert paths.applications_dir == tmp_path / "xdg-data" / "applications"


@pytest.mark.fr("DR-010")
def test_relative_and_empty_xdg_values_count_as_unset(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {
        "HOME": str(home),
        "XDG_DATA_HOME": "relative/data",
        "XDG_CONFIG_HOME": "",
        "XDG_STATE_HOME": "not-absolute",
    }
    paths = OwnedPaths.from_env(env, uid=1000, system_run=tmp_path / "no-run")
    assert paths.data == home / ".local" / "share" / "ginn"
    assert paths.config == home / ".config" / "ginn"
    assert paths.state == home / ".local" / "state" / "ginn"


@pytest.mark.fr("DR-010")
def test_ginn_runtime_dir_overrides_the_xdg_runtime_dir(tmp_path: Path) -> None:
    override = tmp_path / "custom-runtime"
    env = {
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "xdg-runtime"),
        "GINN_RUNTIME_DIR": str(override),
    }
    paths = OwnedPaths.from_env(env, uid=1000, system_run=tmp_path / "no-run")
    assert paths.runtime == override


@pytest.mark.fr("DR-010")
def test_relative_ginn_runtime_dir_is_a_usage_error(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "GINN_RUNTIME_DIR": "relative/run"}
    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env(env, uid=1000, system_run=tmp_path / "no-run")
    assert excinfo.value.code == "GINN-E-USAGE"
    assert excinfo.value.exit_code == 2


@pytest.mark.fr("DR-010")
def test_system_run_directory_is_used_only_when_it_exists(tmp_path: Path) -> None:
    system_run = tmp_path / "run"
    env = {"HOME": str(tmp_path)}

    without = OwnedPaths.from_env(env, uid=4242, system_run=system_run)
    assert without.runtime == tmp_path / ".local" / "state" / "ginn" / "run"

    (system_run / "ginn" / "4242").mkdir(parents=True)
    with_dir = OwnedPaths.from_env(env, uid=4242, system_run=system_run)
    assert with_dir.runtime == system_run / "ginn" / "4242"


@pytest.mark.fr("DR-010")
def test_derived_locations_sit_under_their_roots(tmp_path: Path) -> None:
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=tmp_path / "no-run")
    assert paths.podman_root == paths.data / "podman"
    assert paths.podman_runroot == paths.runtime / "podman"
    assert paths.realms == paths.state / "realms"
    assert paths.homes == paths.data / "homes"
    assert paths.icons == paths.data / "icons"
    assert paths.bin == paths.data / "bin"
    assert paths.cdi == paths.data / "cdi"
    assert paths.sv == paths.state / "sv"
    assert paths.audit_log == paths.state / "audit.log"
    assert paths.journal == paths.state / "journal"
    assert paths.locks == paths.runtime / "locks"


@pytest.mark.fr("DR-010")
def test_owned_roots_are_the_four_roots_and_not_the_applications_directory(
    tmp_path: Path,
) -> None:
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=tmp_path / "no-run")
    roots = paths.all_owned_roots()
    assert roots == [paths.data, paths.config, paths.state, paths.runtime]
    assert paths.applications_dir not in roots


@pytest.mark.fr("DR-010")
def test_only_ginn_desktop_entries_in_applications_are_owned(tmp_path: Path) -> None:
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=tmp_path / "no-run")
    assert paths.desktop_entry_glob == "ginn-*.desktop"
    assert paths.owned_desktop_entries() == []

    apps = paths.applications_dir
    apps.mkdir(parents=True)
    (apps / "ginn-debian-shell.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    (apps / "firefox.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    assert paths.owned_desktop_entries() == [apps / "ginn-debian-shell.desktop"]


@pytest.mark.fr("DR-010")
@pytest.mark.fr("DR-021")
def test_ensure_creates_every_owned_directory_with_mode_0700(tmp_path: Path) -> None:
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=tmp_path / "no-run")
    paths.ensure()
    for directory in paths.owned_directories():
        assert directory.is_dir(), directory
        assert directory.stat().st_mode & 0o777 == 0o700, directory

    # A second run changes nothing and reports nothing.
    reports: list[str] = []
    paths.ensure(on_tighten=reports.append)
    assert reports == []


@pytest.mark.fr("DR-021")
def test_ensure_tightens_open_directories_and_reports_each_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=tmp_path / "no-run")
    paths.state.mkdir(parents=True, mode=0o700)
    paths.realms.mkdir(mode=0o755)
    paths.realms.chmod(0o755)

    reports: list[str] = []
    paths.ensure(on_tighten=reports.append)

    assert paths.realms.stat().st_mode & 0o777 == 0o700
    assert len(reports) == 1
    assert str(paths.realms) in reports[0]
    assert "to 700" in reports[0]
    assert capsys.readouterr().out == ""


@pytest.mark.fr("DR-010")
def test_ensure_refuses_a_symlink_in_place_of_an_owned_directory(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=tmp_path / "no-run")
    paths.data.parent.mkdir(parents=True)
    paths.data.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError, match="symbolic link"):
        paths.ensure()
    assert target.stat().st_mode & 0o777 == 0o755
