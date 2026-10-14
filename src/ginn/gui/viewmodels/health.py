# SPDX-License-Identifier: Apache-2.0
"""Host health viewmodel: doctor results and the integrity banner (FR-004..007, FR-104, IR-005).

`DoctorReport` and `IntegrityState` come from the backend, which reads `ginn --json doctor` and
`ginn --json integrity`. The banner blocks the screen while host files differ from the baseline
and no one has acknowledged the change (FR-104 AC-1). `acknowledge_args` is the argv of the
`ginn --json integrity --ack` call that the acknowledge action runs. Pure Python, no GTK.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ginn.gui.backend import HOST_CHANGED_CODE, DoctorReport, IntegrityState


class HealthBadge(StrEnum):
    """The overall doctor result. `unknown` before the first report arrives."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CheckRow:
    name: str
    status: str
    message: str
    fixes: tuple[str, ...]


@dataclass(frozen=True)
class IntegrityBanner:
    """The blocking banner for an unacknowledged host change. It lists each changed path."""

    title: str
    paths: tuple[str, ...]
    remedy: str


ACKNOWLEDGE_ARGS: tuple[str, ...] = ("--json", "integrity", "--ack")
BANNER_TITLE = "Host files changed since the baseline"
BANNER_REMEDY = "Review the listed paths, then acknowledge the change."
BLOCKED_TEXT = "Acknowledge the host change in Host health first (FR-104)"
"""The tooltip of an action that waits until the host change is acknowledged."""


class HealthViewModel:
    """The doctor rows, the overall badge and the integrity banner."""

    def __init__(self) -> None:
        self._report: DoctorReport | None = None
        self._integrity: IntegrityState | None = None

    @property
    def report(self) -> DoctorReport | None:
        return self._report

    def apply_doctor(self, report: DoctorReport) -> None:
        self._report = report

    def apply_integrity(self, state: IntegrityState) -> None:
        self._integrity = state

    @property
    def checks(self) -> tuple[CheckRow, ...]:
        if self._report is None:
            return ()
        return tuple(
            CheckRow(name=check.name, status=check.status, message=check.message, fixes=check.fixes)
            for check in self._report.checks
        )

    @property
    def badge(self) -> HealthBadge:
        if self._report is None:
            return HealthBadge.UNKNOWN
        statuses = {check.status for check in self._report.checks}
        if "FAIL" in statuses:
            return HealthBadge.FAIL
        if "WARN" in statuses:
            return HealthBadge.WARN
        return HealthBadge.OK

    @property
    def banner(self) -> IntegrityBanner | None:
        """The blocking banner, or None. It shows while a host change is unacknowledged."""
        state = self._integrity
        if state is None:
            return None
        blocking = state.status == "changed" or any(
            warning.code == HOST_CHANGED_CODE for warning in state.warnings
        )
        if not blocking:
            return None
        paths = state.changed or state.pending or ()
        return IntegrityBanner(title=BANNER_TITLE, paths=paths, remedy=BANNER_REMEDY)

    @property
    def blocks_mutations(self) -> bool:
        """True while the banner shows. The actions that change the host or the realms are off."""
        return self.banner is not None

    @property
    def acknowledge_args(self) -> tuple[str, ...]:
        """The argv of the acknowledge action (FR-104). Shown only while the banner is."""
        return ACKNOWLEDGE_ARGS
