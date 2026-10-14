# SPDX-License-Identifier: Apache-2.0
"""`ginn update` in the CLI command module (T8.4, cli/commands/update.py, IR-010, IR-011).

The command is not in the command table yet (cli/commands/__init__.py), so these tests build its
parser from SPEC and call its run hook, as the table would. Each test runs on FakePodman through the
`world` fixture. Covered: the argument rules, the JSON shape of an update and of a check, the text
lines, the disk precheck note, the warning path, and the exit by raising the first failed realm.
"""

from __future__ import annotations

import argparse
import json
import os
import types

import pytest

from ginn.cli import _context
from ginn.cli.commands import Context
from ginn.cli.commands.update import SPEC
from ginn.cli.output import Output
from ginn.distros import Verb, load
from ginn.domain import RealmState
from ginn.errors import GinnError
from ginn.ops.lifecycle import LifecycleHooks
from ginn.ops.packages import PackageHooks
from ginn.ops.update import DISK_PRECHECK_SKIPPED
from ginn.packages import manager
from ginn.podman.models import ExecResult
from ginn.runtime.realm_security import SecurityHooks
from tests.unit.realm_cli_support import World, make_realm

DEBIAN_NETWORK = "E: Failed to fetch http://deb.debian.org/x: Temporary failure resolving 'deb'"


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> str:
    """The Arch check's scratch directory, pinned: the manager draws it from a uuid (ADR-011)."""
    fixed = types.SimpleNamespace(hex="cafe0000")
    monkeypatch.setattr(manager, "uuid", types.SimpleNamespace(uuid4=lambda: fixed))
    return f"{manager.SCRATCH_PREFIX}cafe0000"


@pytest.fixture(autouse=True)
def _no_real_sleep(world: World, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every package hook of these tests waits nothing. The waits are collected for a check."""
    waits: list[float] = []
    mountinfo = world.tmp / "mountinfo"

    def hooks(_paths: object) -> PackageHooks:
        lifecycle = LifecycleHooks(
            exists=lambda p: p.startswith(str(world.tmp)) and os.path.exists(p),
            mountinfo=mountinfo,
            security=SecurityHooks(gpg_extra=lambda: None),
        )
        return PackageHooks(lifecycle=lifecycle, sleep=waits.append)

    monkeypatch.setattr(_context, "package_hooks", hooks)
    return waits


def _run(argv: list[str], *, json_mode: bool = True) -> int:
    """Parse `argv` as the command table would, and run the command."""
    parser = argparse.ArgumentParser(prog="ginn update")
    SPEC.configure(parser)
    args = parser.parse_args(argv)
    return SPEC.run(args, Context(Output(json_mode=json_mode, verbose=False)))


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    out = capsys.readouterr().out
    loaded: dict[str, object] = json.loads(out)
    return loaded


@pytest.mark.fr("IR-010")
def test_the_command_is_named_update_with_an_example() -> None:
    assert SPEC.name == "update"
    assert SPEC.example == "ginn update arch --json"


@pytest.mark.fr("IR-011")
def test_update_with_neither_a_realm_nor_all_nor_check_is_a_usage_error() -> None:
    with pytest.raises(GinnError) as caught:
        _run([])
    assert caught.value.code == "GINN-E-USAGE"
    assert caught.value.exit_code == 2


@pytest.mark.fr("IR-010")
def test_update_with_both_a_realm_and_all_is_a_usage_error(world: World) -> None:
    with pytest.raises(GinnError) as caught:
        _run(["arch", "--all"])
    assert caught.value.code == "GINN-E-USAGE"


@pytest.mark.fr("FR-023")
def test_update_one_realm_prints_the_result_and_the_disk_precheck_note(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert _run(["arch"]) == 0
    out = _json(capsys)
    assert out == {
        "realms": [
            {
                "realm": "arch",
                "status": "updated",
                "state": "running",
                "code": None,
                "message": None,
                "disk_precheck": DISK_PRECHECK_SKIPPED,
            }
        ]
    }
    upgrade = tuple(load("arch").argv(Verb.UPGRADE))
    assert upgrade in [tuple(call.argv) for call in world.fake.exec_calls]


@pytest.mark.fr("FR-023")
def test_update_in_text_mode_names_the_realm_and_its_state(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert _run(["arch"], json_mode=False) == 0
    assert capsys.readouterr().out == "arch: updated, now running\n"


@pytest.mark.fr("FR-023")
@pytest.mark.fr("FR-120")
def test_update_all_writes_every_row_then_raises_the_first_failure(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[tuple(load("arch").argv(Verb.UPGRADE))] = ExecResult(
        1, "", "error: failed to commit\n"
    )
    with pytest.raises(GinnError) as caught:
        _run(["--all"])
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert caught.value.exit_code == 1
    rows = _json(capsys)["realms"]
    assert isinstance(rows, list)
    assert [(row["realm"], row["status"]) for row in rows] == [
        ("arch", "error"),
        ("deb", "updated"),
    ]
    assert rows[0]["code"] == "GINN-E-PKG-MANAGER"


@pytest.mark.fr("FR-120")
def test_a_network_failure_retries_with_the_delays_and_names_network(
    world: World, _no_real_sleep: list[float]
) -> None:
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[tuple(load("debian").argv(Verb.UPGRADE))] = ExecResult(
        100, "", DEBIAN_NETWORK
    )
    with pytest.raises(GinnError) as caught:
        _run(["deb"])
    assert caught.value.code == "GINN-E-NETWORK"
    assert caught.value.exit_code == 69
    assert _no_real_sleep == [5.0, 15.0, 45.0]


@pytest.mark.fr("FR-024")
def test_check_all_reports_updates_and_the_count_as_json(
    world: World, capsys: pytest.CaptureFixture[str], scratch: str
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    make_realm(world, "deb", RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[("pacman", "--query", "--upgrades", "--dbpath", scratch)] = ExecResult(
        0, "bash 5.2-1 -> 5.2-2\n", ""
    )
    world.fake.exec_results[("apt", "list", "--upgradable")] = ExecResult(0, "Listing...\n", "")
    assert _run(["--check"]) == 0
    rows = _json(capsys)["realms"]
    assert isinstance(rows, list)
    by_realm = {row["realm"]: row for row in rows}
    assert by_realm["arch"]["status"] == "updates"
    assert by_realm["arch"]["updates"] is True
    assert by_realm["arch"]["count"] == 1
    assert by_realm["deb"]["status"] == "none"
    assert by_realm["deb"]["updates"] is False


@pytest.mark.fr("FR-024")
def test_check_of_a_stopped_realm_says_it_is_not_checked(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    assert _run(["--check", "arch"], json_mode=False) == 0
    assert capsys.readouterr().out == "arch: not checked: the realm is stopped; start it to check\n"


@pytest.mark.fr("FR-024")
def test_check_writes_no_audit_entry_and_starts_nothing(world: World) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    assert _run(["--check"]) == 0
    assert world.fake.ops() == []
