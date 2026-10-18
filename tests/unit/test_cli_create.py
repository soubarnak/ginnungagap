# SPDX-License-Identifier: Apache-2.0
"""`ginn create` through the CLI entry point (FR-010, FR-014, IR-010, IR-011).

The real ops/create.py runs on FakePodman, with `integrate` False (the `world` fixture), so no
package step runs. The refusals of options that are not built yet are checked before any write.
"""

from __future__ import annotations

import io
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.cli.commands import create as create_cmd
from ginn.distros import Verb
from ginn.distros import load as load_distro
from ginn.domain import RealmName, RealmState, Tier
from ginn.errors import warn
from ginn.ops.create import CreateRequest, CreateResult, SizeEstimate
from ginn.ops.disk import estimate_required
from ginn.ops.provision import services_off_required_bytes, storage_peak_for
from ginn.podman.models import ContainerState, ExecResult
from ginn.store.records import RealmStore
from ginn.trust import manifest as trust
from tests.unit.realm_cli_support import World, record_of, seed_killed_estimate


def _recorded_result(request: CreateRequest) -> CreateResult:
    """The result of a create that stopped its realm, with no notice and no estimate."""
    record = record_of(request.name.value, RealmState.STOPPED, "c" * 64)
    return CreateResult(record=record, warnings=[], locale_note=None)


def _recorded(monkeypatch: pytest.MonkeyPatch) -> list[CreateRequest]:
    """Replace create_realm with a recorder that returns a stopped record (no ops run)."""
    requests: list[CreateRequest] = []

    def fake_create(env: Any, podman: Any, request: CreateRequest, *, hooks: Any) -> CreateResult:
        requests.append(request)
        return _recorded_result(request)

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


def _resumed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace resume_create with a recorder that returns a stopped record (no ops run)."""
    calls: list[dict[str, Any]] = []

    def fake_resume(
        env: Any,
        podman: Any,
        name: RealmName,
        *,
        uid: int,
        gid: int,
        host_home: Any,
        host_env: Any,
        hooks: Any,
    ) -> CreateResult:
        calls.append({"name": name, "hooks": hooks})
        record = record_of(name.value, RealmState.STOPPED, "c" * 64)
        return CreateResult(record=record, warnings=[], locale_note=None, resumed=True)

    monkeypatch.setattr(create_cmd, "resume_create", fake_resume)
    return calls


@pytest.mark.fr("FR-122")
def test_resume_names_the_realm_and_reports_it_resumed(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _resumed(monkeypatch)
    assert main(["create", "--resume", "arch"]) == 0
    assert [call["name"] for call in calls] == [RealmName("arch")]
    assert "realm arch resumed: state stopped" in capsys.readouterr().out


@pytest.mark.fr("FR-122")
def test_resume_in_json_has_the_create_payload(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _resumed(monkeypatch)
    assert main(["--json", "create", "--resume", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "arch"
    assert payload["state"] == "stopped"
    assert payload["resumed"] is True


@pytest.mark.fr("FR-122")
@pytest.mark.fr("IR-011")
@pytest.mark.parametrize(
    "extra",
    [
        ["--name", "x"],
        ["--tier", "basic"],
        ["--services", "off"],
        ["--with", "htop"],
        ["--everything"],
    ],
)
def test_resume_takes_only_the_realm_name_and_yes(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
) -> None:
    calls = _resumed(monkeypatch)
    assert main(["create", "--resume", "arch", *extra]) == 2
    assert calls == []
    assert capsys.readouterr().err.startswith("GINN-E-USAGE: --resume takes only the realm name")


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-014")
def test_resume_answers_the_everything_prompt_with_yes_or_not_at_all_without_a_terminal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _resumed(monkeypatch)
    assert main(["create", "--resume", "kali", "--yes"]) == 0
    confirm = calls[-1]["hooks"].confirm
    assert confirm is not None and confirm("prompt") is True
    assert main(["--json", "create", "--resume", "kali"]) == 0
    assert calls[-1]["hooks"].confirm is None


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


@pytest.mark.fr("FR-014")
def test_everything_without_a_terminal_or_yes_is_refused_before_any_write(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = _recorded(monkeypatch)
    monkeypatch.setattr(create_cmd, "_can_prompt", lambda: False)
    assert main(["create", "kali", "--everything"]) == 2
    assert requests == []
    assert world.fake.containers == {}
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-USAGE: ")
    assert "--yes" in err


@pytest.mark.fr("FR-014")
def test_everything_with_json_needs_yes_because_a_json_run_does_not_prompt(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = _recorded(monkeypatch)
    monkeypatch.setattr(create_cmd, "_can_prompt", lambda: True)
    assert main(["--json", "create", "kali", "--everything"]) == 2
    assert requests == []
    assert "a JSON run does not prompt" in capsys.readouterr().err


@pytest.mark.fr("FR-014")
def test_everything_with_yes_is_confirmed_without_a_prompt(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests: list[CreateRequest] = []
    hooks_seen: list[Any] = []

    def capture(env: Any, podman: Any, request: CreateRequest, *, hooks: Any) -> CreateResult:
        requests.append(request)
        hooks_seen.append(hooks)
        return _recorded_result(request)

    monkeypatch.setattr(create_cmd, "create_realm", capture)
    monkeypatch.setattr(create_cmd, "_can_prompt", lambda: False)
    assert main(["create", "kali", "--everything", "--yes"]) == 0
    (request,) = requests
    assert request.everything is True
    assert hooks_seen[0].confirm("any prompt") is True
    assert capsys.readouterr().err == ""


@pytest.mark.fr("FR-014")
def test_the_estimate_of_a_confirmed_everything_install_is_shown_in_text_and_json(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def with_estimate(env: Any, podman: Any, request: CreateRequest, *, hooks: Any) -> CreateResult:
        estimate = SizeEstimate(packages=3, download_bytes=6000, installed_bytes=6144)
        return replace(_recorded_result(request), estimate=estimate)

    monkeypatch.setattr(create_cmd, "create_realm", with_estimate)
    assert main(["create", "kali", "--everything", "--yes"]) == 0
    assert "everything: 3 packages, download 5.9 KiB, installed 6.0 KiB" in capsys.readouterr().out
    assert main(["--json", "create", "kali", "--everything", "--yes"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["everything"] == {"packages": 3, "download_bytes": 6000, "installed_bytes": 6144}


@pytest.mark.fr("FR-014")
@pytest.mark.parametrize(
    ("answer", "confirmed"), [("y\n", True), ("yes\n", True), ("n\n", False), ("", False)]
)
def test_the_typed_answer_at_the_everything_prompt_decides_the_install(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    confirmed: bool,
) -> None:
    requests: list[CreateRequest] = []
    hooks_seen: list[Any] = []

    def capture(env: Any, podman: Any, request: CreateRequest, *, hooks: Any) -> CreateResult:
        requests.append(request)
        hooks_seen.append(hooks)
        return _recorded_result(request)

    monkeypatch.setattr(create_cmd, "create_realm", capture)
    monkeypatch.setattr(create_cmd, "_can_prompt", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert main(["create", "kali", "--everything"]) == 0
    assert len(requests) == 1
    assert hooks_seen[0].confirm("Install kali-linux-everything?") is confirmed
    assert "Install kali-linux-everything?" in capsys.readouterr().err


@pytest.mark.fr("FR-026")
@pytest.mark.fr("FR-027")
def test_a_kali_realm_is_created_like_any_other_distribution(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = _recorded(monkeypatch)
    assert main(["create", "kali"]) == 0
    (request,) = requests
    assert request.distro == "kali"
    assert request.everything is False
    assert capsys.readouterr().out == "realm kali created: state stopped\n"


@pytest.mark.fr("FR-026")
@pytest.mark.fr("FR-027")
@pytest.mark.fr("FR-014")
def test_the_kali_help_states_the_limits_of_oos_9(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["create", "--help"]) == 0
    out = " ".join(capsys.readouterr().out.split())  # argparse wraps the epilog
    assert "kali" in out
    assert "Wi-Fi monitor mode" in out
    assert "raw-socket or ARP scans of physical networks" in out
    assert "authorized testing" in out
    assert "--everything" in out and "--yes" in out


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
@pytest.mark.fr("FR-080")
def test_services_on_is_accepted_and_leaves_the_realm_stopped_with_the_pending_notice(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_context, "ginn_program", lambda paths=None: "/usr/bin/ginn")
    assert main(["create", "debian", "--services", "on", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "stopped"
    assert payload["services"] is True
    (notice,) = payload["warnings"]
    assert notice["code"] == "GINN-W-SERVICES-PENDING"
    record = RealmStore(world.paths).load(RealmName("debian"))
    assert record.state is RealmState.STOPPED
    assert record.settings.services is True
    assert [c.state for c in world.fake.containers.values()] == [ContainerState.CREATED]
    # Only the provisioning container is started, and it is gone (ADR-015 Decision 2). The realm
    # container, which the record names, is never started (ADR-003).
    started = [call.args[0] for call in world.fake.calls if call.op == "start"]
    assert record.container_id not in started
    assert started
    assert payload["notes"] == []


@pytest.mark.fr("FR-010")
def test_a_services_off_create_lists_no_notes_in_its_json(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "create", "debian"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["notes"] == []


@pytest.mark.fr("FR-080")
def test_a_stage_note_is_printed_as_a_note_line_in_text_mode(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR-015 Decision 2h: a stage removal that fails after the commit is a note, and the create
    still finishes. Text mode prints it as a `note:` line, as it prints the locale and GPU notes."""
    monkeypatch.setattr(_context, "ginn_program", lambda paths=None: "/usr/bin/ginn")
    world.fake.fail_ops["rm"] = "GINN-E-INTERNAL"
    assert main(["create", "debian", "--services", "on"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "realm debian created: state stopped",
        "note: reset-removed failed with GINN-E-INTERNAL",
    ]


@pytest.mark.fr("FR-080")
def test_a_stage_note_is_listed_under_notes_in_the_json(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_context, "ginn_program", lambda paths=None: "/usr/bin/ginn")
    world.fake.fail_ops["rm"] = "GINN-E-INTERNAL"
    assert main(["--json", "create", "debian", "--services", "on"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["notes"] == ["reset-removed failed with GINN-E-INTERNAL"]


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-080")
def test_services_on_with_packages_is_refused_by_the_create_before_any_write(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["create", "debian", "--services", "on", "--with", "htop"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-NOT-SUPPORTED-YET: ")
    assert (
        "--with packages on a services-on create (the CLI cannot start a services-on container, "
        "ADR-003) arrives in ginn install, after the realm's first supervised start" in err
    )
    assert world.fake.containers == {}
    assert RealmStore(world.paths).list_names() == []


@pytest.mark.fr("FR-010")
def test_a_real_create_on_the_fake_leaves_a_stopped_basic_realm(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["create", "debian", "--json"]) == 0
    captured = capsys.readouterr()
    progress = [
        json.loads(line)["progress"]
        for line in captured.err.splitlines()
        if line.startswith('{"progress"')
    ]
    [space] = [item for item in progress if item["step"] == "space-checked"]
    entry = trust.load().entry("debian")
    debian = load_distro("debian")
    services_off = services_off_required_bytes(
        debian.base_layer_allocated_bytes,
        debian.stage_layer_allocated_bytes,
        debian.package_cache_peak_bytes,
        download_bytes=entry.compressed_size_bytes,
        base_pulled=True,
    )
    assert space["required_bytes"] == services_off
    assert space["free_bytes"] >= space["required_bytes"]
    assert space["reason"] is None
    payload = json.loads(captured.out)
    assert payload == {
        "name": "debian",
        "distro": "debian",
        "state": "stopped",
        "tier": "basic",
        "services": False,
        "generation": 1,
        "image_digest": payload["image_digest"],
        "resumed": False,
        "locale_note": payload["locale_note"],
        "gpu_note": payload["gpu_note"],
        "notes": [],
        "everything": None,
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


KALI_LISTING = "kali-linux-everything\n  Depends: alpha\nalpha\n  Depends: beta\nbeta\n"
KALI_SHOW = (
    "Package: kali-linux-everything\nVersion: 2026.3\nSize: 1000\nInstalled-Size: 1\n\n"
    "Package: alpha\nVersion: 1.0\nSize: 2000\nInstalled-Size: 2\n\n"
    "Package: beta\nVersion: 1.0\nSize: 3000\nInstalled-Size: 3\n"
)
"""The closure of the everything metapackage, and its sizes: 6000 bytes to download and 6144
installed (`Installed-Size` is in KiB)."""


def _kali_answers(world: World) -> None:
    """The package-list answers that the Kali closure reads inside the throwaway container."""
    kali = load_distro("kali")
    names = ["kali-linux-everything", "alpha", "beta"]
    resolve = tuple(kali.argv(Verb.RESOLVE, ["kali-linux-everything"]))
    world.fake.exec_results[resolve] = ExecResult(0, KALI_LISTING, "")
    world.fake.exec_results[tuple(kali.argv(Verb.ESTIMATE, names))] = ExecResult(0, KALI_SHOW, "")


def _kali_base_peak() -> int:
    """The services-off storage peak of the kali base, pulled (T7.35), as the estimate checks it."""
    entry = trust.load().entry("kali")
    return storage_peak_for(
        load_distro("kali"),
        services=False,
        download_bytes=entry.compressed_size_bytes,
        base_pulled=True,
    )


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-121")
def test_the_estimate_prints_the_sizes_as_json_and_leaves_no_realm_or_container(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _kali_answers(world)
    assert main(["--json", "create", "kali", "--everything", "--estimate"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "distro": "kali",
        "metapackages": ["kali-linux-everything"],
        "packages": 3,
        "download_bytes": 6000,
        "installed_bytes": 6144,
        "required_bytes": estimate_required(_kali_base_peak() + 6000 + 6144),
        "base_required_bytes": estimate_required(_kali_base_peak()),
        "everything_required_bytes": estimate_required(6000 + 6144),
        "services": False,
    }
    assert world.fake.containers == {}
    assert world.fake.images == {}
    assert not (world.paths.homes / "kali").exists()
    assert RealmStore(world.paths).exists(RealmName("kali")) is False


@pytest.mark.fr("FR-014")
def test_the_estimate_text_is_human_readable_and_needs_no_terminal(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _kali_answers(world)
    assert main(["create", "kali", "--everything", "--estimate"]) == 0
    out = capsys.readouterr().out
    assert "kali everything: 3 packages, download 5.9 KiB, installed 6.0 KiB" in out
    assert "required free space: " in out and "(estimate x 1.10; services off; base " in out
    assert "estimate only: no realm was made, and nothing was installed" in out


@pytest.mark.fr("FR-014")
@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["create", "kali", "--estimate"], "GINN-E-USAGE"),
        (["create", "debian", "--everything", "--estimate"], "GINN-E-OPTION-INVALID"),
        (["create", "kali", "--everything", "--estimate", "--yes"], "GINN-E-USAGE"),
        (["create", "kali", "--everything", "--estimate", "--with", "htop"], "GINN-E-USAGE"),
        (["create", "kali", "--everything", "--estimate", "--name", "work"], "GINN-E-USAGE"),
        (["create", "kali", "--everything", "--estimate", "--tier", "basic"], "GINN-E-USAGE"),
        (["create", "kali", "--everything", "--estimate", "--services", "on"], "GINN-E-USAGE"),
        (["create", "--resume", "kali", "--estimate"], "GINN-E-USAGE"),
    ],
)
def test_the_estimate_refuses_any_other_option_before_any_podman_call(
    world: World, capsys: pytest.CaptureFixture[str], argv: list[str], code: str
) -> None:
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{code}: ")
    assert world.fake.calls == []
    assert world.fake.containers == {}


@pytest.mark.fr("FR-107")
def test_the_estimate_prints_one_progress_line_per_step_in_json_mode(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _kali_answers(world)
    assert main(["--json", "create", "kali", "--everything", "--estimate"]) == 0
    lines = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith('{"progress"')
    ]
    assert [line["progress"]["step"] for line in lines] == [
        "space-checked",
        "pulled",
        "created",
        "refreshed",
        "estimated",
    ]
    assert lines[1] == {"progress": {"message": "estimate: step pulled done", "step": "pulled"}}
    # T7.35: the space check's line carries its figures; the other lines keep their three keys.
    assert lines[0]["progress"]["required_bytes"] == estimate_required(_kali_base_peak())
    assert isinstance(lines[0]["progress"]["free_bytes"], int)
    assert lines[0]["progress"]["reason"] is None
    assert set(lines[1]["progress"]) == {"step", "message"}


@pytest.mark.fr("FR-006")
@pytest.mark.fr("FR-014")
def test_the_estimate_is_refused_by_a_changed_host_before_any_podman_call(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _kali_answers(world)
    assert main(["--json", "create", "kali", "--everything", "--estimate"]) == 0  # the baseline
    capsys.readouterr()
    (world.host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")
    world.fake.calls.clear()

    assert main(["create", "kali", "--everything", "--estimate"]) == 4
    assert capsys.readouterr().err.startswith("GINN-E-HOST-UNACKED: ")
    assert world.fake.calls == []


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-014")
def test_the_estimate_sweeps_a_killed_estimate_after_its_own_work_and_the_list_does_not(
    world: World,
) -> None:
    _kali_answers(world)
    seed_killed_estimate(world)

    assert main(["list"]) == 0
    assert len(world.fake.containers) == 1  # a read-only command writes no podman state

    assert main(["--json", "create", "kali", "--everything", "--estimate"]) == 0
    assert world.fake.containers == {}  # the sweep runs after the estimate, so its gate comes first
