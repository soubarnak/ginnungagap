# SPDX-License-Identifier: Apache-2.0
"""Error catalogue (NFR-040, IR-011).

Every GINN-E-* and GINN-W-* code that appears in GINN-SRS-001 has an entry. Some codes are
ginn-defined and the SRS does not name them: the CLI-level GINN-E-USAGE, GINN-E-INTERRUPTED and
GINN-E-INTERNAL, GINN-E-RECOVERY-PENDING (FR-122), GINN-E-HOST-NOT-SET-UP and
GINN-E-RUNTIME-DIR-UNSAFE (DR-010, ADR-004), GINN-E-SUBID-LOCK-UNSAFE (FR-001), GINN-E-DISTRO-DATA
(NFR-060), GINN-E-SUBID-LOCKED (FR-001), GINN-E-TRUST-DATA (DR-040), GINN-E-NO-SUCH-USER
(FR-001), GINN-E-SUBID-CONFLICT (FR-001, FR-002), GINN-E-SETUP-PATH-UNSAFE (FR-001, DR-009),
GINN-E-SETUP-BUSY (FR-001), GINN-E-UNDO-CGROUP-BUSY, GINN-E-UNDO-SERVICE-RUNNING and
GINN-E-UNDO-SUPERVISE-STATE (FR-003). `source` cites the requirement that defines the failure.

Exit statuses by category are in EXIT_CATEGORIES. Every catalogue entry's exit status must
appear there; tools/gen_errors_doc.py renders the table into docs/errors.md.

Summaries and remedies may hold {placeholders} that `make()` and `warn()` fill. A literal brace
is written as {{ or }}. `make()` and `warn()` reject missing and unexpected placeholder names.
"""

import string
from collections.abc import Mapping
from dataclasses import dataclass

EXEC_GINN_FAILURE = 125
"""Exit status for ginn-side failures of `ginn exec` and `ginn enter` (IR-011, FR-049).

`ginn exec` returns the inner command's exit status, so a ginn failure must not reuse a status
a command can return. The exec command remaps its own failures to this value (used in M4).
No catalogue entry uses it.
"""


class GinnError(Exception):
    """A failure with a catalogue code, a one-line remedy and a non-zero process exit status.

    `code` must be in CATALOGUE, and `exit_code` must equal the catalogue value. The one
    exception is EXEC_GINN_FAILURE (125), which `ginn exec` and `ginn enter` use to remap their
    own failures. Exit status 0 belongs to warnings, which are not exceptions, so it raises
    ValueError here (use `warn()`). `details` holds the values that filled the placeholders; the
    JSON error object carries them.
    """

    def __init__(
        self,
        code: str,
        message: str,
        remedy: str,
        exit_code: int = 1,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if exit_code == 0:
            raise ValueError(f"{code} is a warning (exit 0); use warn(), not an exception")
        spec = CATALOGUE.get(code)
        if spec is None:
            raise ValueError(f"{code} is not in the error catalogue")
        if spec.exit_code == 0:
            raise ValueError(f"{code} is a warning (exit 0); use warn(), not an exception")
        if exit_code not in (spec.exit_code, EXEC_GINN_FAILURE):
            raise ValueError(f"{code} exits {spec.exit_code} in the catalogue, not {exit_code}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.remedy = remedy
        self.exit_code = exit_code
        self.details: dict[str, object] = dict(details or {})


@dataclass(frozen=True)
class Warning:
    """A non-fatal GINN-W-* notice. The command keeps going and exits 0."""

    code: str
    message: str
    remedy: str


@dataclass(frozen=True)
class ErrorSpec:
    """One catalogue entry. Warnings (GINN-W-*) use the same record with exit_code 0."""

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
        code="GINN-E-HOST-NOT-SET-UP",
        exit_code=77,
        summary="runtime directory {path} does not exist",
        remedy="run 'ginn doctor'; if Host Setup has not run, run 'sudo ginn host-setup --user "
        "<name>'; otherwise check 'sudo sv status ginn-<user>'",
        source="DR-010; ADR-004 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-RUNTIME-DIR-UNSAFE",
        exit_code=77,
        summary="runtime directory {path} must be a real directory owned by uid {uid} with mode "
        "0700; found {found}",
        remedy="run 'ginn doctor' to see the cause; if Host Setup has not run, run 'sudo ginn "
        "host-setup --user <name>'; otherwise check 'sudo sv status ginn-<user>'",
        source="DR-010; ADR-004 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-SUBID-LOCK-UNSAFE",
        exit_code=77,
        summary="stale subordinate ID lock {lock} cannot be reclaimed: "
        "its reclaim gate {path} is a symbolic link",
        remedy="as root, run 'ls -l {path}' to see where the link points, then run 'rm {path}' to "
        "remove the link itself, not its target; then run 'sudo ginn host-setup --user <name>' "
        "again",
        source="FR-001 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-SUBID-LOCKED",
        exit_code=75,
        summary="subordinate ID lock {lock} is held by another process ({holder})",
        remedy="check for a running 'ginn host-setup'; if none is running, remove {lock} by hand, "
        "then run 'sudo ginn host-setup --user <name>' again",
        source="FR-001 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-NO-SUCH-USER",
        exit_code=2,
        summary="no account named '{user}' on this host",
        remedy="create that account first, or run 'sudo ginn host-setup --user <name>' with the "
        "name of an existing account",
        source="FR-001 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-SUBID-CONFLICT",
        exit_code=4,
        summary="subordinate ID range {start}:{count} for {user} is no longer free in {path}",
        remedy="free the overlapping line in {path} without changing the recorded range, then run "
        "'sudo ginn host-setup --user {user}' again; Host Setup never picks another range",
        source="FR-001 AC-1, FR-002 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-SETUP-PATH-UNSAFE",
        exit_code=77,
        summary="Host Setup refuses {path}: {reason}",
        remedy="inspect {path} with 'sudo ls -ld {path}' and 'sudo readlink -f {path}'; for a Host "
        "Setup Record, keep a copy with 'sudo cp -p' before any change, because it holds the "
        "pre-images; for a system path, correct it in place by hand; then run 'sudo ginn "
        "host-setup --user <name>' again",
        source="FR-001, DR-009 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-SETUP-BUSY",
        exit_code=75,
        summary="another Host Setup holds {lock}; gave up after {seconds} s",
        remedy="another 'ginn host-setup' (or its --undo) holds "
        "/run/lock/ginn-host-setup.lock; the kernel releases that flock when its holder exits, so "
        "no stale lock exists; wait for that run to finish, then run 'sudo ginn host-setup --user "
        "<name>' again",
        source="FR-001 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-UNDO-CGROUP-BUSY",
        exit_code=75,
        summary="cgroup {path} is still busy after {seconds} s; undo stopped before its cleanup",
        remedy="run 'sudo ls {path}' and 'sudo cat {path}/cgroup.procs' to see what is still in "
        "it; stop those processes, and remove any child cgroup with 'sudo rmdir' once it is empty; "
        "then run 'sudo ginn host-setup --undo --user {user}' again; the Host Setup Record is kept",
        source="FR-003 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-UNDO-REALMS-EXIST",
        exit_code=4,
        summary="undo refused: user {user} still has realms",
        remedy="delete those realms first with 'ginn delete <realm>'",
        source="FR-003 AC-2",
    ),
    ErrorSpec(
        code="GINN-E-UNDO-SERVICE-RUNNING",
        exit_code=75,
        summary="service ginn-{user} is still running after {seconds} s; undo stopped before its "
        "cleanup",
        remedy="run 'sudo sv force-stop /etc/sv/ginn-{user}', then 'sudo sv exit "
        "/etc/sv/ginn-{user}', then run 'sudo ginn host-setup --undo --user {user}' again; the "
        "Host Setup Record is kept",
        source="FR-003 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-UNDO-SUPERVISE-STATE",
        exit_code=77,
        summary="runsv state {path} is a {state}, but runit keeps a FIFO there; undo stopped "
        "before its cleanup",
        remedy="run 'sudo ls -l {path}' to see what it is; if no runsv runs for ginn-{user}, "
        "remove that entry by hand, then run 'sudo ginn host-setup --undo --user {user}' again; "
        "the Host Setup Record is kept",
        source="FR-003 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-HOST-CHANGED",
        exit_code=0,
        summary="host files changed since baseline: {paths}",
        remedy="review the listed paths; the likely cause is xbps or a system file edit made while "
        "ginn was running (R-004); then run 'ginn integrity --ack'",
        source="FR-006 AC-1; FR-104 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-HOST-UNACKED",
        exit_code=4,
        summary="host integrity warning not acknowledged; changed paths: {paths}",
        remedy="review the listed paths, then run 'ginn integrity --ack' and retry the command",
        source="FR-006 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-NAME-TAKEN",
        exit_code=4,
        summary="realm name '{name}' is already taken",
        remedy="choose another name with --name",
        source="FR-010 AC-3",
    ),
    ErrorSpec(
        code="GINN-E-NAME-INVALID",
        exit_code=2,
        summary="realm name '{name}' does not match ^[a-z][a-z0-9-]{{0,31}}$ or is reserved",
        remedy="use a lowercase letter first, then lowercase letters, digits or hyphens; "
        "not ginn, host, root or all",
        source="DR-002",
    ),
    ErrorSpec(
        code="GINN-E-IMAGE-UNVERIFIED",
        exit_code=65,
        summary="image digest for {distro} does not match the trust manifest",
        remedy="do not use this image; retry later, and update ginn if the mismatch persists",
        source="FR-011 AC-1",
    ),
    ErrorSpec(
        code="GINN-E-PKG-SIGNATURE",
        exit_code=65,
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
        exit_code=3,
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
        code="GINN-E-PKG-MANAGER",
        exit_code=1,
        summary="native package manager failed in realm '{realm}'",
        remedy="run again with --verbose to see the package manager output, fix its cause, "
        "then retry",
        source="FR-040; FR-120",
    ),
    ErrorSpec(
        code="GINN-E-DOCTOR-FAILED",
        exit_code=1,
        summary="{failed} of {total} doctor checks failed",
        remedy="run the command printed under each failing check, then run 'ginn doctor' again",
        source="FR-007 AC-1",
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
        code="GINN-E-RECOVERY-PENDING",
        exit_code=4,
        summary="realm '{realm}' has an interrupted {op} that was never recovered",
        remedy="if the interrupted {op} was a create, run 'ginn create --resume {realm}' or "
        "'ginn delete {realm}'; otherwise run 'ginn doctor' and retry the {op}, and do not delete "
        "the realm",
        source="FR-122, NFR-030 (ginn-defined wording; not an SRS code)",
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
    ErrorSpec(
        code="GINN-E-DISTRO-DATA",
        exit_code=70,
        summary="shipped distro data {file} is invalid: {field} {rule}",
        remedy="the shipped distro data is invalid; reinstall ginn, or report a bug with this "
        "message",
        source="NFR-060 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-TRUST-DATA",
        exit_code=70,
        summary="shipped trust data {source} is invalid: {problem}",
        remedy="the shipped trust data is invalid; reinstall ginn, or report a bug with this "
        "message",
        source="DR-040 (ginn-defined; not an SRS code)",
    ),
)

CATALOGUE: dict[str, ErrorSpec] = {spec.code: spec for spec in _SPECS}

EXIT_CATEGORIES: tuple[tuple[int, str], ...] = (
    (0, "success. `GINN-W-*` warnings also keep 0: they are notices, not exceptions."),
    (1, "operational failure: GINN-E-PKG-MANAGER, GINN-E-DOCTOR-FAILED"),
    (2, "input validation: usage, names, options, paths"),
    (3, "not found: GINN-E-NO-SUCH-REALM"),
    (
        4,
        "conflict with existing state: name taken, undo refused, unacknowledged host change, "
        "interrupted operation not yet recovered",
    ),
    (65, "data error (EX_DATAERR): image or package signature not verified"),
    (69, "network failure"),
    (70, "internal error (EX_SOFTWARE)"),
    (73, "not enough disk space"),
    (
        75,
        "realm, subordinate ID file or Host Setup busy, or a service still running at undo "
        "(EX_TEMPFAIL)",
    ),
    (
        77,
        "Host Setup must run as root, has not run for this user, or refuses an unsafe path it "
        "cannot repair (EX_NOPERM)",
    ),
    (EXEC_GINN_FAILURE, "reserved; see EXEC_GINN_FAILURE"),
    (130, "interrupted by the user"),
)
"""Exit statuses by category, in ascending order (NFR-040, IR-010, IR-011)."""


def placeholders(text: str) -> frozenset[str]:
    """Return the {field} names in a catalogue template. Escaped braces are not fields."""
    return frozenset(
        name for _literal, name, _spec, _conv in string.Formatter().parse(text) if name is not None
    )


def _render(code: str, spec: ErrorSpec, fmt: Mapping[str, object]) -> tuple[str, str]:
    """Fill summary and remedy. Missing or unexpected placeholder names raise ValueError."""
    fields = placeholders(spec.summary) | placeholders(spec.remedy)
    not_names = sorted(name for name in fields if not name.isidentifier())
    if not_names:
        raise ValueError(f"{code}: placeholder names must be identifiers, got {not_names}")
    missing = sorted(fields - set(fmt))
    unexpected = sorted(set(fmt) - fields)
    if missing or unexpected:
        raise ValueError(
            f"{code}: missing fields {missing or 'none'}, unexpected fields {unexpected or 'none'}"
        )
    return spec.summary.format(**fmt), spec.remedy.format(**fmt)


def make(code: str, **fmt: object) -> GinnError:
    """Build a GinnError for an error code, filling {placeholders} from `fmt`."""
    spec = CATALOGUE[code]
    if spec.exit_code == 0:
        raise ValueError(f"{code} is a warning (exit 0); use warn(), not make()")
    message, remedy = _render(code, spec, fmt)
    return GinnError(code, message, remedy, spec.exit_code, details=fmt)


def warn(code: str, **fmt: object) -> Warning:
    """Build a Warning for a GINN-W-* code, filling {placeholders} from `fmt`."""
    spec = CATALOGUE[code]
    if spec.exit_code != 0:
        raise ValueError(f"{code} is an error (exit {spec.exit_code}); use make(), not warn()")
    message, remedy = _render(code, spec, fmt)
    return Warning(code, message, remedy)
