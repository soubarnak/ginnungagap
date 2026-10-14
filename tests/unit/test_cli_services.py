# SPDX-License-Identifier: Apache-2.0
"""`ginn services <realm> on|off` (FR-083, IR-010), with the operation replaced.

The command turns its arguments into a call of ops/service.py `set_services_mode`, and it prints the
mode. A change is recorded, and a realm that is neither stopped nor running is refused with its own
state code before anything is written.
The host world (tests/unit/conftest.py) pins the paths, so nothing is written.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.cli.commands import services as services_cmd
from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.domain.permissions import Permissions
from ginn.errors import GinnError, warn
from ginn.store.records import RealmRecord
from tests.unit.realm_cli_support import World

pytestmark = [pytest.mark.fr("FR-083")]


def _record(services: bool) -> RealmRecord:
    from datetime import UTC, datetime

    t0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    settings = RealmSettings(
        name=RealmName("lab"),
        distro=DistroId("debian"),
        tier=Tier.BASIC,
        services=services,
        autostart=False,
        everything=False,
    )
    return RealmRecord(
        settings=settings,
        state=RealmState.STOPPED,
        previous_state=None,
        image_ref="registry.example/debian@sha256:" + "ab" * 32,
        image_digest="sha256:" + "ab" * 32,
        container_id="c" * 64,
        generation=1,
        created_at=t0,
        updated_at=t0,
        restart_required=False,
        permissions=Permissions(),
    )


def _recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Replace set_services_mode with a recorder that answers with the requested mode."""
    calls: list[tuple[str, bool]] = []

    def fake_set_services_mode(
        env: Any, name: RealmName, *, on: bool, warnings: list[Any] | None = None
    ) -> RealmRecord:
        calls.append((str(name), on))
        return _record(on)

    monkeypatch.setattr(services_cmd, "set_services_mode", fake_set_services_mode)
    return calls


@pytest.mark.parametrize(("mode", "on"), [("on", True), ("off", False)])
def test_each_mode_reaches_the_operation(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    on: bool,
) -> None:
    calls = _recorded(monkeypatch)

    assert main(["services", "lab", mode]) == 0

    assert calls == [("lab", on)]
    assert f"services are {mode}" in capsys.readouterr().out


def test_an_unknown_mode_is_an_argument_error_before_anything_runs(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recorded(monkeypatch)

    assert main(["services", "lab", "maybe"]) == 2
    assert calls == []


def test_a_refused_change_prints_its_code_and_exits_with_the_catalogue_status(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(
        env: Any, name: RealmName, *, on: bool, warnings: list[Any] | None = None
    ) -> RealmRecord:
        raise GinnError(
            "GINN-E-REALM-STATE",
            "realm 'lab' is failed; a change of services mode needs it stopped or running",
            "",
            4,
        )

    monkeypatch.setattr(services_cmd, "set_services_mode", refuse)

    assert main(["services", "lab", "off"]) == 4
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-REALM-STATE")


def test_a_change_warns_that_the_next_start_rebuilds(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A services change is a create-time change: the notice says that the next start rebuilds
    the realm, and the command prints it (FR-083, CL-19)."""

    def fake_set_services_mode(
        env: Any, name: RealmName, *, on: bool, warnings: list[Any] | None = None
    ) -> RealmRecord:
        assert warnings is not None
        warnings.append(
            warn("GINN-W-REBUILD-PENDING", realm="lab", change="the services mode changed")
        )
        return _record(on)

    monkeypatch.setattr(services_cmd, "set_services_mode", fake_set_services_mode)
    assert main(["services", "lab", "on"]) == 0
    err = capsys.readouterr().err
    assert err.startswith("GINN-W-REBUILD-PENDING: ")
    assert "because the services mode changed" in err


def test_json_output_carries_the_name_and_the_mode(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _recorded(monkeypatch)

    assert main(["services", "lab", "on", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"name": "lab", "services": True}


@pytest.mark.fr("IR-010")
@pytest.mark.parametrize("verb", ["services", "autostart", "update"])
def test_the_verb_is_in_the_command_table_and_its_help_parses(
    capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    """The verbs of IR-010 that this milestone adds or that T8.4 committed are registered."""
    from ginn.cli import commands as registry

    assert verb in {spec.name for spec in registry.COMMANDS}
    assert main([verb, "--help"]) == 0
    assert "usage" in capsys.readouterr().out
