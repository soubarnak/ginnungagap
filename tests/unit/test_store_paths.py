# SPDX-License-Identifier: Apache-2.0
"""Owned Paths: XDG roots, runtime root checks, directory and config writes (DR-010).

Tests run as the current user, so the runtime directory is owned by `UID`. Owner and mode checks
are exercised by passing a different uid, or by changing the directory's mode.
"""

import os
import stat
import threading
import tomllib
from pathlib import Path

import pytest

from ginn.errors import GinnError
from ginn.store import paths as store_paths
from ginn.store.paths import OwnedPaths, write_bytes_atomic

UID = os.getuid()


def _base_env(home: Path) -> dict[str, str]:
    return {"HOME": str(home)}


def _system_run(tmp_path: Path, uid: int = UID) -> Path:
    """Create Host Setup's runtime directory /run/ginn/<uid> under a temporary root.

    The directory is 0700, as Host Setup leaves it, so ensure() reports nothing about it.
    """
    system_run = tmp_path / "run"
    (system_run / "ginn" / str(uid)).mkdir(parents=True, mode=0o700)
    return system_run


def _paths(tmp_path: Path) -> OwnedPaths:
    return OwnedPaths.from_env(_base_env(tmp_path), uid=UID, system_run=_system_run(tmp_path))


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.fr("DR-010")
def test_defaults_follow_xdg_base_directories_under_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = OwnedPaths.from_env(_base_env(home), uid=UID, system_run=_system_run(tmp_path))
    assert paths.data == home / ".local" / "share" / "ginn"
    assert paths.config == home / ".config" / "ginn"
    assert paths.state == home / ".local" / "state" / "ginn"
    assert paths.runtime == tmp_path / "run" / "ginn" / str(UID)
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

    paths = OwnedPaths.from_env(uid=UID, system_run=system_run)

    assert paths.data == tmp_path / "xdg-data" / "ginn"
    assert paths.config == tmp_path / "xdg-config" / "ginn"
    assert paths.state == tmp_path / "xdg-state" / "ginn"
    # XDG_RUNTIME_DIR is ignored: the runtime root is Host Setup's directory (ADR-004).
    assert paths.runtime == system_run / "ginn" / str(UID)
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
    paths = OwnedPaths.from_env(env, uid=UID, system_run=_system_run(tmp_path))
    assert paths.data == home / ".local" / "share" / "ginn"
    assert paths.config == home / ".config" / "ginn"
    assert paths.state == home / ".local" / "state" / "ginn"


@pytest.mark.fr("DR-010")
def test_ginn_runtime_dir_overrides_the_default_and_ignores_xdg_runtime_dir(
    tmp_path: Path,
) -> None:
    override = tmp_path / "custom-runtime"
    override.mkdir(mode=0o700)
    env = {
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "xdg-runtime"),
        "GINN_RUNTIME_DIR": str(override),
    }
    paths = OwnedPaths.from_env(env, uid=UID, system_run=_system_run(tmp_path))
    assert paths.runtime == override


@pytest.mark.fr("DR-010")
def test_relative_ginn_runtime_dir_is_a_usage_error(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "GINN_RUNTIME_DIR": "relative/run"}
    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env(env, uid=UID, system_run=_system_run(tmp_path))
    assert excinfo.value.code == "GINN-E-USAGE"
    assert excinfo.value.exit_code == 2


@pytest.mark.fr("DR-010")
def test_missing_runtime_directory_is_refused_and_never_replaced_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An XDG_RUNTIME_DIR that exists must not be used instead of /run/ginn/<uid>.
    xdg_runtime = tmp_path / "xdg-runtime"
    xdg_runtime.mkdir()
    monkeypatch.delenv("GINN_RUNTIME_DIR", raising=False)
    env = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(xdg_runtime)}

    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env(env, uid=UID, system_run=tmp_path / "run")

    assert excinfo.value.code == "GINN-E-HOST-NOT-SET-UP"
    assert excinfo.value.exit_code == 77
    assert "host-setup" in excinfo.value.remedy
    assert str(tmp_path / "run" / "ginn" / str(UID)) in excinfo.value.message
    assert excinfo.value.details == {"path": str(tmp_path / "run" / "ginn" / str(UID))}
    # No runroot, tmpdir or locks on disk under state: nothing was created.
    assert not (tmp_path / ".local" / "state" / "ginn").exists()


@pytest.mark.fr("DR-010")
def test_missing_ginn_runtime_dir_override_is_refused(tmp_path: Path) -> None:
    absent = tmp_path / "never-created"
    env = {"HOME": str(tmp_path), "GINN_RUNTIME_DIR": str(absent)}
    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env(env, uid=UID, system_run=_system_run(tmp_path))
    assert excinfo.value.code == "GINN-E-HOST-NOT-SET-UP"
    assert excinfo.value.details == {"path": str(absent)}
    assert not absent.exists()


@pytest.mark.fr("DR-010")
@pytest.mark.parametrize("use_override", [False, True])
def test_runtime_directory_with_a_wider_mode_is_refused_and_not_repaired(
    tmp_path: Path, use_override: bool
) -> None:
    system_run = _system_run(tmp_path)
    runtime = system_run / "ginn" / str(UID)
    runtime.chmod(0o755)
    env = {"HOME": str(tmp_path)}
    if use_override:
        env["GINN_RUNTIME_DIR"] = str(runtime)

    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env(env, uid=UID, system_run=system_run)

    assert excinfo.value.code == "GINN-E-RUNTIME-DIR-UNSAFE"
    assert excinfo.value.exit_code == 77
    assert excinfo.value.details["found"] == f"owner uid {UID}, mode 755"
    assert excinfo.value.details["path"] == str(runtime)
    assert "mode 0700" in excinfo.value.message
    # The owner may chmod its own directory, but ginn refuses rather than repair it.
    assert _mode(runtime) == 0o755


@pytest.mark.fr("DR-010")
def test_runtime_directory_owned_by_another_uid_is_refused(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    env = {"HOME": str(tmp_path), "GINN_RUNTIME_DIR": str(runtime)}
    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env(env, uid=UID + 1, system_run=tmp_path)
    assert excinfo.value.code == "GINN-E-RUNTIME-DIR-UNSAFE"
    assert excinfo.value.details["uid"] == str(UID + 1)
    assert excinfo.value.details["found"] == f"owner uid {UID}, mode 700"


@pytest.mark.fr("DR-010")
def test_runtime_path_that_is_a_symbolic_link_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    system_run = tmp_path / "run"
    (system_run / "ginn").mkdir(parents=True, mode=0o700)
    (system_run / "ginn" / str(UID)).symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env({"HOME": str(tmp_path)}, uid=UID, system_run=system_run)

    assert excinfo.value.code == "GINN-E-RUNTIME-DIR-UNSAFE"
    assert excinfo.value.details["found"] == "a symbolic link"
    assert _mode(elsewhere) == 0o700


@pytest.mark.fr("DR-010")
def test_runtime_path_that_is_a_file_is_refused(tmp_path: Path) -> None:
    system_run = tmp_path / "run"
    (system_run / "ginn").mkdir(parents=True, mode=0o700)
    (system_run / "ginn" / str(UID)).write_text("not a directory", encoding="utf-8")

    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env({"HOME": str(tmp_path)}, uid=UID, system_run=system_run)

    assert excinfo.value.code == "GINN-E-RUNTIME-DIR-UNSAFE"
    assert excinfo.value.details["found"] == "a file that is not a directory"


@pytest.mark.fr("DR-010")
@pytest.mark.fr("FR-123")
def test_supervisor_and_session_resolve_one_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GINN_RUNTIME_DIR", raising=False)
    system_run = _system_run(tmp_path)
    runtime = system_run / "ginn" / str(UID)
    home = str(tmp_path / "home")

    # The boot-time supervisor exports XDG_RUNTIME_DIR, and GINN_RUNTIME_DIR too
    # (hostsetup/runit_system.py). Both are the runtime directory, so the override is optional.
    supervisor = {"HOME": home, "XDG_RUNTIME_DIR": str(runtime)}
    supervisor_override = {**supervisor, "GINN_RUNTIME_DIR": str(runtime)}
    # A login session has its own XDG_RUNTIME_DIR under /run/user, and no GINN_RUNTIME_DIR.
    session = {"HOME": home, "XDG_RUNTIME_DIR": "/run/user/1000"}

    boot = OwnedPaths.from_env(supervisor, uid=UID, system_run=system_run)
    boot_override = OwnedPaths.from_env(supervisor_override, uid=UID, system_run=system_run)
    login = OwnedPaths.from_env(session, uid=UID, system_run=system_run)

    assert boot.runtime == boot_override.runtime == login.runtime == runtime
    assert boot.locks == login.locks == runtime / "locks"
    assert boot.podman_runroot == login.podman_runroot == runtime / "podman"
    assert boot.podman_tmpdir == login.podman_tmpdir == runtime / "podman-tmp"


@pytest.mark.fr("DR-010")
def test_runtime_directory_is_per_user(tmp_path: Path) -> None:
    system_run = _system_run(tmp_path)
    env = {"HOME": str(tmp_path)}
    assert OwnedPaths.from_env(env, uid=UID, system_run=system_run).runtime == (
        system_run / "ginn" / str(UID)
    )
    # Another user's directory is not this user's. UID+1 has none, so the refusal names that path.
    with pytest.raises(GinnError) as excinfo:
        OwnedPaths.from_env(env, uid=UID + 1, system_run=system_run)
    assert excinfo.value.code == "GINN-E-HOST-NOT-SET-UP"
    assert excinfo.value.details == {"path": str(system_run / "ginn" / str(UID + 1))}


@pytest.mark.fr("DR-010")
def test_derived_locations_sit_under_their_roots(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert paths.podman_root == paths.data / "podman"
    assert paths.podman_runroot == paths.runtime / "podman"
    assert paths.podman_tmpdir == paths.runtime / "podman-tmp"
    assert paths.podman_containers_conf == paths.config / "containers.conf"
    assert paths.podman_storage_conf == paths.config / "storage.conf"
    assert paths.image_tmp == paths.data / "image-tmp"
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
    paths = _paths(tmp_path)
    roots = paths.all_owned_roots()
    assert roots == [paths.data, paths.config, paths.state, paths.runtime]
    assert paths.applications_dir not in roots


@pytest.mark.fr("DR-010")
def test_only_ginn_desktop_entries_in_applications_are_owned(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert paths.desktop_entry_glob == "ginn-*.desktop"
    assert paths.owned_desktop_entries() == []

    apps = paths.applications_dir
    apps.mkdir(parents=True)
    (apps / "ginn-debian-shell.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    (apps / "firefox.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    assert paths.owned_desktop_entries() == [apps / "ginn-debian-shell.desktop"]


@pytest.mark.fr("DR-010")
@pytest.mark.fr("FR-025")
def test_ensure_creates_every_owned_directory_with_mode_0700(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    for directory in paths.owned_directories():
        assert directory.is_dir(), directory
        assert directory.stat().st_mode & 0o777 == 0o700, directory
    assert paths.podman_tmpdir in paths.owned_directories()
    # The runtime directory is Host Setup's, not ensure()'s (see the two tests below).
    assert paths.runtime not in paths.owned_directories()

    # A second run changes nothing and reports nothing.
    reports: list[str] = []
    paths.ensure(on_tighten=reports.append)
    assert reports == []


@pytest.mark.fr("DR-010")
@pytest.mark.fr("FR-025")
def test_ensure_leaves_the_runtime_directory_mode_alone(tmp_path: Path) -> None:
    # Owner and mode of the runtime directory are checked by check_runtime_dir. ensure() neither
    # tightens it nor reports it.
    paths = _paths(tmp_path)
    paths.runtime.chmod(0o755)
    reports: list[str] = []
    paths.ensure(on_tighten=reports.append)
    assert _mode(paths.runtime) == 0o755
    assert reports == []


@pytest.mark.fr("DR-010")
def test_ensure_refuses_a_missing_runtime_directory_and_creates_nothing(tmp_path: Path) -> None:
    # Creating a child would also create the runtime directory, with the default mode. It must not.
    paths = _paths(tmp_path)
    paths.runtime.rmdir()
    with pytest.raises(GinnError) as excinfo:
        paths.ensure()
    assert excinfo.value.code == "GINN-E-HOST-NOT-SET-UP"
    assert excinfo.value.details == {"path": str(paths.runtime)}
    assert not paths.runtime.exists()
    assert not paths.data.exists()


@pytest.mark.fr("CR-006")
@pytest.mark.fr("DR-010")
def test_image_tmp_is_an_owned_directory_on_disk_under_data(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert paths.image_tmp in paths.owned_directories()
    assert paths.image_tmp.is_relative_to(paths.data)
    # Images are large, so this directory stays on disk, outside the tmpfs runtime root.
    assert not paths.image_tmp.is_relative_to(paths.runtime)
    paths.ensure()
    assert _mode(paths.image_tmp) == 0o700


@pytest.mark.fr("FR-025")
def test_ensure_tightens_open_directories_and_reports_each_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _paths(tmp_path)
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
    paths = _paths(tmp_path)
    paths.data.parent.mkdir(parents=True)
    paths.data.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError, match="symbolic link"):
        paths.ensure()
    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.fr("DR-010")
@pytest.mark.fr("CR-006")
def test_ensure_config_writes_both_files_mode_0600_with_the_pinned_settings(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.ensure_config()

    containers = tomllib.loads(paths.podman_containers_conf.read_text(encoding="utf-8"))
    assert containers == {"engine": {"runtime": "crun", "events_logger": "file"}}
    storage = tomllib.loads(paths.podman_storage_conf.read_text(encoding="utf-8"))
    assert storage == {
        "storage": {
            "driver": "overlay",
            "runroot": str(paths.podman_runroot),
            "graphroot": str(paths.podman_root),
        }
    }
    for path in (paths.podman_containers_conf, paths.podman_storage_conf):
        assert _mode(path) == 0o600, path
        assert not path.with_suffix(".tmp").exists()
    # Every file ensure_config writes sits under the config root.
    assert paths.podman_containers_conf.parent == paths.config
    assert paths.podman_storage_conf.parent == paths.config


@pytest.mark.fr("DR-010")
def test_ensure_config_rewrites_only_a_file_that_changed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure_config()
    containers_inode = paths.podman_containers_conf.stat().st_ino
    storage_inode = paths.podman_storage_conf.stat().st_ino

    # Unchanged content and mode: nothing is replaced, so the inode stays.
    paths.ensure_config()
    assert paths.podman_containers_conf.stat().st_ino == containers_inode
    assert paths.podman_storage_conf.stat().st_ino == storage_inode

    # A changed file is rewritten to the pinned text; the other one is left alone.
    paths.podman_storage_conf.write_text('[storage]\ndriver = "vfs"\n', encoding="utf-8")
    paths.ensure_config()
    assert "overlay" in paths.podman_storage_conf.read_text(encoding="utf-8")
    assert paths.podman_containers_conf.stat().st_ino == containers_inode

    # A file with a wider mode is rewritten to 0600 as well.
    paths.podman_containers_conf.chmod(0o644)
    paths.ensure_config()
    assert _mode(paths.podman_containers_conf) == 0o600


@pytest.mark.fr("DR-010")
def test_ensure_config_quotes_paths_that_contain_quotes_and_backslashes(tmp_path: Path) -> None:
    awkward_home = tmp_path / 'we"ird\\home'
    paths = OwnedPaths.from_env(
        {"HOME": str(awkward_home)}, uid=UID, system_run=_system_run(tmp_path)
    )
    paths.ensure_config()
    storage = tomllib.loads(paths.podman_storage_conf.read_text(encoding="utf-8"))
    assert storage["storage"]["graphroot"] == str(paths.podman_root)
    assert storage["storage"]["runroot"] == str(paths.podman_runroot)


@pytest.mark.fr("DR-010")
def test_ensure_config_refuses_a_symbolic_link_in_place_of_a_config_file(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    outside = tmp_path / "outside.conf"
    outside.write_text("keep me", encoding="utf-8")
    paths.podman_containers_conf.symlink_to(outside)

    with pytest.raises(OSError, match="symbolic link"):
        paths.ensure_config()
    assert outside.read_text(encoding="utf-8") == "keep me"


ROUNDS = 20


def _names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


@pytest.mark.fr("DR-035")
def test_a_stale_temporary_file_neither_changes_the_mode_nor_is_used(tmp_path: Path) -> None:
    # The fixed name the old writer used, with the mode a stale 0644 file would keep.
    target = tmp_path / "containers.conf"
    stale = tmp_path / "containers.tmp"
    stale.write_bytes(b"stale")
    stale.chmod(0o644)

    write_bytes_atomic(target, b"fresh\n")

    assert target.read_bytes() == b"fresh\n"
    assert _mode(target) == 0o600
    assert stale.read_bytes() == b"stale"
    assert _mode(stale) == 0o644


@pytest.mark.fr("DR-035")
def test_two_concurrent_writers_never_leave_an_empty_or_mixed_file(tmp_path: Path) -> None:
    target = tmp_path / "realm.json"
    payloads = [b"A" * 65_536, b"B" * 65_536]
    write_bytes_atomic(target, payloads[0])
    barrier = threading.Barrier(len(payloads))
    failures: list[BaseException] = []

    def writer(data: bytes) -> None:
        try:
            barrier.wait()
            for _ in range(ROUNDS):
                write_bytes_atomic(target, data)
                # Each replace is atomic, so a reader sees one whole payload, never a mix or empty.
                assert target.read_bytes() in payloads
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(data,)) for data in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert failures == []
    assert target.read_bytes() in payloads
    assert _mode(target) == 0o600
    assert _names(tmp_path) == ["realm.json"]


@pytest.mark.fr("DR-035")
def test_a_failed_rename_removes_the_temporary_file_and_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "containers.conf"
    target.write_bytes(b"old\n")

    def refuse(source: str, destination: str) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(store_paths.os, "replace", refuse)
    with pytest.raises(OSError, match="rename refused"):
        write_bytes_atomic(target, b"new\n")

    assert target.read_bytes() == b"old\n"
    assert _names(tmp_path) == ["containers.conf"]
