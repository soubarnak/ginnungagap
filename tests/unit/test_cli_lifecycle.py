# SPDX-License-Identifier: Apache-2.0
"""`ginn start|stop|status|list` through the CLI entry point (FR-016, FR-020, FR-025, AS-025).

The ops run for real on LifecyclePodman (the fake with the helper answered from tmp_path). The
JSON keys of a status are pinned by the ops tests, so these tests check the CLI around them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.domain import RealmName, RealmState
from ginn.errors import make
from ginn.store.records import RealmStore
from tests.unit.realm_cli_support import World, make_realm, stored
from tests.unit.test_ops_recovery import _dead, _write_journal

STATUS_KEYS = (
    "name",
    "state",
    "container_state",
    "state_mismatch",
    "distro",
    "tier",
    "services",
    "generation",
    "image_digest",
    "created_at",
    "updated_at",
    "restart_required",
    "share_skipped",
    "disk",
    "exported_apps",
    "error",
)


@pytest.mark.fr("FR-016")
def test_start_runs_the_realm_and_prints_its_state(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    assert main(["start", "arch"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "realm arch is running"
    assert all(line.startswith(("share skipped: ", "session skipped: ")) for line in lines[1:])
    assert stored(world, "arch").state is RealmState.RUNNING


@pytest.mark.fr("FR-016")
def test_start_json_has_the_state_and_the_skipped_shares(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    assert main(["--json", "start", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "arch"
    assert payload["state"] == "running"
    assert payload["restart_required"] is False
    assert isinstance(payload["share_skipped"], list)


@pytest.mark.fr("FR-025")
def test_start_of_an_unknown_realm_is_not_found_with_exit_3(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["start", "nosuch"]) == 3
    assert capsys.readouterr().err.startswith("GINN-E-NO-SUCH-REALM: ")


@pytest.mark.fr("IR-011")
def test_a_bad_name_is_an_argument_error_with_exit_2(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["start", "Not Valid"]) == 2
    assert capsys.readouterr().err.startswith("GINN-E-NAME-INVALID: ")


@pytest.mark.fr("FR-016")
def test_stop_stops_a_running_realm(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    assert main(["stop", "arch"]) == 0
    assert capsys.readouterr().out == "realm arch is stopped\n"
    assert stored(world, "arch").state is RealmState.STOPPED


@pytest.mark.fr("FR-016")
def test_stop_json_is_the_record_after_the_stop(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    assert main(["--json", "stop", "arch"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "stopped"


@pytest.mark.fr("FR-020")
@pytest.mark.fr("IR-010")
def test_status_json_has_the_pinned_keys(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    assert main(["status", "arch", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert sorted(payload) == sorted(STATUS_KEYS)
    assert payload["name"] == "arch"
    assert payload["state"] == "running"
    assert payload["container_state"] == "running"
    assert payload["state_mismatch"] is False


@pytest.mark.fr("FR-020")
def test_status_text_is_one_line_per_field(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    assert main(["status", "arch"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "name: arch" in lines
    assert "state: stopped" in lines
    assert any(line.startswith("disk: realm home ") for line in lines)


@pytest.mark.fr("FR-025")
def test_status_of_an_unknown_realm_is_not_found(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status", "nosuch", "--json"]) == 3
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "GINN-E-NO-SUCH-REALM"


@pytest.mark.fr("FR-020")
def test_list_json_is_sorted_by_name_and_has_every_realm(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "fedora", RealmState.STOPPED, running=False, distro="fedora")
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["list", "--json"]) == 0
    realms = json.loads(capsys.readouterr().out)["realms"]
    assert [row["name"] for row in realms] == ["arch", "fedora"]
    assert all(sorted(row) == sorted(STATUS_KEYS) for row in realms)


@pytest.mark.fr("FR-020")
def test_list_text_is_a_table_and_says_when_there_are_no_realms(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["list"]) == 0
    assert capsys.readouterr().out == "no realms\n"
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    assert main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "NAME",
        "STATE",
        "CONTAINER",
        "DISTRO",
        "TIER",
        "SERVICES",
        "DISK",
        "NOTES",
    ]
    assert lines[1].split()[:2] == ["arch", "stopped"]


@pytest.mark.fr("FR-122")
def test_a_broken_realm_names_both_recovery_commands(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "debian", RealmState.BROKEN, running=False)
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "realm debian is broken" in out
    assert "'ginn create --resume debian'" in out
    assert "'ginn delete debian'" in out
    assert main(["status", "debian"]) == 0
    assert "'ginn delete debian'" in capsys.readouterr().out


@pytest.mark.fr("FR-122")
def test_an_interrupted_create_is_settled_as_broken_at_the_next_command(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead create journal file and a creating record: the next list shows the realm broken."""
    make_realm(world, "debian", RealmState.CREATING, running=False)
    _write_journal(world.paths, _dead(op="create", realm="debian", step="pulled"))
    assert main(["--json", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["state"] for row in payload["realms"]] == ["broken"]
    (notice,) = payload["warnings"]
    assert notice["code"] == "GINN-W-RECOVERED"
    assert "interrupted create of realm 'debian' as broken" in notice["message"]
    assert not list(world.paths.journal.glob("*.json"))


@pytest.mark.fr("FR-122")
def test_a_realm_command_stops_when_recovery_raises(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(env: object, podman: object, **kwargs: object) -> list[object]:
        raise make("GINN-E-INTERNAL")

    monkeypatch.setattr(_context, "recover", broken)
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    assert main(["start", "arch"]) == 70
    assert stored(world, "arch").state is RealmState.STOPPED
    assert capsys.readouterr().err.startswith("GINN-E-INTERNAL: ")


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-020")
def test_status_and_list_go_on_when_recovery_raises(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(env: object, podman: object, **kwargs: object) -> list[object]:
        raise make("GINN-E-INTERNAL")

    monkeypatch.setattr(_context, "recover", broken)
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    assert main(["status", "arch"]) == 0
    assert "state: stopped" in capsys.readouterr().out
    assert main(["list"]) == 0
    captured = capsys.readouterr()
    assert "arch" in captured.out
    assert captured.err.startswith("GINN-W-RECOVERY-UNSETTLED: ")


@pytest.mark.fr("FR-122")
def test_a_corrupt_journal_file_is_reported_as_unsettled(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "debian", RealmState.CREATING, running=False)
    path = _write_journal(world.paths, _dead(op="create", realm="debian", step="pulled"))
    path.write_text("not json", encoding="utf-8")
    assert main(["list"]) == 0
    err = capsys.readouterr().err
    assert "GINN-W-RECOVERY-UNSETTLED: " in err
    assert str(path) in err
    assert RealmStore(world.paths).load(RealmName("debian")).state is RealmState.CREATING


def _host_changes_during(world: World, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    """Make podman's `method` change a host file, as an unrelated process would, mid-operation."""
    real = getattr(world.fake, method)

    def change_then_call(*args: Any) -> Any:
        (world.host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")
        return real(*args)

    monkeypatch.setattr(world.fake, method, change_then_call)


@pytest.mark.fr("FR-006")
def test_a_host_change_during_start_is_warned_on_stderr_in_text_mode(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    _host_changes_during(world, monkeypatch, "start")
    assert main(["start", "arch"]) == 0
    err = capsys.readouterr().err
    assert err.startswith("GINN-W-HOST-CHANGED: ")
    assert "/etc/hosts" in err
    assert stored(world, "arch").state is RealmState.RUNNING


@pytest.mark.fr("FR-006")
def test_a_host_change_during_start_is_in_the_json_result(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    _host_changes_during(world, monkeypatch, "start")
    assert main(["--json", "start", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    (notice,) = payload["warnings"]
    assert notice["code"] == "GINN-W-HOST-CHANGED"
    assert payload["state"] == "running"


@pytest.mark.fr("FR-006")
def test_a_host_change_during_stop_is_warned_and_the_stop_still_succeeds(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _host_changes_during(world, monkeypatch, "stop")
    assert main(["stop", "arch"]) == 0
    assert capsys.readouterr().err.startswith("GINN-W-HOST-CHANGED: ")
    assert stored(world, "arch").state is RealmState.STOPPED
