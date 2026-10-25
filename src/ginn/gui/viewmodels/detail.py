# SPDX-License-Identifier: Apache-2.0
"""Realm detail viewmodel: the status rows, the actions a state allows and their confirmations.

The detail view shows the rows and the buttons this module chooses (FR-102, IR-003). Rebuild and
delete need a confirmation. Delete offers "also delete home data" as a separate choice that is
unchecked by default. Pure Python, no GTK.
"""

from __future__ import annotations

from dataclasses import dataclass

from ginn.gui.backend import RealmView
from ginn.gui.viewmodels.labels import NONE_TEXT, disk_label, services_label

REBUILD_AVAILABLE = True
"""`ginn rebuild` is a CLI command (T8.3, FR-021). The Control Plane offers the button, after its
confirmation, for a realm that can rebuild (`can_rebuild`)."""

SERVICES_TOGGLE_AVAILABLE = True
"""`ginn services on|off` is a CLI command (T8.3, FR-083). Each switch is offered only for the mode
that it changes (`can_services_on`, `can_services_off`)."""

EXPORTS_UNKNOWN_TEXT = "not reported by the CLI yet"


@dataclass(frozen=True)
class Confirmation:
    """The question an action asks before it runs. `check_label`, when set, is an extra choice."""

    heading: str
    body: str
    confirm_label: str
    destructive: bool
    check_label: str | None = None


def detail_rows(realm: RealmView) -> tuple[tuple[str, str], ...]:
    """The label and value of each status row. Unknown values show `-`, never a guess."""
    rows: list[tuple[str, str]] = [
        ("State", realm.state or NONE_TEXT),
        ("Container", realm.container_state or NONE_TEXT),
        ("Distribution", realm.distro or NONE_TEXT),
        ("Tier", realm.tier or NONE_TEXT),
        ("Services", services_label(realm.services)),
        ("Generation", NONE_TEXT if realm.generation is None else str(realm.generation)),
        ("Disk", disk_label(realm.disk)),
        ("Exported apps", _exports_text(realm.exported_apps)),
    ]
    if realm.restart_required:
        rows.append(("Restart", "required"))
    if realm.error is not None:
        rows.append(("Error", realm.error))
    return tuple(rows)


def _exports_text(count: int | None) -> str:
    return EXPORTS_UNKNOWN_TEXT if count is None else str(count)


def can_start(realm: RealmView) -> bool:
    return realm.state == "stopped"


def can_stop(realm: RealmView) -> bool:
    return realm.state == "running"


def can_update(realm: RealmView) -> bool:
    return realm.state in ("stopped", "running")


def can_rebuild(realm: RealmView) -> bool:
    """A rebuild runs on a stopped or running realm, as `ginn rebuild` does (FR-021)."""
    return realm.state in ("stopped", "running")


def can_services_on(realm: RealmView) -> bool:
    """Services on is offered only for a realm whose services are off (FR-083)."""
    return SERVICES_TOGGLE_AVAILABLE and realm.services is False


def can_services_off(realm: RealmView) -> bool:
    """Services off is offered only for a realm whose services are on (FR-083)."""
    return SERVICES_TOGGLE_AVAILABLE and realm.services is True


def delete_confirmation(name: str) -> Confirmation:
    """FR-102: delete asks first. The home data stays unless the second choice is ticked."""
    return Confirmation(
        heading=f"Delete realm {name}?",
        body=(
            "The realm's container, services and exports are removed. Its home data is kept "
            "unless the box below is ticked."
        ),
        confirm_label="Delete",
        destructive=True,
        check_label="Also delete home data",
    )


def rebuild_confirmation(name: str) -> Confirmation:
    """FR-102: a rebuild creates a new container with the same home. It asks first."""
    return Confirmation(
        heading=f"Rebuild realm {name}?",
        body="A new container is made with the same home data. The realm stops while it runs.",
        confirm_label="Rebuild",
        destructive=False,
    )
