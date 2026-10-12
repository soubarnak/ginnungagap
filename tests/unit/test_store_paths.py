# SPDX-License-Identifier: Apache-2.0
"""Owned Paths from XDG variables, the single runtime root and directory creation (DR-010)."""

from pathlib import Path

import pytest

from ginn.errors import GinnError
from ginn.store.paths import OwnedPaths


def _base_env(home: Path) -> dict[str, str]:
    return {"HOME": str(home)}


def _system_run(tmp_path: Path, uid: int = 1000) -> Path:
    """Create Host Setup's runtime directory /run/ginn/<uid> under a temporary root.

    The directory is 0700, as Host Setup leaves it, so ensure() reports nothing about it.
    """
    system_run = tmp_path / "run"
    (system_run / "ginn" / str(uid)).mkdir(parents=True, mode=0o700)
    return system_run


@pytest.mark.fr("DR-010")
def test_defaults_follow_xdg_base_directories_under_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = OwnedPaths.from_env(_base_env(home), uid=1000, system_run=_system_run(tmp_path))
    assert paths.data == home / ".local" / "share" / "ginn"
    assert paths.config == home / ".config" / "ginn"
    assert paths.state == home / ".local" / "state" / "ginn"
    assert paths.runtime == tmp_path / "run" / "ginn" / "1000"
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
    system_run = _system_run(tmp_path)

    paths = OwnedPaths.from_env(uid=1000, system_run=system_run)

    assert paths.data == tmp_path / "xdg-data" / "ginn"
    assert paths.config == tmp_path / "xdg-config" / "ginn"
    assert paths.state == tmp_path / "xdg-state" / "ginn"
    # XDG_RUNTIME_DIR is ignored: the runtime root is Host Setup's directory (ADR-004).
    assert paths.runtime == system_run / "ginn" / "1000"
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
    paths = OwnedPaths.from_env(env, uid=1000, system_run=_system_run(tmp_path))
    assert paths.data == home / ".local" / "share" / "ginn"
    assert paths.config == home / ".config" / "ginn"
    assert paths.state == home / ".local" / "state" / "ginn"


@pytest.mark.fr("DR-010")
def test_ginn_runtime_dir_overrides_the_default_and_ignores_xdg_runtime_dir(
    tmp_path: Path,
) -> None:
    override = tmp_path / "custom-runtime"
    env = {
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "xdg-runtime"),
        "GINN_RUNTIME_DIR": str(override),
    }
    paths = OwnedPaths.from_env(env, uid=1000, system_run=_system_run(tmp_path))
    assert paths.runtime == override


@pytest.mark.fr("DR-010")
def test_relative_ginn_runtime_dir_is_a_usage_error(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "GINN_RUNTIME_DIR": "relative/run"}
    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env(env, uid=1000, system_run=_system_run(tmp_path))
    assert excinfo.value.code == "GINN-E-USAGE"
    assert excinfo.value.exit_code == 2


@pytest.mark.fr("DR-010")
@pytest.mark.fr("FR-123")
def test_missing_runtime_directory_is_refused_and_never_replaced_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An XDG_RUNTIME_DIR that exists must not be used instead of /run/ginn/<uid>.
    xdg_runtime = tmp_path / "xdg-runtime"
    xdg_runtime.mkdir()
    monkeypatch.delenv("GINN_RUNTIME_DIR", raising=False)
    env = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(xdg_runtime)}

    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env(env, uid=1000, system_run=tmp_path / "run")

    assert excinfo.value.code == "GINN-E-USAGE"
    assert excinfo.value.exit_code == 2
    assert "host-setup" in excinfo.value.message
    assert str(tmp_path / "run" / "ginn" / "1000") in excinfo.value.message
    # No runroot, tmpdir or locks on disk under state: nothing was created.
    assert not (tmp_path / ".local" / "state" / "ginn").exists()


@pytest.mark.fr("DR-010")
def test_runtime_directory_is_per_user(tmp_path: Path) -> None:
    system_run = _system_run(tmp_path, uid=4242)
    env = {"HOME": str(tmp_path)}
    assert OwnedPaths.from_env(env, uid=4242, system_run=system_run).runtime == (
        system_run / "ginn" / "4242"
    )
    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env(env, uid=1000, system_run=system_run)
    assert excinfo.value.code == "GINN-E-USAGE"


@pytest.mark.fr("DR-010")
@pytest.mark.fr("FR-123")
@pytest.mark.parametrize("with_override", [False, True])
def test_supervisor_and_session_resolve_one_runtime_root(
    tmp_path: Path, with_override: bool
) -> None:
    system_run = _system_run(tmp_path)
    runtime = system_run / "ginn" / "1000"
    home = str(tmp_path / "home")

    # The boot-time supervisor exports XDG_RUNTIME_DIR and GINN_RUNTIME_DIR, both set to the
    # runtime directory (hostsetup/runit_system.py). The override is optional here.
    supervisor = {"HOME": home, "XDG_RUNTIME_DIR": str(runtime)}
    if with_override:
        supervisor["GINN_RUNTIME_DIR"] = str(runtime)
    # A login session has its own XDG_RUNTIME_DIR under /run/user, and no GINN_RUNTIME_DIR.
    session = {"HOME": home, "XDG_RUNTIME_DIR": "/run/user/1000"}

    boot = OwnedPaths.from_env(supervisor, uid=1000, system_run=system_run)
    login = OwnedPaths.from_env(session, uid=1000, system_run=system_run)

    assert boot.runtime == login.runtime == runtime
    assert boot.locks == login.locks == runtime / "locks"
    assert boot.podman_runroot == login.podman_runroot == runtime / "podman"
    assert boot.podman_tmpdir == login.podman_tmpdir == runtime / "podman-tmp"


@pytest.mark.fr("DR-010")
def test_derived_locations_sit_under_their_roots(tmp_path: Path) -> None:
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=_system_run(tmp_path))
    assert paths.podman_root == paths.data / "podman"
    assert paths.podman_runroot == paths.runtime / "podman"
    assert paths.podman_tmpdir == paths.runtime / "podman-tmp"
    assert paths.podman_containers_conf == paths.config / "containers.conf"
    assert paths.podman_storage_conf == paths.config / "storage.conf"
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
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=_system_run(tmp_path))
    roots = paths.all_owned_roots()
    assert roots == [paths.data, paths.config, paths.state, paths.runtime]
    assert paths.applications_dir not in roots


@pytest.mark.fr("DR-010")
def test_only_ginn_desktop_entries_in_applications_are_owned(tmp_path: Path) -> None:
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=_system_run(tmp_path))
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
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=_system_run(tmp_path))
    paths.ensure()
    for directory in paths.owned_directories():
        assert directory.is_dir(), directory
        assert directory.stat().st_mode & 0o777 == 0o700, directory
    assert paths.podman_tmpdir in paths.owned_directories()

    # A second run changes nothing and reports nothing.
    reports: list[str] = []
    paths.ensure(on_tighten=reports.append)
    assert reports == []


@pytest.mark.fr("DR-021")
def test_ensure_tightens_open_directories_and_reports_each_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=_system_run(tmp_path))
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
    paths = OwnedPaths.from_env(_base_env(tmp_path), uid=1000, system_run=_system_run(tmp_path))
    paths.data.parent.mkdir(parents=True)
    paths.data.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError, match="symbolic link"):
        paths.ensure()
    assert target.stat().st_mode & 0o777 == 0o755
