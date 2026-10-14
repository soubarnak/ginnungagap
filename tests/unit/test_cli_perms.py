# SPDX-License-Identifier: Apache-2.0
"""`ginn perms <realm>` shows, or changes, the Custom toggles and shares (FR-063, DR-004).

The command turns its options into one call of ops/permissions.py `set_permissions`, or, with no
change option, reads the record. The operations are replaced by recorders here, so the tests check
the arguments and the output, and the operations' own tests check the rules.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.cli.commands import perms as perms_cmd
from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.domain.paths import Mode
from ginn.domain.permissions import BASIC_TOGGLES, CustomShare, Permissions, Toggle
from ginn.errors import GinnError
from ginn.store.records import RealmRecord
from tests.unit.realm_cli_support import World

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


def _record(permissions: Permissions | None = None, *, restart: bool = False) -> RealmRecord:
    settings = RealmSettings(
        name=RealmName("lab"),
        distro=DistroId("debian"),
        tier=Tier.CUSTOM,
        services=False,
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
        created_at=T0,
        updated_at=T0,
        restart_required=restart,
        permissions=permissions or Permissions(toggles=BASIC_TOGGLES),
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Recorders for the read and the change. The change returns a record with the new share."""
    recorded: dict[str, list[dict[str, Any]]] = {"read": [], "change": []}

    def read(env: Any, name: RealmName) -> RealmRecord:
        recorded["read"].append({"name": str(name)})
        return _record()

    def change(
        env: Any,
        name: RealmName,
        host: Any,
        *,
        share: Sequence[tuple[str, Mode]] = (),
        unshare: Sequence[str] = (),
        enable: Sequence[str] = (),
        disable: Sequence[str] = (),
        **kwargs: Any,
    ) -> RealmRecord:
        recorded["change"].append(
            {
                "share": list(share),
                "unshare": list(unshare),
                "enable": list(enable),
                "disable": list(disable),
            }
        )
        share_record = CustomShare("/home/alice/Music", "ro")
        return _record(Permissions(toggles=BASIC_TOGGLES, shares=(share_record,)))

    monkeypatch.setattr(perms_cmd, "read_permissions", read)
    monkeypatch.setattr(perms_cmd, "set_permissions", change)
    return recorded


@pytest.mark.fr("FR-063")
def test_no_change_option_shows_the_permissions_and_writes_nothing(
    world: World, calls: dict[str, list[dict[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["perms", "lab"]) == 0
    assert calls["change"] == []
    assert calls["read"] == [{"name": "lab"}]
    out = capsys.readouterr().out
    assert "tier custom" in out
    assert "toggles: " + ", ".join(sorted(t.value for t in BASIC_TOGGLES)) in out


@pytest.mark.fr("FR-063")
def test_share_and_toggle_options_reach_the_operation_with_their_modes(
    world: World, calls: dict[str, list[dict[str, Any]]]
) -> None:
    argv = [
        "perms",
        "lab",
        "--share",
        "/home/alice/Projects",
        "--share-ro",
        "/home/alice/Music",
        "--no-share",
        "/home/alice/Old",
        "--enable",
        "projects",
        "--disable",
        "downloads",
    ]
    assert main(argv) == 0
    (change,) = calls["change"]
    assert change["share"] == [
        ("/home/alice/Projects", "rw"),
        ("/home/alice/Music", "ro"),
    ]
    assert change["unshare"] == ["/home/alice/Old"]
    assert change["enable"] == ["projects"]
    assert change["disable"] == ["downloads"]


@pytest.mark.fr("FR-063")
def test_repeated_options_are_all_kept(
    world: World, calls: dict[str, list[dict[str, Any]]]
) -> None:
    assert main(["perms", "lab", "--enable", "projects", "--enable", "kvm"]) == 0
    (change,) = calls["change"]
    assert change["enable"] == ["projects", "kvm"]


@pytest.mark.fr("FR-063")
def test_a_later_call_without_options_does_not_see_an_earlier_one(
    world: World, calls: dict[str, list[dict[str, Any]]]
) -> None:
    """The options are lists built per call, so one call's shares never leak into the next."""
    assert main(["perms", "lab", "--share", "/home/alice/Music"]) == 0
    assert main(["perms", "lab"]) == 0
    assert len(calls["change"]) == 1
    assert calls["read"] == [{"name": "lab"}]


@pytest.mark.fr("FR-066")
def test_json_output_lists_the_shares_and_the_restart_flag(
    world: World, calls: dict[str, list[dict[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["perms", "lab", "--share-ro", "/home/alice/Music", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["shares"] == [{"path": "/home/alice/Music", "mode": "ro"}]
    assert payload["toggles"] == sorted(t.value for t in BASIC_TOGGLES)
    assert payload["restart_required"] is False


@pytest.mark.fr("FR-063")
@pytest.mark.fr("NFR-SEC-006")
def test_a_refused_share_prints_its_code_and_exits_with_the_catalogue_status(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> RealmRecord:
        raise GinnError("GINN-E-PATH-FORBIDDEN", "path '/etc' may not be shared", "", 2)

    monkeypatch.setattr(perms_cmd, "set_permissions", refuse)
    assert main(["perms", "lab", "--share", "/etc"]) == 2
    assert capsys.readouterr().err.startswith("GINN-E-PATH-FORBIDDEN")


@pytest.mark.fr("FR-063")
def test_a_running_realm_reports_the_restart_the_change_needs(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(perms_cmd, "read_permissions", lambda env, name: _record(restart=True))
    assert main(["perms", "lab"]) == 0
    assert "restart required" in capsys.readouterr().out
    assert Toggle.DOWNLOADS in BASIC_TOGGLES
