# SPDX-License-Identifier: Apache-2.0
"""`ginn autostart <realm> on|off` (FR-084, DR-012), with the operation replaced.

The command turns its arguments into a call of ops/service.py `set_autostart` and prints the flag.
A services-off realm is refused by the settings rule (DR-012) inside the operation. The host world
(tests/unit/conftest.py) pins the paths, so nothing is written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.cli.commands import autostart as autostart_cmd
from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.domain.permissions import Permissions
from ginn.errors import GinnError
from ginn.store.records import RealmRecord
from tests.unit.realm_cli_support import World

pytestmark = [pytest.mark.fr("FR-084"), pytest.mark.fr("DR-012")]

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _record(on: bool) -> RealmRecord:
    settings = RealmSettings(
        name=RealmName("lab"),
        distro=DistroId("debian"),
        tier=Tier.BASIC,
        services=True,
        autostart=on,
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
        created_at=T0,
        updated_at=T0,
        restart_required=False,
        permissions=Permissions(),
    )


def _recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Replace set_autostart with a recorder that answers with the requested flag."""
    calls: list[tuple[str, bool]] = []

    def fake_set_autostart(env: Any, name: RealmName, *, on: bool) -> RealmRecord:
        calls.append((str(name), on))
        return _record(on)

    monkeypatch.setattr(autostart_cmd, "set_autostart", fake_set_autostart)
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

    assert main(["autostart", "lab", mode]) == 0

    assert calls == [("lab", on)]
    assert f"autostart is {mode}" in capsys.readouterr().out


def test_an_unknown_mode_is_an_argument_error_before_anything_runs(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recorded(monkeypatch)

    assert main(["autostart", "lab", "sometimes"]) == 2
    assert calls == []


def test_autostart_on_a_services_off_realm_is_refused_with_the_settings_rule(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(env: Any, name: RealmName, *, on: bool) -> RealmRecord:
        raise GinnError(
            "GINN-E-OPTION-INVALID",
            "option '--autostart without services' is invalid: "
            "autostart needs services on (DR-012)",
            "",
            2,
        )

    monkeypatch.setattr(autostart_cmd, "set_autostart", refuse)

    assert main(["autostart", "lab", "on"]) == 2
    assert "DR-012" in capsys.readouterr().err


def test_json_output_carries_the_name_services_and_the_flag(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _recorded(monkeypatch)

    assert main(["autostart", "lab", "on", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"name": "lab", "services": True, "autostart": True}
