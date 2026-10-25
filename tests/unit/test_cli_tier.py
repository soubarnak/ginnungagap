# SPDX-License-Identifier: Apache-2.0
"""`ginn tier <realm> basic|development|custom` (FR-060, FR-066), with the operation replaced.

The command turns its arguments into a call of ops/permissions.py `set_tier`, and prints the tier
and the restart flag. The host world (tests/unit/conftest.py) pins the paths, so nothing is written.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.cli.commands import tier as tier_cmd
from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.domain.permissions import BASIC_TOGGLES, Permissions
from ginn.errors import GinnError, warn
from ginn.store.records import RealmRecord
from tests.unit.realm_cli_support import World

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


def _record(
    tier: Tier, *, restart: bool = False, state: RealmState = RealmState.STOPPED
) -> RealmRecord:
    settings = RealmSettings(
        name=RealmName("lab"),
        distro=DistroId("debian"),
        tier=tier,
        services=False,
        autostart=False,
        everything=False,
    )
    return RealmRecord(
        settings=settings,
        state=state,
        previous_state=None,
        image_ref="registry.example/debian@sha256:" + "ab" * 32,
        image_digest="sha256:" + "ab" * 32,
        container_id="c" * 64,
        generation=1,
        created_at=T0,
        updated_at=T0,
        restart_required=restart,
        permissions=Permissions(toggles=BASIC_TOGGLES) if tier is Tier.CUSTOM else Permissions(),
    )


def _recorded(
    monkeypatch: pytest.MonkeyPatch, result: Callable[[Tier], RealmRecord] | None = None
) -> list[tuple[str, Tier]]:
    """Replace set_tier with a recorder that returns a record of the requested tier."""
    calls: list[tuple[str, Tier]] = []

    def fake_set_tier(env: Any, name: RealmName, tier: Tier, **kwargs: Any) -> RealmRecord:
        calls.append((str(name), tier))
        return result(tier) if result is not None else _record(tier)

    monkeypatch.setattr(tier_cmd, "set_tier", fake_set_tier)
    return calls


@pytest.mark.fr("FR-060")
@pytest.mark.parametrize("option", ["basic", "development", "custom"])
def test_each_tier_name_reaches_the_operation(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], option: str
) -> None:
    calls = _recorded(monkeypatch)
    assert main(["tier", "lab", option]) == 0
    assert calls == [("lab", Tier.parse(option))]
    out = capsys.readouterr().out
    assert f"tier {option}" in out


@pytest.mark.fr("FR-060")
def test_an_unknown_tier_is_an_argument_error_before_anything_runs(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recorded(monkeypatch)
    assert main(["tier", "lab", "wizard"]) == 2
    assert calls == []


@pytest.mark.fr("FR-066")
def test_a_running_realm_reports_that_a_restart_is_required(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _recorded(
        monkeypatch,
        lambda tier: _record(tier, restart=True, state=RealmState.RUNNING),
    )
    assert main(["tier", "lab", "custom"]) == 0
    out = capsys.readouterr().out
    assert "restart required" in out
    assert "ginn stop lab" in out and "ginn start lab" in out


@pytest.mark.fr("FR-066")
def test_a_change_that_needs_a_new_container_warns_that_the_next_start_rebuilds(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The notice is written at change time, so the rebuild at the next start is not a surprise
    (CL-19). The operation appends it to `warnings`, and the command prints it."""

    def fake_set_tier(env: Any, name: RealmName, tier: Tier, **kwargs: Any) -> RealmRecord:
        kwargs["warnings"].append(
            warn("GINN-W-REBUILD-PENDING", realm="lab", change="the tier changed")
        )
        return _record(tier, restart=True)

    monkeypatch.setattr(tier_cmd, "set_tier", fake_set_tier)
    assert main(["tier", "lab", "development"]) == 0
    err = capsys.readouterr().err
    assert err.startswith("GINN-W-REBUILD-PENDING: ")
    assert "the next start rebuilds it into a new container, because the tier changed" in err


@pytest.mark.fr("FR-066")
def test_json_output_carries_the_tier_the_toggles_and_the_flag(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    _recorded(monkeypatch)
    assert main(["tier", "lab", "custom", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tier"] == "custom"
    assert payload["toggles"] == sorted(t.value for t in BASIC_TOGGLES)
    assert payload["shares"] == []
    assert payload["restart_required"] is False


@pytest.mark.fr("NFR-SEC-002")
def test_a_refused_change_prints_its_code_and_exits_with_the_catalogue_status(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(env: Any, name: RealmName, tier: Tier, **kwargs: Any) -> RealmRecord:
        raise GinnError(
            "GINN-E-REALM-STATE", "realm 'lab' is failed; a tier needs it stopped", "", 4
        )

    monkeypatch.setattr(tier_cmd, "set_tier", refuse)
    assert main(["tier", "lab", "development"]) == 4
    assert capsys.readouterr().err.startswith("GINN-E-REALM-STATE")


@pytest.mark.fr("FR-060")
def test_without_a_tier_the_current_permissions_are_shown_and_nothing_is_written(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _recorded(monkeypatch)
    monkeypatch.setattr(tier_cmd, "read_permissions", lambda env, name: _record(Tier.CUSTOM))
    assert main(["tier", "lab"]) == 0
    assert calls == []
    assert "tier custom" in capsys.readouterr().out
