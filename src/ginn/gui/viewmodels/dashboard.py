# SPDX-License-Identifier: Apache-2.0
"""Dashboard viewmodel: the realm list with its state, tier, services, disk and badge (FR-100).

The view holds one `DashboardViewModel` and passes it each `RealmListing` that the backend
returns. A listing replaces the rows as a whole, so the view never mixes two reads. A listing
older than the one already applied is dropped (DR-031, monotonic reads).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ginn.gui.backend import RealmListing, RealmView
from ginn.gui.viewmodels.labels import NONE_TEXT, Failure, disk_label, failure_of, services_label


class Badge(StrEnum):
    """The health badge of one realm. `attention` needs a look, `broken` needs recovery (FR-122)."""

    OK = "ok"
    ATTENTION = "attention"
    BROKEN = "broken"


@dataclass(frozen=True)
class RealmRow:
    """One line of the dashboard. Every text field is ready to show."""

    name: str
    distro: str
    state: str
    tier: str
    services: str
    disk: str
    exported_apps: str
    updates: bool | None
    badge: Badge
    notes: tuple[str, ...]


def badge_for(realm: RealmView) -> Badge:
    """`broken` for a broken realm. `attention` for a missing state, an error, a mismatch, a
    pending restart or a skipped share. `ok` otherwise."""
    if realm.state == "broken":
        return Badge.BROKEN
    if realm.state is None or realm.error is not None or realm.state_mismatch:
        return Badge.ATTENTION
    if realm.restart_required or realm.share_skipped:
        return Badge.ATTENTION
    return Badge.OK


def _notes(realm: RealmView) -> tuple[str, ...]:
    notes: list[str] = []
    if realm.error is not None:
        notes.append(realm.error)
    if realm.state_mismatch:
        notes.append("state mismatch")
    if realm.restart_required:
        notes.append("restart required")
    if realm.share_skipped:
        notes.append(f"{len(realm.share_skipped)} share(s) skipped")
    return tuple(notes)


def row_for(realm: RealmView) -> RealmRow:
    """The dashboard line for one realm. Unknown values show `-`, never a guess."""
    return RealmRow(
        name=realm.name,
        distro=realm.distro or NONE_TEXT,
        state=realm.state or NONE_TEXT,
        tier=realm.tier or NONE_TEXT,
        services=services_label(realm.services),
        disk=disk_label(realm.disk),
        exported_apps=NONE_TEXT if realm.exported_apps is None else str(realm.exported_apps),
        # FR-024 update indicator. The CLI does not report it yet, so it stays None.
        updates=None,
        badge=badge_for(realm),
        notes=_notes(realm),
    )


class DashboardViewModel:
    """The rows and the banner of the dashboard, built from the latest applied listing."""

    def __init__(self) -> None:
        self._applied_request = 0
        self._rows: tuple[RealmRow, ...] = ()
        self._loaded = False
        self._failure: Failure | None = None

    @property
    def rows(self) -> tuple[RealmRow, ...]:
        return self._rows

    @property
    def failure(self) -> Failure | None:
        """The error of the last applied listing. Rows from an earlier read stay on screen."""
        return self._failure

    @property
    def loaded(self) -> bool:
        """True once a listing has been applied successfully."""
        return self._loaded

    @property
    def empty(self) -> bool:
        return self._loaded and not self._rows

    def apply(self, listing: RealmListing) -> bool:
        """Apply a listing. Return False, and change nothing, when it is older than the applied one.

        A failed listing sets the banner and keeps the rows of the last good read. It never clears
        them to a partial state.
        """
        if listing.request_id < self._applied_request:
            return False
        self._applied_request = listing.request_id
        if listing.realms is None:
            self._failure = failure_of(listing.result)
            return True
        self._rows = tuple(row_for(realm) for realm in listing.realms)
        self._loaded = True
        self._failure = None
        return True

    def row(self, name: str) -> RealmRow | None:
        for row in self._rows:
            if row.name == name:
                return row
        return None
