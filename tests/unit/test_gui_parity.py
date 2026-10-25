# SPDX-License-Identifier: Apache-2.0
"""FR-106 CLI parity: every Control Plane action has a CLI equivalent that the real parser accepts.

`ginn.gui.actions` maps each IR-001..IR-006 screen to the controls it shows, and each control to
the IR-010 command it runs. The controls build their argv with the same functions the views call.
Here every argv goes through the real parser of `ginn.cli.main`. A control whose command the parser
does not know is recorded as expected to fail with its milestone (`actions.KNOWN_MISSING`), not
skipped, so a gap stays visible. No control is in that list now: `rebuild` is a command since T8.3.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from ginn.cli.main import build_parser
from ginn.gui import actions
from ginn.gui.backend import CliRunner

IR_SCREENS = ("IR-001", "IR-002", "IR-003", "IR-004", "IR-005", "IR-006")
"""The Control Plane screens of SRS section 5.1 (IR-001..IR-006)."""

IR_010_VERBS = frozenset(
    {
        "host-setup",
        "doctor",
        "integrity",
        "create",
        "list",
        "status",
        "start",
        "stop",
        "enter",
        "exec",
        "install",
        "remove",
        "export",
        "unexport",
        "tier",
        "perms",
        "services",
        "autostart",
        "rebuild",
        "update",
        "delete",
        "log",
    }
)
"""The verbs of the CLI command inventory (SRS IR-010)."""

CONTROL_VERBS: Mapping[str, str] = {
    control: verb for controls in actions.PARITY.values() for control, verb in controls.items()
}
"""Each control of IR-001..IR-006, and the verb that it runs. Cancel runs SIGINT, not a verb."""

COMMAND_CONTROLS: tuple[str, ...] = tuple(
    control for control, verb in CONTROL_VERBS.items() if verb != "SIGINT"
)


def _verb_of(argv: tuple[str, ...]) -> str:
    assert argv[0] == "--json", f"the Control Plane runs --json only: {argv!r}"
    return argv[1]


def _parity_params() -> list[object]:
    params: list[object] = []
    for control in COMMAND_CONTROLS:
        verb = CONTROL_VERBS[control]
        if verb in actions.KNOWN_MISSING:
            params.append(
                pytest.param(
                    control,
                    marks=pytest.mark.xfail(
                        strict=True,
                        raises=Exception,
                        reason=f"not a CLI command yet: {actions.KNOWN_MISSING[verb]}",
                    ),
                    id=control,
                )
            )
        else:
            params.append(pytest.param(control, id=control))
    return params


@pytest.mark.fr("FR-106")
@pytest.mark.parametrize("screen", IR_SCREENS)
def test_every_ir_screen_from_001_to_006_has_a_gui_action(screen: str) -> None:
    assert actions.PARITY.get(screen), f"{screen} has no GUI action"


@pytest.mark.fr("FR-106")
def test_every_control_in_the_mapping_builds_an_argv_and_no_other() -> None:
    assert set(COMMAND_CONTROLS) == set(actions.CONTROLS)


@pytest.mark.fr("FR-106")
@pytest.mark.parametrize("control", COMMAND_CONTROLS)
def test_every_control_builds_the_argv_of_the_command_it_is_listed_under(control: str) -> None:
    argv = actions.CONTROLS[control]()

    assert _verb_of(argv) == CONTROL_VERBS[control]


@pytest.mark.fr("FR-106")
def test_every_mapped_verb_is_an_ir_010_command() -> None:
    for verb in set(CONTROL_VERBS.values()) - {"SIGINT"}:
        assert verb in IR_010_VERBS, f"{verb} is not in the IR-010 inventory"


@pytest.mark.fr("FR-106")
@pytest.mark.parametrize("control", _parity_params())
def test_the_cli_parser_accepts_the_argv_of_every_control(control: str) -> None:
    assert build_parser().parse_args(list(actions.CONTROLS[control]())) is not None


@pytest.mark.fr("FR-106")
def test_no_control_is_a_known_gap_and_the_rebuild_argv_is_the_rebuild_command() -> None:
    assert dict(actions.KNOWN_MISSING) == {}
    parsed = build_parser().parse_args(list(actions.rebuild_args(actions.SAMPLE_REALM)))
    assert parsed.command == "rebuild"
    assert parsed.realm == actions.SAMPLE_REALM


@pytest.mark.fr("FR-106")
def test_ir_006_cancel_is_the_sigint_that_the_runner_sends() -> None:
    assert actions.PARITY["IR-006"] == {"progress.cancel": "SIGINT"}
    assert callable(CliRunner.cancel)
    long_operations = {
        "create": "wizard.create",
        "update": "detail.update",
        "rebuild": "detail.rebuild",
    }
    assert set(actions.PROGRESS_OPERATIONS) == set(long_operations)
    for verb, control in long_operations.items():
        assert _verb_of(actions.CONTROLS[control]()) == verb


@pytest.mark.fr("FR-106")
def test_delete_passes_yes_and_purge_only_when_asked() -> None:
    assert actions.delete_args("arch", purge_home=False) == ("--json", "delete", "arch", "--yes")
    assert actions.delete_args("arch", purge_home=True) == (
        "--json",
        "delete",
        "arch",
        "--purge-home",
        "--yes",
    )


@pytest.mark.fr("FR-014")
def test_the_kali_everything_argv_carries_its_package_and_yes_and_parses() -> None:
    argv = actions.kali_everything_args()

    assert argv[:3] == ("--json", "create", "kali")
    assert argv[argv.index("--with") + 1] == "htop"
    assert argv[-2:] == ("--everything", "--yes")
    parsed = build_parser().parse_args(list(argv))
    assert parsed.everything is True
    assert parsed.yes is True
    assert parsed.with_packages == ["htop"]


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-101")
@pytest.mark.fr("IR-002")
def test_the_kali_estimate_argv_is_the_first_step_and_parses_as_an_estimate() -> None:
    assert actions.PARITY["IR-002"]["wizard.create-kali-estimate"] == "create"
    argv = actions.kali_estimate_args()

    assert argv == ("--json", "create", "kali", "--everything", "--estimate")
    parsed = build_parser().parse_args(list(argv))
    assert parsed.estimate is True
    assert parsed.everything is True
    assert parsed.yes is False
    assert parsed.with_packages is None
