# SPDX-License-Identifier: Apache-2.0
"""`ginn create` through the CLI entry point (FR-010, FR-014, IR-010, IR-011).

The real ops/create.py runs on FakePodman, with `integrate` False (the `world` fixture), so no
package step runs. The refusals of options that are not built yet are checked before any write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.cli.commands import create as create_cmd
from ginn.domain import RealmName, RealmState, Tier
from ginn.errors import warn
from ginn.ops.create import CreateRequest, CreateResult
from ginn.store.records import RealmStore
from tests.unit.realm_cli_support import World, record_of


def _recorded(monkeypatch: pytest.MonkeyPatch) -> list[CreateRequest]:
    """Replace create_realm with a recorder that returns a stopped record (no ops run)."""
    requests: list[CreateRequest] = []

    def fake_create(env: Any, podman: Any, request: CreateRequest, *, hooks: Any) -> CreateResult:
        requests.append(request)
        record = record_of(request.name.value, RealmState.STOPPED, "c" * 64)
        return CreateResult(record=record, warnings=[], locale_note=None)

    monkeypatch.setattr(create_cmd, "create_realm", fake_create)
    return requests


@pytest.mark.fr("FR-010")
def test_defaults_are_basic_tier_services_off_and_the_distro_name(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = _recorded(monkeypatch)
    assert main(["create", "debian"]) == 0
    (request,) = requests
    assert request.name == RealmName("debian")
    assert request.tier is Tier.BASIC
    assert request.services is False
    assert request.everything is False
    assert tuple(request.with_packages) == ()
    assert capsys.readouterr().out == "realm debian created: state stopped\n"


@pytest.mark.fr("FR-060")
@pytest.mark.parametrize(
    ("option", "tier"), [("development", Tier.DEVELOPMENT), ("custom", Tier.CUSTOM)]
)
def test_the_tier_flag_reaches_the_create(
    world: World, monkeypatch: pytest.MonkeyPatch, option: str, tier: Tier
) -> None:
    requests = _recorded(monkeypatch)
    assert main(["create", "debian", "--tier", option]) == 0
    (request,) = requests
    assert request.tier is tier


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-015")
def test_name_and_with_packages_are_taken_from_the_flags(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = _recorded(monkeypatch)
    argv = ["create", "debian", "--name", "debian-dev", "--with", "htop", "vim", "--with", "make"]
    assert main(argv) == 0
    (request,) = requests
    assert request.name == RealmName("debian-dev")
    assert tuple(request.with_packages) == ("htop", "vim", "make")
    assert "realm debian-dev created" in capsys.readouterr().out


@pytest.mark.fr("FR-010")
@pytest.mark.fr("IR-011")
def test_a_bad_realm_name_is_an_argument_error_before_anything_runs(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = _recorded(monkeypatch)
    assert main(["create", "arch", "--name", "Bad_Name"]) == 2
    assert requests == []
    assert "GINN-E-NAME-INVALID" in capsys.readouterr().err


@pytest.mark.fr("IR-011")
@pytest.mark.parametrize(
    ("argv", "feature", "milestone"),
    [
        (["create", "arch", "--services", "on"], "create of a services-on realm", "M7"),
        (["create", "arch", "--resume"], "create --resume", "M8"),
        (["create", "kali", "--everything"], "create --everything", "M8"),
        (["create", "kali"], "create of a kali realm", "M10"),
    ],
)
def test_options_not_built_yet_are_refused_with_their_milestone(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    feature: str,
    milestone: str,
) -> None:
    requests = _recorded(monkeypatch)
    assert main(argv) == 2
    assert requests == []
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-NOT-SUPPORTED-YET: ")
    assert f"{feature} arrives in {milestone}" in err


@pytest.mark.fr("FR-014")
def test_everything_on_a_non_kali_distro_is_option_invalid(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = _recorded(monkeypatch)
    assert main(["create", "debian", "--everything"]) == 2
    assert requests == []
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-OPTION-INVALID: ")
    assert "--everything" in err


@pytest.mark.fr("IR-011")
def test_a_refusal_from_the_spec_is_mapped_to_not_supported_yet(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(env: Any, podman: Any, request: CreateRequest, *, hooks: Any) -> CreateResult:
        raise NotImplementedError("kali needs pasta and NET_RAW, arriving in M10 (T10.1)")

    monkeypatch.setattr(create_cmd, "create_realm", refuse)
    assert main(["create", "debian"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-NOT-SUPPORTED-YET: ")
    assert "kali needs pasta" in err


@pytest.mark.fr("FR-010")
def test_an_unknown_distro_is_refused_before_any_write(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["create", "nosuchdistro"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-USAGE: unknown distribution 'nosuchdistro'")
    assert world.fake.containers == {}
    assert RealmStore(world.paths).list_names() == []


@pytest.mark.fr("FR-010")
def test_json_result_carries_the_state_and_the_warnings(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def with_warning(env: Any, podman: Any, request: CreateRequest, *, hooks: Any) -> CreateResult:
        record = record_of(request.name.value, RealmState.STOPPED, "c" * 64)
        notice = warn("GINN-W-HOST-CHANGED", paths="/etc/hosts")
        return CreateResult(record=record, warnings=[notice], locale_note=None)

    monkeypatch.setattr(create_cmd, "create_realm", with_warning)
    assert main(["--json", "create", "debian"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "stopped"
    assert payload["services"] is False
    assert payload["tier"] == "basic"
    (notice,) = payload["warnings"]
    assert notice["code"] == "GINN-W-HOST-CHANGED"
    assert "/etc/hosts" in notice["message"]


@pytest.mark.fr("FR-013")
def test_a_locale_fallback_is_printed_as_a_note(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def with_note(env: Any, podman: Any, request: CreateRequest, *, hooks: Any) -> CreateResult:
        record = record_of(request.name.value, RealmState.STOPPED, "c" * 64)
        return CreateResult(record=record, warnings=[], locale_note="LANG unusable, using C.UTF-8")

    monkeypatch.setattr(create_cmd, "create_realm", with_note)
    assert main(["create", "debian"]) == 0
    assert "note: LANG unusable, using C.UTF-8" in capsys.readouterr().out


@pytest.mark.fr("FR-010")
def test_a_real_create_on_the_fake_leaves_a_stopped_basic_realm(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["create", "debian", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "name": "debian",
        "distro": "debian",
        "state": "stopped",
        "tier": "basic",
        "services": False,
        "generation": 1,
        "image_digest": payload["image_digest"],
        "locale_note": payload["locale_note"],
        "gpu_note": payload["gpu_note"],
    }
    record = RealmStore(world.paths).load(RealmName("debian"))
    assert record.state is RealmState.STOPPED
    assert record.container_id is not None


@pytest.mark.fr("FR-010")
def test_the_default_hooks_run_the_package_steps_and_no_test_seam(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production seams: the package steps run (integrate True), no helper override."""
    monkeypatch.setattr(_context, "seams", _context.Seams)
    hooks = _context.create_hooks()
    assert hooks.integrate is True
    assert hooks.helper is None
    assert hooks.mountinfo == Path("/proc/self/mountinfo")
