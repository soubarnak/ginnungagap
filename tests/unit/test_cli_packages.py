# SPDX-License-Identifier: Apache-2.0
"""`ginn install` and `ginn remove` through the CLI entry point (T5.3, IR-010, IR-011).

The result is one JSON object with --json (realm, packages, manifest). Each refusal exits with
its catalogue status and names its code on stderr, and a refusal before a podman call runs none.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ginn.__main__ import main
from ginn.distros import Verb, load
from ginn.domain import InstallMethod, InstallSource, ManifestEntry, RealmState
from ginn.export.sync import DESKTOP_LIST
from ginn.podman.models import ExecResult
from ginn.store.records import RealmStore
from tests.unit.realm_cli_support import World, make_realm, stored


def _error(err: str) -> dict[str, object]:
    """The error object of a --json run: its last stderr line."""
    return json.loads(err.strip().splitlines()[-1])["error"]


def _init(name: str) -> ManifestEntry:
    return ManifestEntry(name=name, source=InstallSource.REPO, method=InstallMethod.GINN, init=True)


def _with_init(world: World, realm: str, *entries: ManifestEntry) -> None:
    """Store `realm` with `entries` as its manifest, so the install sees its init entries."""
    RealmStore(world.paths).save(replace(stored(world, realm), manifest=list(entries)))


@pytest.mark.fr("FR-040")
@pytest.mark.fr("DR-007")
def test_install_prints_the_result_and_records_the_packages(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    assert main(["--json", "install", "deb", "htop", "gimp"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["realm"] == "deb"
    assert out["packages"] == ["htop", "gimp"]
    assert out["manifest"] == [
        {"name": "gimp", "source": "repo", "method": "ginn"},
        {"name": "htop", "source": "repo", "method": "ginn"},
    ]
    assert [e.name for e in stored(world, "deb").manifest] == ["gimp", "htop"]


@pytest.mark.fr("FR-040")
def test_install_in_text_mode_names_the_realm_and_the_packages(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["install", "arch", "htop"]) == 0
    assert capsys.readouterr().out == "installed in realm arch: htop\n"


@pytest.mark.fr("FR-040")
@pytest.mark.fr("NFR-SEC-005")
def test_a_bad_name_exits_two_with_its_code_and_runs_no_podman_call(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    assert main(["--json", "install", "deb", "gimp*"]) == 2
    error = _error(capsys.readouterr().err)
    assert error["code"] == "GINN-E-PKG-NAME-INVALID"
    assert error["exit_code"] == 2
    assert world.fake.calls == []


@pytest.mark.fr("FR-040")
def test_install_without_a_package_is_a_usage_error(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    assert main(["--json", "install", "deb"]) == 2
    assert _error(capsys.readouterr().err)["code"] == "GINN-E-USAGE"


@pytest.mark.fr("FR-043")
def test_aur_on_a_non_arch_realm_is_an_option_error_and_makes_no_call(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    assert main(["--json", "install", "deb", "--aur", "yay-bin"]) == 2
    assert _error(capsys.readouterr().err)["code"] == "GINN-E-OPTION-INVALID"
    assert world.fake.calls == []


@pytest.mark.fr("FR-043")
@pytest.mark.fr("IR-023")
def test_aur_on_arch_builds_in_the_realm_and_prints_source_aur_ginn(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.unit.test_ops_packages import _aur_answers

    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    _aur_answers(world)
    assert main(["--json", "install", "arch", "--aur", "yay-bin"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["manifest"] == [{"name": "yay-bin", "source": "aur", "method": "ginn"}]
    assert [e.name for e in stored(world, "arch").manifest] == ["yay-bin"]


@pytest.mark.fr("FR-041")
@pytest.mark.fr("DR-007")
def test_remove_runs_the_dry_run_and_removes_the_package(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["--json", "remove", "arch", "htop"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"realm": "arch", "packages": ["htop"], "manifest": []}
    removes = [call.argv for call in world.fake.exec_calls if call.op == "exec"]
    assert removes == [tuple(load("arch").argv(Verb.REMOVE, ["htop"])), DESKTOP_LIST]


@pytest.mark.fr("FR-041")
def test_remove_refuses_a_dependent_removal_with_exit_four(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    dry_run = tuple(load("debian").argv(Verb.REMOVE_CHECK, ["gimp"]))
    world.fake.exec_results[dry_run] = ExecResult(0, "Remv gimp [2.10]\nRemv gimpy [1]\n", "")
    assert main(["--json", "remove", "deb", "gimp"]) == 4
    error = _error(capsys.readouterr().err)
    assert error["code"] == "GINN-E-PKG-DEPENDENTS"
    assert error["exit_code"] == 4
    removes = [call.argv for call in world.fake.exec_calls if call.op == "exec"]
    assert removes == [dry_run]


@pytest.mark.fr("FR-041")
def test_remove_without_a_package_is_a_usage_error(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    assert main(["--json", "remove", "deb"]) == 2
    assert _error(capsys.readouterr().err)["code"] == "GINN-E-USAGE"


@pytest.mark.fr("FR-044")
def test_a_failed_menu_sync_after_an_install_exits_zero_and_warns_on_stderr(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[DESKTOP_LIST] = ExecResult(1, "", "find: Permission denied")
    assert main(["install", "deb", "htop"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "installed in realm deb: htop\n"
    assert captured.err.startswith(
        "GINN-W-EXPORT-SYNC: the host menu of realm 'deb' is not fully synced"
    )
    assert "find exited with status 1" in captured.err


@pytest.mark.fr("FR-040")
def test_a_normal_install_json_has_its_four_keys_and_no_notes(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    assert main(["--json", "install", "deb", "htop"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"realm", "packages", "manifest", "notes"}
    assert out["notes"] == []
    assert out["packages"] == ["htop"]


@pytest.mark.fr("FR-040")
def test_a_failed_install_that_names_an_init_entry_carries_the_note_on_the_error(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    _with_init(world, "arch", _init("systemd"))
    world.fake.exec_results[tuple(load("arch").argv(Verb.INSTALL, ["htop"]))] = ExecResult(
        1, "", "error: target not found: htop"
    )
    assert main(["--json", "install", "arch", "systemd", "htop"]) != 0
    error = _error(capsys.readouterr().err)
    assert error["code"] == "GINN-E-PKG-MANAGER"
    notes = error["notes"]
    assert isinstance(notes, list) and len(notes) == 1
    assert "'systemd' is an init package of realm 'arch'" in str(notes[0])


@pytest.mark.fr("FR-040")
def test_a_failed_install_in_text_mode_prints_the_note_as_a_failure_note(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    _with_init(world, "arch", _init("systemd"))
    world.fake.exec_results[tuple(load("arch").argv(Verb.INSTALL, ["htop"]))] = ExecResult(
        1, "", "error: target not found: htop"
    )
    assert main(["install", "arch", "systemd", "htop"]) != 0
    err_lines = capsys.readouterr().err.splitlines()
    assert any(
        line.startswith("  note: 'systemd' is an init package of realm 'arch'")
        for line in err_lines
    )


@pytest.mark.fr("FR-040")
def test_an_install_of_an_init_entry_in_text_mode_claims_nothing_was_installed(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    _with_init(world, "deb", _init("systemd"))
    assert main(["install", "deb", "systemd"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "nothing was installed in realm deb\n"
    # The note goes to stderr in text mode, one line, and the result on stdout claims nothing.
    (line,) = captured.err.splitlines()
    assert line.startswith("note: 'systemd' is an init package of realm 'deb'")
    assert world.fake.calls == []
    assert [e.name for e in stored(world, "deb").manifest] == ["systemd"]


@pytest.mark.fr("FR-040")
def test_an_install_of_an_init_entry_in_json_carries_the_note_and_no_packages(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    _with_init(world, "deb", _init("systemd"))
    assert main(["--json", "install", "deb", "systemd"]) == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["packages"] == []
    assert out["manifest"] == [
        {"name": "systemd", "source": "repo", "method": "ginn", "init": True}
    ]
    (note,) = out["notes"]
    assert "'systemd' is an init package of realm 'deb'" in note
    assert captured.err == ""
    assert world.fake.calls == []


@pytest.mark.fr("FR-040")
def test_an_install_that_names_an_init_entry_installs_the_others_in_text_mode(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    _with_init(world, "deb", _init("systemd"))
    assert main(["install", "deb", "systemd", "htop"]) == 0
    captured = capsys.readouterr()
    # The success line lists only htop, the one package this call installed.
    assert captured.out == "installed in realm deb: htop\n"
    (line,) = captured.err.splitlines()
    assert line.startswith("note: 'systemd' is an init package of realm 'deb'")


@pytest.mark.fr("FR-040")
def test_an_install_that_names_an_init_entry_asks_the_manager_only_for_the_others(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    _with_init(world, "deb", _init("systemd"))
    assert main(["--json", "install", "deb", "systemd", "htop"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["packages"] == ["htop"]
    assert len(out["notes"]) == 1
    assert out["manifest"] == [
        {"name": "htop", "source": "repo", "method": "ginn"},
        {"name": "systemd", "source": "repo", "method": "ginn", "init": True},
    ]
    calls = [call.argv for call in world.fake.exec_calls if call.op == "exec"]
    assert calls == [
        tuple(load("debian").argv(Verb.REFRESH)),
        tuple(load("debian").argv(Verb.INSTALL, ["htop"])),
        DESKTOP_LIST,
    ]


@pytest.mark.fr("FR-041")
def test_remove_of_an_init_entry_exits_four_with_protected_and_runs_no_call(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    _with_init(world, "deb", _init("systemd"))
    assert main(["--json", "remove", "deb", "systemd"]) == 4
    error = _error(capsys.readouterr().err)
    assert error["code"] == "GINN-E-PKG-PROTECTED"
    assert error["exit_code"] == 4
    assert world.fake.calls == []


def _unreadable_list_install(world: World) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[tuple(load("debian").argv(Verb.INSTALL, ["gimp"]))] = ExecResult(
        100, "", "E: Unable to locate package gimp"
    )
    world.fake.exec_results[tuple(load("debian").argv(Verb.LIST_EXPLICIT))] = ExecResult(
        1, "", "E: could not read the database"
    )


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_a_failed_install_with_an_unreadable_list_prints_the_unverified_note(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _unreadable_list_install(world)
    assert main(["install", "deb", "gimp"]) != 0
    err_lines = capsys.readouterr().err.splitlines()
    assert any(line.startswith("  note: GINN-W-MANIFEST-UNVERIFIED") for line in err_lines)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_a_failed_install_with_an_unreadable_list_carries_the_unverified_note_in_json(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _unreadable_list_install(world)
    assert main(["--json", "install", "deb", "gimp"]) != 0
    error = _error(capsys.readouterr().err)
    assert error["code"] == "GINN-E-PKG-MANAGER"
    notes = error["notes"]
    assert isinstance(notes, list)
    assert any(str(n).startswith("GINN-W-MANIFEST-UNVERIFIED") for n in notes)
