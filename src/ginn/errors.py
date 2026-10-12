# SPDX-License-Identifier: Apache-2.0
"""Error catalogue (NFR-040).

Every GINN-E-* and GINN-W-* code that appears in GINN-SRS-001 has an entry, plus the CLI-level
codes GINN-E-USAGE, GINN-E-INTERRUPTED and GINN-E-INTERNAL. `source` cites the requirement that
defines the failure. Summaries and remedies may hold {placeholders} that `make()` fills.
"""

from dataclasses import dataclass


class GinnError(Exception):
    """A failure with a catalogue code, a one-line remedy and a process exit status."""

    def __init__(self, code: str, message: str, remedy: str, exit_code: int = 1) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.remedy = remedy
        self.exit_code = exit_code


@dataclass(frozen=True)
class ErrorSpec:
    """One catalogue entry."""

    code: str
    exit_code: int
    summary: str
    remedy: str
    source: str


_SPECS: tuple[ErrorSpec, ...] = (
    ErrorSpec(
        code="GINN-E-SETUP-ROOT",
        exit_code=77,
        summary="Host Setup must run as root",
        remedy="run 'sudo ginn host-setup --user <name>'",
        source="FR-001 AC-2",
    ),
    ErrorSpec(
        code="GINN-E-UNDO-REALMS-EXIST",
        exit_code=1,
        summary="undo refused: user {user} still has realms",
        remedy="delete those realms first with 'ginn delete <realm>'",
        source="FR-003 AC-2",
    ),
    ErrorSpec(
        code="GINN-W-HOST-CHANGED",
        exit_code=0,
        summary="host files changed since baseline: {paths}",
        remedy="review the listed paths, then run 'ginn integrity --ack'",
        source="FR-006 AC-1; FR-104 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-NAME-TAKEN",
        exit_code=2,
        summary="realm name '{name}' is already taken",
        remedy="choose another name with --name",
        source="FR-010 AC-3",
    ),
    ErrorSpec(
        code="GINN-E-IMAGE-UNVERIFIED",
        exit_code=1,
        summary="image digest for {distro} does not match the trust manifest",
        remedy="do not use this image; retry later, and update ginn if the mismatch persists",
        source="FR-011 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-PKG-SIGNATURE",
        exit_code=1,
        summary="package signature check failed in realm '{realm}'",
        remedy="fix the repository keys; ginn never skips signature checks",
        source="FR-012 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-OPTION-INVALID",
        exit_code=2,
        summary="option {option} is not valid for distro {distro}",
        remedy="remove the option, or choose a distro that supports it",
        source="FR-014 AC-3",
    ),
    ErrorSpec(
        code="GINN-W-REPLAY-MISSING",
        exit_code=0,
        summary="package {package} is no longer available upstream",
        remedy="reinstall it later with 'ginn install' if it returns",
        source="FR-022 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-NO-SUCH-REALM",
        exit_code=2,
        summary="no realm named '{realm}' for this user",
        remedy="run 'ginn list' to see your realms",
        source="FR-025 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-PKG-NAME-INVALID",
        exit_code=2,
        summary="package name '{name}' is not valid",
        remedy="use plain package names with no wildcards or shell characters",
        source="FR-040 AC-2",
    ),
    ErrorSpec(
        code="GINN-E-PATH-FORBIDDEN",
        exit_code=2,
        summary="path '{path}' may not be shared with a realm",
        remedy="choose another path; see DR-004",
        source="FR-063 AC-2; FR-103 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-NETWORK",
        exit_code=69,
        summary="network failure; retries exhausted",
        remedy="check the network connection, then retry the command",
        source="FR-120 AC-1, AC-2",
    ),
    ErrorSpec(
        code="GINN-E-DISK-SPACE",
        exit_code=73,
        summary="not enough disk space: need {needed}, have {available}",
        remedy="free space on the partition that holds ginn data, then retry",
        source="FR-121 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-REALM-BUSY",
        exit_code=75,
        summary="{op} in progress",
        remedy="wait for the running operation to finish, then retry",
        source="FR-123 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-USAGE",
        exit_code=2,
        summary="{detail}",
        remedy="run 'ginn --help' to list commands and options",
        source="IR-011",
    ),
    ErrorSpec(
        code="GINN-E-INTERRUPTED",
        exit_code=130,
        summary="interrupted by the user",
        remedy="run 'ginn list' to check realm state, then retry",
        source="IR-011",
    ),
    ErrorSpec(
        code="GINN-E-INTERNAL",
        exit_code=70,
        summary="internal error; this is a bug",
        remedy="re-run with --verbose and report the output",
        source="NFR-040",
    ),
)

CATALOGUE: dict[str, ErrorSpec] = {spec.code: spec for spec in _SPECS}


def make(code: str, **fmt: object) -> GinnError:
    """Build a GinnError for a catalogue code, filling {placeholders} from `fmt`."""
    spec = CATALOGUE[code]
    return GinnError(
        code,
        spec.summary.format(**fmt),
        spec.remedy.format(**fmt),
        spec.exit_code,
    )
