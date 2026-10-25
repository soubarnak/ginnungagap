# SPDX-License-Identifier: Apache-2.0
"""Control Plane viewmodels: dashboard, create wizard, permissions editor and host health.

These tests run without a display. The viewmodels import no GTK module, so the checks here are
the ones the SRS states as viewmodel-level acceptance: FR-101 AC-1 and AC-2, FR-103 AC-1 and
FR-104 AC-1. Argv outputs are also parsed by the real CLI parser, so the forms cannot drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ginn.cli.main import build_parser
from ginn.distros import available
from ginn.domain import Tier
from ginn.domain.names import NAME_CODE
from ginn.domain.packages import PACKAGE_CODE
from ginn.domain.paths import PATH_CODE
from ginn.domain.permissions import Toggle, effective_toggles
from ginn.gui.backend import (
    HOST_CHANGED_CODE,
    CliError,
    CliResult,
    CliWarning,
    DiskUse,
    DoctorCheck,
    DoctorReport,
    IntegrityState,
    PermissionsView,
    RealmListing,
    RealmView,
    ShareView,
)
from ginn.gui.viewmodels.dashboard import Badge, DashboardViewModel, badge_for
from ginn.gui.viewmodels.health import ACKNOWLEDGE_ARGS, HealthBadge, HealthViewModel
from ginn.gui.viewmodels.labels import failure_of, format_bytes
from ginn.gui.viewmodels.permissions import PermissionsEditor, Share
from ginn.gui.viewmodels.progress import Phase, ProgressModel
from ginn.gui.viewmodels.wizard import (
    SERVICES_CONFLICT_TEXT,
    CreateWizard,
    EverythingSizes,
    sizes_of,
)


def _realm(name: str, **fields: object) -> RealmView:
    base: dict[str, object] = {
        "name": name,
        "state": "stopped",
        "container_state": None,
        "state_mismatch": False,
        "distro": name,
        "tier": "basic",
        "services": False,
        "generation": 1,
        "restart_required": False,
        "share_skipped": None,
        "disk": None,
        "exported_apps": None,
        "error": None,
    }
    base.update(fields)
    return RealmView(**base)  # type: ignore[arg-type]


def _ok() -> CliResult:
    return CliResult(
        argv=("ginn", "--json", "list"),
        exit_code=0,
        payload={"realms": []},
        error=None,
        warnings=(),
        stderr="",
        cancelled=False,
    )


def _failed(code: str | None, exit_code: int) -> CliResult:
    error = (
        None
        if code is None
        else CliError(
            code=code,
            message="listing failed",
            remedy="run ginn doctor",
            exit_code=exit_code,
            details={},
            notes=(),
        )
    )
    return CliResult(
        argv=("ginn", "--json", "list"),
        exit_code=exit_code,
        payload=None,
        error=error,
        warnings=(),
        stderr="",
        cancelled=False,
    )


def _listing(request_id: int, *realms: RealmView) -> RealmListing:
    return RealmListing(request_id=request_id, result=_ok(), realms=tuple(realms))


def _editor(
    tmp_path: Path,
    *,
    tier: str = "custom",
    toggles: tuple[str, ...] = (),
    shares: tuple[ShareView, ...] = (),
) -> PermissionsEditor:
    view = PermissionsView(
        name="lab",
        tier=tier,
        toggles=toggles,
        shares=shares,
        restart_required=False,
    )
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True, exist_ok=True)
    return PermissionsEditor(view, home=home, owned_paths=(tmp_path / "owned",), env={})


def _cli_parses(args: tuple[str, ...]) -> None:
    """The argv must be accepted by the real CLI parser, so a flag rename breaks the test."""
    build_parser().parse_args(list(args))


# Dashboard (FR-100) ---------------------------------------------------------------------------


@pytest.mark.fr("FR-100")
def test_dashboard_shows_name_state_tier_services_disk_and_app_count() -> None:
    vm = DashboardViewModel()
    realms = (
        _realm(
            "arch",
            state="stopped",
            tier="basic",
            services=False,
            disk=DiskUse(
                realm_home_bytes=1610612736, realm_home_partial=False, container_bytes=None
            ),
            exported_apps=2,
        ),
        _realm(
            "debian",
            state="running",
            tier="development",
            services=True,
            disk=DiskUse(realm_home_bytes=2048, realm_home_partial=True, container_bytes=4096),
            exported_apps=0,
        ),
        _realm("fedora", state="broken", tier="basic", services=False, disk=None),
    )

    assert vm.apply(_listing(1, *realms))

    assert [row.name for row in vm.rows] == ["arch", "debian", "fedora"]
    arch = vm.row("arch")
    assert arch is not None
    assert (arch.distro, arch.state, arch.tier, arch.services) == (
        "arch",
        "stopped",
        "basic",
        "off",
    )
    assert (arch.disk, arch.exported_apps) == ("1.5 GiB", "2")
    debian = vm.row("debian")
    assert debian is not None
    assert (debian.state, debian.tier, debian.services) == ("running", "development", "on")
    assert debian.disk == "2.0 KiB (partial)"
    fedora = vm.row("fedora")
    assert fedora is not None
    assert (fedora.disk, fedora.exported_apps, fedora.badge) == ("-", "-", Badge.BROKEN)
    assert all(row.updates is None for row in vm.rows)


@pytest.mark.fr("DR-031")
def test_an_older_listing_is_dropped_and_a_failed_read_keeps_the_last_rows() -> None:
    vm = DashboardViewModel()
    assert vm.apply(_listing(2, _realm("arch")))

    assert vm.apply(_listing(1, _realm("debian"))) is False
    assert [row.name for row in vm.rows] == ["arch"]

    failed = RealmListing(request_id=3, result=_failed("GINN-E-USAGE", 2), realms=None)
    assert vm.apply(failed)
    assert [row.name for row in vm.rows] == ["arch"]
    assert vm.failure is not None
    assert vm.failure.code == "GINN-E-USAGE"

    assert vm.apply(_listing(4, _realm("fedora")))
    assert [row.name for row in vm.rows] == ["fedora"]
    assert vm.failure is None


@pytest.mark.fr("FR-100")
def test_an_empty_listing_is_loaded_and_empty() -> None:
    vm = DashboardViewModel()
    assert not vm.loaded

    vm.apply(_listing(1))

    assert vm.loaded
    assert vm.empty
    assert vm.rows == ()


@pytest.mark.fr("FR-122")
def test_the_badge_marks_broken_and_attention_realms() -> None:
    assert badge_for(_realm("a")) is Badge.OK
    assert badge_for(_realm("b", state="broken")) is Badge.BROKEN
    assert badge_for(_realm("c", restart_required=True)) is Badge.ATTENTION
    assert badge_for(_realm("d", state_mismatch=True)) is Badge.ATTENTION
    assert badge_for(_realm("e", share_skipped=("/x",))) is Badge.ATTENTION
    assert badge_for(_realm("f", state=None, error="unreadable record")) is Badge.ATTENTION


@pytest.mark.fr("IR-011")
def test_a_failure_without_an_error_object_still_shows_the_status() -> None:
    failure = failure_of(_failed(None, 1))

    assert failure is not None
    assert failure.code is None
    assert failure.message == "ginn exited with status 1"
    assert failure_of(_ok()) is None


@pytest.mark.fr("FR-020")
def test_byte_counts_use_binary_units() -> None:
    assert format_bytes(1023) == "1023 B"
    assert format_bytes(1024) == "1.0 KiB"
    assert format_bytes(1536 * 1024 * 1024) == "1.5 GiB"


# Create wizard (FR-101) -----------------------------------------------------------------------


@pytest.mark.fr("FR-101")
def test_a_name_with_a_capital_and_a_bang_shows_the_dr002_error_and_blocks_create() -> None:
    wizard = CreateWizard(available())
    wizard.choose_distro("arch")

    wizard.set_name("Arch!")

    failure = wizard.name_failure()
    assert failure is not None
    assert failure.code == NAME_CODE
    assert wizard.can_create() is False
    with pytest.raises(ValueError):
        wizard.create_args()

    wizard.set_name("arch")
    assert wizard.name_failure() is None
    assert wizard.can_create() is True


@pytest.mark.fr("FR-101")
def test_the_everything_option_is_offered_only_for_kali() -> None:
    wizard = CreateWizard(available())

    wizard.choose_distro("debian")
    assert wizard.show_everything is False
    wizard.set_everything(True)
    assert wizard.everything is False
    assert "--everything" not in wizard.create_args()
    assert "--yes" not in wizard.create_args()

    wizard.choose_distro("kali")
    assert wizard.show_everything is True
    wizard.set_everything(True)
    assert wizard.everything is True
    assert wizard.create_args()[-2:] == ("--everything", "--yes")

    wizard.choose_distro("debian")
    assert wizard.everything is False


@pytest.mark.fr("FR-010")
def test_the_name_follows_the_distribution_until_the_user_types_one() -> None:
    wizard = CreateWizard(available())

    wizard.choose_distro("fedora")
    assert wizard.name == "fedora"
    wizard.choose_distro("debian")
    assert wizard.name == "debian"

    wizard.set_name("work")
    wizard.choose_distro("arch")
    assert wizard.name == "work"


@pytest.mark.fr("FR-101")
def test_create_args_are_the_cli_create_argv_with_the_defaults() -> None:
    wizard = CreateWizard(available())
    wizard.choose_distro("debian")

    assert wizard.create_args() == (
        "--json",
        "create",
        "debian",
        "--name",
        "debian",
        "--tier",
        "basic",
        "--services",
        "off",
    )
    _cli_parses(wizard.create_args())


@pytest.mark.fr("FR-101")
def test_create_args_carry_the_chosen_tier_and_packages() -> None:
    wizard = CreateWizard(available())
    wizard.choose_distro("debian")
    wizard.set_name("dev")
    wizard.set_tier(Tier.DEVELOPMENT)
    assert wizard.add_package("htop") is None
    assert wizard.add_package("curl") is None

    args = wizard.create_args()

    assert args == (
        "--json",
        "create",
        "debian",
        "--name",
        "dev",
        "--tier",
        "development",
        "--services",
        "off",
        "--with",
        "htop",
        "curl",
    )
    _cli_parses(args)


@pytest.mark.fr("FR-101")
def test_a_services_on_create_without_packages_carries_the_mode_and_no_with() -> None:
    wizard = CreateWizard(available())
    wizard.choose_distro("debian")
    wizard.set_services(on=True)

    args = wizard.create_args()

    assert args[args.index("--services") + 1] == "on"
    assert "--with" not in args
    assert wizard.services_conflict() is None
    _cli_parses(args)


@pytest.mark.fr("FR-101")
def test_a_services_on_create_with_a_package_is_refused_before_any_argv() -> None:
    """ADR-003: the CLI cannot start a services-on container, so the create is not sent."""
    wizard = CreateWizard(available())
    wizard.choose_distro("debian")
    wizard.set_services(on=True)
    assert wizard.add_package("htop") is None

    assert wizard.services_conflict() == SERVICES_CONFLICT_TEXT
    assert wizard.can_create() is False
    with pytest.raises(ValueError):
        wizard.create_args()

    wizard.remove_package("htop")
    assert wizard.services_conflict() is None
    assert wizard.can_create() is True


@pytest.mark.fr("FR-101")
def test_a_services_on_kali_create_with_the_everything_set_is_refused_until_it_is_off() -> None:
    wizard = CreateWizard(available())
    wizard.choose_distro("kali")
    wizard.set_services(on=True)
    wizard.set_everything(True)

    assert wizard.services_conflict() == SERVICES_CONFLICT_TEXT
    assert wizard.can_create() is False
    with pytest.raises(ValueError):
        wizard.create_args()

    wizard.set_everything(False)
    assert wizard.can_create() is True
    assert "--everything" not in wizard.create_args()


@pytest.mark.fr("NFR-SEC-005")
def test_a_package_with_shell_characters_is_refused_before_any_run() -> None:
    wizard = CreateWizard(available())
    wizard.choose_distro("arch")

    failure = wizard.add_package("htop;rm -rf /")

    assert failure is not None
    assert failure.code == PACKAGE_CODE
    assert wizard.packages == ()


@pytest.mark.fr("NFR-SEC-005")
def test_switching_distribution_drops_the_packages_the_new_one_rejects() -> None:
    wizard = CreateWizard(available())
    wizard.choose_distro("fedora")
    assert wizard.add_package("Qt5") is None

    dropped = wizard.choose_distro("debian")

    assert dropped == ("Qt5",)
    assert wizard.packages == ()


# Permissions editor (FR-103) ------------------------------------------------------------------


@pytest.mark.fr("FR-103")
def test_a_share_of_etc_is_refused_with_path_forbidden_and_not_saved(tmp_path: Path) -> None:
    editor = _editor(tmp_path)

    failure = editor.add_share("/etc", "rw")

    assert failure is not None
    assert failure.code == PATH_CODE
    assert editor.shares == ()
    assert editor.commands() == ()


@pytest.mark.fr("FR-063")
def test_a_share_under_home_is_saved_and_applied_with_share_ro(tmp_path: Path) -> None:
    editor = _editor(tmp_path)
    path = (tmp_path / "home" / "projects").resolve()

    assert editor.add_share(str(path), "ro") is None

    assert editor.shares == (Share(path=str(path), mode="ro"),)
    commands = editor.commands()
    assert commands == (("--json", "perms", "lab", "--share-ro", str(path)),)
    _cli_parses(commands[0])


@pytest.mark.fr("FR-063")
def test_toggles_are_edited_only_in_the_custom_tier(tmp_path: Path) -> None:
    editor = _editor(tmp_path, tier="basic", toggles=("display",))

    assert editor.editable is False
    assert editor.toggles == effective_toggles(Tier.BASIC, frozenset())
    assert editor.toggles
    with pytest.raises(ValueError):
        editor.set_toggle(Toggle.GPU, on=True)


@pytest.mark.fr("FR-066")
def test_moving_to_custom_applies_the_tier_before_the_toggles(tmp_path: Path) -> None:
    editor = _editor(tmp_path, tier="basic", toggles=("display",))
    path = (tmp_path / "home" / "projects").resolve()

    editor.set_tier(Tier.CUSTOM)
    editor.set_toggle(Toggle.GPU, on=True)
    assert editor.add_share(str(path), "rw") is None

    commands = editor.commands()
    assert commands == (
        ("--json", "tier", "lab", "custom"),
        ("--json", "perms", "lab", "--enable", "gpu", "--share", str(path)),
    )
    for call in commands:
        _cli_parses(call)


@pytest.mark.fr("FR-063")
def test_disabling_a_toggle_emits_disable_and_removing_a_share_emits_no_share(
    tmp_path: Path,
) -> None:
    shares = (ShareView(path="/home/tester/projects", mode="ro"),)
    editor = _editor(tmp_path, toggles=("downloads", "gpu"), shares=shares)

    editor.set_toggle(Toggle.GPU, on=False)
    editor.remove_share("/home/tester/projects")

    commands = editor.commands()
    assert commands == (
        ("--json", "perms", "lab", "--disable", "gpu", "--no-share", "/home/tester/projects"),
    )
    _cli_parses(commands[0])


# Host health (FR-104) -------------------------------------------------------------------------


@pytest.mark.fr("FR-104")
def test_an_unacknowledged_host_change_shows_a_blocking_banner_listing_the_paths() -> None:
    vm = HealthViewModel()
    vm.apply_integrity(
        IntegrityState(
            status="changed",
            changed=("/etc/hosts", "/usr/bin/tool"),
            pending=("/etc/hosts", "/usr/bin/tool"),
            warnings=(
                CliWarning(
                    code=HOST_CHANGED_CODE,
                    message="host files changed since baseline",
                    remedy="run 'ginn integrity --ack' after review",
                ),
            ),
        )
    )

    banner = vm.banner
    assert banner is not None
    assert banner.paths == ("/etc/hosts", "/usr/bin/tool")
    assert vm.acknowledge_args == ACKNOWLEDGE_ARGS
    _cli_parses(vm.acknowledge_args)


@pytest.mark.fr("FR-104")
def test_no_banner_when_the_host_is_unchanged_or_acknowledged() -> None:
    vm = HealthViewModel()
    vm.apply_integrity(IntegrityState(status="unchanged", changed=(), pending=None, warnings=()))
    assert vm.banner is None

    vm.apply_integrity(
        IntegrityState(status="acknowledged", changed=("/etc/hosts",), pending=None, warnings=())
    )
    assert vm.banner is None


@pytest.mark.fr("FR-104")
def test_the_doctor_badge_follows_the_worst_check() -> None:
    vm = HealthViewModel()
    assert vm.badge is HealthBadge.UNKNOWN

    vm.apply_doctor(
        DoctorReport(
            user="tester",
            uid=1000,
            ok=True,
            counts={"PASS": 2, "WARN": 0, "FAIL": 0, "INFO": 0},
            checks=(
                DoctorCheck(name="podman", status="PASS", message="ok", fixes=()),
                DoctorCheck(name="subuid", status="PASS", message="ok", fixes=()),
            ),
        )
    )
    assert vm.badge is HealthBadge.OK

    vm.apply_doctor(
        DoctorReport(
            user="tester",
            uid=1000,
            ok=False,
            counts={"PASS": 1, "WARN": 0, "FAIL": 1, "INFO": 0},
            checks=(
                DoctorCheck(
                    name="subuid",
                    status="FAIL",
                    message="no subuid entry",
                    fixes=("ginn host-setup --user tester",),
                ),
                DoctorCheck(name="podman", status="PASS", message="ok", fixes=()),
            ),
        )
    )
    assert vm.badge is HealthBadge.FAIL
    assert vm.checks[0].fixes == ("ginn host-setup --user tester",)


@pytest.mark.fr("FR-104")
def test_the_changing_actions_stay_blocked_until_the_host_change_is_acknowledged() -> None:
    vm = HealthViewModel()
    assert vm.blocks_mutations is False

    vm.apply_integrity(
        IntegrityState(
            status="changed", changed=("/etc/hosts",), pending=("/etc/hosts",), warnings=()
        )
    )
    assert vm.blocks_mutations is True

    vm.apply_integrity(IntegrityState(status="acknowledged", changed=(), pending=None, warnings=()))
    assert vm.blocks_mutations is False


# Kali everything estimate (FR-014 AC-1, FR-101 AC-1) ----------------------------------------------

ESTIMATE_PAYLOAD: dict[str, object] = {
    "distro": "kali",
    "everything": ["kali-linux-everything"],
    "packages": 3,
    "download_bytes": 6000,
    "installed_bytes": 6144,
    "required_bytes": 13359,
}


def _result(payload: dict[str, object] | None, *, exit_code: int = 0) -> CliResult:
    return CliResult(
        argv=("--json", "create", "kali", "--everything", "--estimate"),
        exit_code=exit_code,
        payload=payload,
        error=None,
        warnings=(),
        stderr="",
        cancelled=False,
    )


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-101")
@pytest.mark.fr("IR-002")
def test_the_kali_estimate_is_the_first_step_and_names_no_realm_or_package() -> None:
    wizard = CreateWizard(available())
    wizard.choose_distro("kali")
    wizard.set_everything(True)
    wizard.add_package("htop")

    args = wizard.estimate_args()

    assert args == ("--json", "create", "kali", "--everything", "--estimate")
    _cli_parses(args)
    assert "--yes" not in args and "--with" not in args


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-101")
def test_the_estimate_is_refused_unless_the_kali_everything_option_is_on() -> None:
    wizard = CreateWizard(available())
    wizard.choose_distro("kali")
    with pytest.raises(ValueError):
        wizard.estimate_args()
    wizard.choose_distro("debian")
    wizard.set_everything(True)
    with pytest.raises(ValueError):
        wizard.estimate_args()


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-101")
def test_the_confirmed_create_passes_yes_only_after_the_estimate_step() -> None:
    wizard = CreateWizard(available())
    wizard.choose_distro("kali")
    wizard.set_everything(True)

    assert wizard.estimate_args()[-1] == "--estimate"
    args = wizard.create_args()
    assert args[-2:] == ("--everything", "--yes")
    _cli_parses(args)


@pytest.mark.fr("FR-014")
def test_the_estimate_payload_is_read_into_sizes_and_the_confirmation_shows_them() -> None:
    sizes = EverythingSizes.from_payload(ESTIMATE_PAYLOAD)

    assert sizes == EverythingSizes(
        packages=3, download_bytes=6000, installed_bytes=6144, required_bytes=13359
    )
    body = sizes.confirmation_body()
    assert "3 packages" in body
    assert "download 5.9 KiB, installed 6.0 KiB" in body
    assert "13.0 KiB free" in body
    assert "Nothing of the set is installed until you confirm" in body


@pytest.mark.fr("FR-014")
@pytest.mark.parametrize("key", ["packages", "download_bytes", "installed_bytes", "required_bytes"])
@pytest.mark.parametrize("value", [None, "6000", True, -1, 1.5])
def test_a_size_that_is_not_a_whole_number_is_refused(key: str, value: object) -> None:
    payload = {**ESTIMATE_PAYLOAD, key: value}

    with pytest.raises(ValueError):
        EverythingSizes.from_payload(payload)


@pytest.mark.fr("FR-014")
def test_sizes_of_reads_the_last_result_and_refuses_a_run_without_a_payload() -> None:
    assert sizes_of([_result(ESTIMATE_PAYLOAD)]) == EverythingSizes.from_payload(ESTIMATE_PAYLOAD)
    assert sizes_of([]) is None
    assert sizes_of([_result(None)]) is None
    assert sizes_of([_result({"packages": 1})]) is None


@pytest.mark.fr("FR-107")
def test_the_progress_model_keeps_the_results_of_a_finished_run() -> None:
    model = ProgressModel(title="Sizing")
    result = _result(ESTIMATE_PAYLOAD)

    model.finish([result], None)

    assert model.phase is Phase.DONE
    assert model.results == (result,)
