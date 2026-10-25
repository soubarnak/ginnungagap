# SPDX-License-Identifier: Apache-2.0
"""The argv of every Control Plane control, and the IR-001..IR-006 to CLI mapping (FR-106).

Each control runs one `ginn --json` command. The views build their argv here, and the parity test
(tests/unit/test_gui_parity.py) parses the same functions with the real CLI parser. That check
proves the argv is well formed for the parser, and nothing more: it does not run the command. A
control that runs a command the parser does not know yet is listed with its missing milestone, so
the parity test records it as expected to fail and does not skip it.

The Kali "everything" create runs in two steps. The first control's argv, `--everything --estimate`,
changes nothing and prints the sizes that the confirmation shows (FR-014 AC-1, FR-101 AC-1). The
second is `--everything --yes`, which runs only after that confirmation.

Realm names and values are passed as argv items only. No function here builds a shell string
(NFR-SEC-005).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from ginn.gui.viewmodels.wizard import CreateWizard

Argv = tuple[str, ...]

SAMPLE_REALM: Final = "arch"
"""The realm name that the parity test puts into the controls that take one."""


def list_args() -> Argv:
    return ("--json", "list")


def status_args(realm: str) -> Argv:
    return ("--json", "status", realm)


def start_args(realm: str) -> Argv:
    return ("--json", "start", realm)


def stop_args(realm: str) -> Argv:
    return ("--json", "stop", realm)


def services_args(realm: str, on: bool) -> Argv:
    return ("--json", "services", realm, "on" if on else "off")


def autostart_args(realm: str, on: bool) -> Argv:
    return ("--json", "autostart", realm, "on" if on else "off")


def update_args(realm: str) -> Argv:
    return ("--json", "update", realm)


def rebuild_args(realm: str) -> Argv:
    """`ginn rebuild <realm>`, which asks no yes of its own: the view confirms first (FR-021)."""
    return ("--json", "rebuild", realm)


def create_args() -> Argv:
    """The argv the wizard builds for a plain Debian realm with the defaults (FR-101)."""
    wizard = CreateWizard(("debian",))
    wizard.choose_distro("debian")
    return wizard.create_args()


def kali_estimate_args() -> Argv:
    """The argv the wizard builds for the Kali everything estimate, its first step (FR-014 AC-1)."""
    wizard = CreateWizard(("kali",))
    wizard.choose_distro("kali")
    wizard.set_everything(True)
    return wizard.estimate_args()


def kali_everything_args() -> Argv:
    """The argv the wizard builds for Kali with the everything set and one package (FR-014, FR-101).

    The package makes the parity test cover `--with` as well as `--everything --yes`.
    """
    wizard = CreateWizard(("kali",))
    wizard.choose_distro("kali")
    wizard.set_everything(True)
    wizard.add_package("htop")
    return wizard.create_args()


def delete_args(realm: str, *, purge_home: bool) -> Argv:
    """`delete` with --yes, because the Control Plane has asked the user already (FR-102).

    A purge without --yes is refused in JSON mode, so the flag is always passed.
    """
    if purge_home:
        return ("--json", "delete", realm, "--purge-home", "--yes")
    return ("--json", "delete", realm, "--yes")


def tier_args(realm: str, tier: str) -> Argv:
    return ("--json", "tier", realm, tier)


def doctor_args() -> Argv:
    return ("--json", "doctor")


def integrity_args() -> Argv:
    return ("--json", "integrity")


def acknowledge_args() -> Argv:
    return ("--json", "integrity", "--ack")


CONTROLS: Final[Mapping[str, Callable[[], Argv]]] = {
    "dashboard.refresh": list_args,
    "dashboard.open": lambda: status_args(SAMPLE_REALM),
    "detail.refresh": lambda: status_args(SAMPLE_REALM),
    "detail.start": lambda: start_args(SAMPLE_REALM),
    "detail.stop": lambda: stop_args(SAMPLE_REALM),
    "detail.services-on": lambda: services_args(SAMPLE_REALM, True),
    "detail.services-off": lambda: services_args(SAMPLE_REALM, False),
    "detail.autostart-on": lambda: autostart_args(SAMPLE_REALM, True),
    "detail.autostart-off": lambda: autostart_args(SAMPLE_REALM, False),
    "detail.update": lambda: update_args(SAMPLE_REALM),
    "detail.rebuild": lambda: rebuild_args(SAMPLE_REALM),
    "detail.delete": lambda: delete_args(SAMPLE_REALM, purge_home=False),
    "detail.delete-purge-home": lambda: delete_args(SAMPLE_REALM, purge_home=True),
    "wizard.create": create_args,
    "wizard.create-kali-estimate": kali_estimate_args,
    "wizard.create-kali-everything": kali_everything_args,
    "permissions.tier": lambda: tier_args(SAMPLE_REALM, "custom"),
    "permissions.perms": lambda: ("--json", "perms", SAMPLE_REALM, "--enable", "gpu"),
    "health.doctor": doctor_args,
    "health.integrity": integrity_args,
    "health.acknowledge": acknowledge_args,
}
"""Each control's argv, built by the same functions the views call."""

PARITY: Final[Mapping[str, Mapping[str, str]]] = {
    "IR-001": {"dashboard.refresh": "list", "dashboard.open": "status"},
    "IR-002": {
        "wizard.create": "create",
        "wizard.create-kali-estimate": "create",
        "wizard.create-kali-everything": "create",
    },
    "IR-003": {
        "detail.refresh": "status",
        "detail.start": "start",
        "detail.stop": "stop",
        "detail.services-on": "services",
        "detail.services-off": "services",
        "detail.autostart-on": "autostart",
        "detail.autostart-off": "autostart",
        "detail.update": "update",
        "detail.rebuild": "rebuild",
        "detail.delete": "delete",
        "detail.delete-purge-home": "delete",
    },
    "IR-004": {"permissions.tier": "tier", "permissions.perms": "perms"},
    "IR-005": {
        "health.doctor": "doctor",
        "health.integrity": "integrity",
        "health.acknowledge": "integrity",
    },
    "IR-006": {"progress.cancel": "SIGINT"},
}
"""Each IR-001..IR-006 screen, and the CLI verb that each of its controls runs.

The progress dialog (IR-006) has no verb of its own. Its Cancel sends SIGINT to the running
command, and the long operations it shows are create, update and rebuild (FR-107).
"""

PROGRESS_OPERATIONS: Final = ("create", "update", "rebuild")
"""The long operations that show the progress dialog (FR-107)."""

KNOWN_MISSING: Final[Mapping[str, str]] = {}
"""CLI verbs that a control runs but the parser does not register yet, with their milestone. It is
empty: every control's verb is a registered command (IR-010), `rebuild` since T8.3."""
