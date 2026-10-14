# SPDX-License-Identifier: Apache-2.0
"""Text and JSON views of realm records and statuses for the realm commands (IR-010, FR-020).

The JSON keys of a status come from `RealmStatus.to_json()`, which a golden test pins, so this
module adds nothing to them. A broken realm's text names both recovery commands (FR-122 AC-1).
`create --resume` is M8 (T8.5), so the text says so.
"""

from __future__ import annotations

from collections.abc import Sequence

from ginn.domain import RealmState
from ginn.domain.permissions import effective_toggles
from ginn.ops.lifecycle import RealmStatus, render_status_text
from ginn.store.records import RealmRecord

LIST_HEADER: tuple[str, ...] = (
    "NAME",
    "STATE",
    "CONTAINER",
    "DISTRO",
    "TIER",
    "SERVICES",
    "DISK",
    "NOTES",
)
NO_REALMS = "no realms"


def record_json(record: RealmRecord) -> dict[str, object]:
    """The result of start and stop: the realm, its state and what the start left out."""
    return {
        "name": record.settings.name.value,
        "state": record.state.value,
        "restart_required": record.restart_required,
        "share_skipped": list(record.share_skipped),
    }


def record_text(record: RealmRecord) -> str:
    """One line for the realm and its state, then each skipped share and a restart flag."""
    lines = [f"realm {record.settings.name.value} is {record.state.value}"]
    lines.extend(record.share_skipped)
    if record.restart_required:
        lines.append("restart required")
    return "\n".join(lines)


def services_json(record: RealmRecord) -> dict[str, object]:
    """The services mode of a realm after `ginn services` (FR-083): its name and the mode."""
    return {"name": record.settings.name.value, "services": record.settings.services}


def services_text(record: RealmRecord) -> str:
    mode = "on" if record.settings.services else "off"
    return f"realm {record.settings.name.value} services are {mode}"


def autostart_json(record: RealmRecord) -> dict[str, object]:
    """The autostart of a realm after `ginn autostart` (FR-084): its name and the flag."""
    return {
        "name": record.settings.name.value,
        "services": record.settings.services,
        "autostart": record.settings.autostart,
    }


def autostart_text(record: RealmRecord) -> str:
    mode = "on" if record.settings.autostart else "off"
    return f"realm {record.settings.name.value} autostart is {mode}"


def permissions_json(record: RealmRecord) -> dict[str, object]:
    """The tier and the permissions of a realm (FR-060, FR-063), with its restart flag.

    `toggles` are the effective ones: a Basic or Development realm shows its tier's set.
    """
    toggles = effective_toggles(record.settings.tier, record.permissions.toggles)
    return {
        "name": record.settings.name.value,
        "state": record.state.value,
        "tier": record.settings.tier.value,
        "toggles": sorted(toggle.value for toggle in toggles),
        "shares": [share.to_dict() for share in record.permissions.shares],
        "restart_required": record.restart_required,
    }


def permissions_text(record: RealmRecord) -> str:
    """The tier, the toggles and each share, then the restart flag (FR-066)."""
    toggles = effective_toggles(record.settings.tier, record.permissions.toggles)
    lines = [
        f"realm {record.settings.name.value} tier {record.settings.tier.value}",
        "toggles: " + (", ".join(sorted(toggle.value for toggle in toggles)) or "none"),
    ]
    lines += [f"share: {share.path} ({share.mode})" for share in record.permissions.shares]
    if record.restart_required:
        lines.append(
            f"restart required: run 'ginn stop {record.settings.name.value}' and "
            f"'ginn start {record.settings.name.value}' to apply the change"
        )
    return "\n".join(lines)


def status_text(status: RealmStatus) -> str:
    """The status of one realm, with the recovery commands when the realm is broken (FR-122)."""
    text = render_status_text(status)
    if status.state == RealmState.BROKEN.value:
        return f"{text}\n{_broken_hint(status.name)}"
    return text


def list_text(statuses: Sequence[RealmStatus]) -> str:
    """A compact table of the realms, then one recovery line for each broken realm."""
    if not statuses:
        return NO_REALMS
    rows = [LIST_HEADER, *(_row(status) for status in statuses)]
    lines = [table(rows)]
    lines.extend(_broken_hint(s.name) for s in statuses if s.state == RealmState.BROKEN.value)
    return "\n".join(lines)


def table(rows: Sequence[Sequence[str]]) -> str:
    """Pad each column to its widest cell. The first row is the header."""
    widths = [max(len(row[column]) for row in rows) for column in range(len(LIST_HEADER))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def _row(status: RealmStatus) -> list[str]:
    services = "-" if status.services is None else ("on" if status.services else "off")
    disk = "-" if status.disk is None else str(status.disk.realm_home_bytes)
    if status.disk is not None and status.disk.realm_home_partial:
        disk += " (partial)"
    return [
        status.name,
        status.state or "-",
        status.container_state or "-",
        status.distro or "-",
        status.tier or "-",
        services,
        disk,
        "; ".join(_notes(status)) or "-",
    ]


def _notes(status: RealmStatus) -> list[str]:
    notes: list[str] = []
    if status.error is not None:
        notes.append(status.error)
    if status.state_mismatch:
        notes.append("state mismatch")
    if status.restart_required:
        notes.append("restart required")
    if status.share_skipped:
        notes.append(f"{len(status.share_skipped)} share(s) skipped")
    return notes


def _broken_hint(name: str) -> str:
    return (
        f"realm {name} is broken: run 'ginn create --resume {name}' (arrives in M8) "
        f"or 'ginn delete {name}'"
    )
