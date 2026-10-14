# SPDX-License-Identifier: Apache-2.0
"""Error catalogue (NFR-040, IR-011).

Every GINN-E-* and GINN-W-* code that appears in GINN-SRS-001 has an entry. Some codes are
ginn-defined and the SRS does not name them: the CLI-level GINN-E-USAGE, GINN-E-INTERRUPTED and
GINN-E-INTERNAL, GINN-E-RECOVERY-PENDING (FR-122), GINN-E-HOST-NOT-SET-UP and
GINN-E-RUNTIME-DIR-UNSAFE (DR-010, ADR-004), GINN-E-SUBID-LOCK-UNSAFE (FR-001), GINN-E-DISTRO-DATA
(NFR-060), GINN-E-SUBID-LOCKED (FR-001), GINN-E-TRUST-DATA (DR-040), GINN-E-NO-SUCH-USER
(FR-001), GINN-E-SUBID-CONFLICT (FR-001, FR-002), GINN-E-SETUP-PATH-UNSAFE (FR-001, DR-009),
GINN-E-SETUP-BUSY (FR-001), GINN-E-UNDO-CGROUP-BUSY, GINN-E-UNDO-SERVICE-RUNNING and
GINN-E-UNDO-SUPERVISE-STATE (FR-003), GINN-E-REALM-STATE (DR-020), GINN-E-ATTACH-FAILED
(ADR-008), GINN-E-REALM-USER (FR-048, ADR-006), GINN-E-AGENT-FILTER (FR-067),
GINN-E-DBUS-PROXY (FR-068), GINN-E-NVIDIA-STALE and GINN-E-NVIDIA-CDI (FR-069), and
GINN-E-SUPERVISOR-DOWN, GINN-E-SERVICE-TIMEOUT, GINN-E-SERVICE-CONTROL and GINN-E-SERVICE-MISSING
(FR-081, AS-028), GINN-E-NO-INIT (FR-080, F3), GINN-E-RELAY-UNAVAILABLE, GINN-E-RELAY-REJECTED,
GINN-E-RELAY-TIMEOUT, GINN-E-RELAY-LOST and GINN-E-RELAY-BUSY (FR-049, ADR-014),
GINN-W-RELAY-ORPHAN (FR-123, ADR-014), and GINN-W-RELAY-SYNC (FR-084, ADR-014). `source` cites
the requirement that defines the failure.

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
a command can return. The exec and enter run (ginn/cli/_enter.py) remaps a ginn-side failure to
this value: a GinnError, or any other exception, which becomes GINN-E-INTERNAL. Only a GINN-E line
or a JSON error object on stderr marks a ginn failure. A 125 that podman's own exec returns, or the
inner command returns, is passed through unchanged, and the exit status alone cannot tell them
apart. argparse refusals (exit 2) and Ctrl-C (130) are the documented exceptions: they are raised
before the realm is touched, or by the interrupt itself.
No catalogue code has this exit status.
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
        "not ginn, host, root, all or supervisor",
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
        code="GINN-E-PKG-DEPENDENTS",
        exit_code=4,
        summary="removing {packages} in realm '{realm}' would also remove {dependents}",
        remedy="remove the dependent packages first with 'ginn remove {realm} PACKAGE', or keep "
        "the package installed; ginn does not force a removal",
        source="FR-041; ADR-011 (ginn-defined; not an SRS code)",
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
    ErrorSpec(
        code="GINN-E-ATTACH-FAILED",
        exit_code=1,
        summary="attaching the grants into the realm failed: {detail}",
        remedy="retry the start, enter or launch; if it fails again, run 'ginn doctor' and report "
        "the detail",
        source="ADR-008 (T4.10 notes); FR-061, FR-066 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-REALM-USER",
        exit_code=1,
        summary="the realm user of '{realm}' cannot be set up: {detail}",
        remedy="if the detail shows a line for the host uid that is not the expected entry, the "
        "image already has that uid, and keep-id may then write no entry: use a different distro, "
        "or report a bug; if the host home holds '$', ':' or a newline, move it; if the host user "
        "name is not a realm user name, a retry cannot change it, so report it; otherwise retry "
        "the create, and report the detail if it repeats",
        source="FR-048, ADR-006 amendment of 2026-10-13 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-AGENT-FILTER",
        exit_code=1,
        summary="the SSH agent filter did not start: {detail}",
        remedy="check that SSH_AUTH_SOCK names a running agent socket and that the ginn runtime "
        "directory is writable, then retry the start, enter or launch",
        source="FR-067 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-DBUS-PROXY",
        exit_code=1,
        summary="the session bus filter for '{realm}' did not start: {detail}",
        remedy="check that the host session bus is running and that xdg-dbus-proxy is installed, "
        "then retry the launch",
        source="FR-068, AS-012 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-NVIDIA-STALE",
        exit_code=1,
        summary="the NVIDIA libraries do not match the kernel driver: {detail}",
        remedy="retry the GPU start, which regenerates the spec from the installed driver; if it "
        "repeats, reinstall the NVIDIA driver so that its libraries and kernel module match",
        source="FR-069, R-005 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-NVIDIA-CDI",
        exit_code=1,
        summary="podman cannot resolve the GPU's CDI devices: {detail}",
        remedy="re-run 'ginn doctor'; if the NVIDIA CDI spec under ginn's cdi directory is "
        "missing, start the GPU realm again so the spec is written, then retry",
        source="FR-069, ADR-012 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-HOST-DEVICE",
        exit_code=1,
        summary="the host has no {device}, which {feature} needs",
        remedy="load the kernel module that provides the device, then retry the create; ginn does "
        "not make a nested-container realm without it",
        source="FR-062 AC-3, ADR-006 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-SECCOMP-SOURCE",
        exit_code=1,
        summary="the seccomp profile of a nested-container realm cannot be made from {source}: "
        "{detail}",
        remedy="install the package that provides the host's default seccomp profile "
        "(containers-common on Void), then retry the create; ginn never runs such a realm without "
        "the profile",
        source="ADR-006 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-TIER-NOT-CUSTOM",
        exit_code=4,
        summary="realm '{realm}' is {tier}, so its shares and toggles cannot change",
        remedy="run 'ginn tier {realm} custom' first; the shares and toggles belong to a Custom "
        "realm, and a Basic or Development realm has its permissions from its tier",
        source="FR-063 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-REALM-STATE",
        exit_code=4,
        summary="realm '{realm}' is {state}; {action} needs it {allowed}",
        remedy="run 'ginn status {realm}', then the command that state allows; a broken realm "
        "from an interrupted create is resumed with 'ginn create --resume {realm}', and any other "
        "broken realm is removed with 'ginn delete {realm}'",
        source="DR-020 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-HOME-UNSAFE",
        exit_code=77,
        summary="realm home '{path}' cannot be removed: {reason}",
        remedy="inspect '{path}' and fix it by hand, then run 'ginn delete {realm} --purge-home' "
        "again; 'ginn delete {realm}' removes the realm and keeps the home",
        source="DR-010, DR-021 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-NOT-SUPPORTED-YET",
        exit_code=2,
        summary="realm '{realm}' cannot be used yet: {feature} arrives in {milestone}",
        remedy="use a realm this build can run; docs/roadmap.md lists the milestones",
        source="FR-026, FR-027, ADR-003 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-SUPERVISOR-DOWN",
        exit_code=1,
        summary="the runit supervisor of realm '{realm}' is not running: {detail}",
        remedy="run 'ginn doctor' and follow its runtime-dir and runsvdir checks, which name the "
        "fix; then run the command again",
        source="FR-081, ADR-003 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-SERVICE-TIMEOUT",
        exit_code=1,
        summary="the runit service of realm '{realm}' did not {action} in {seconds} s: {detail}",
        remedy="run 'ginn status {realm}' for its runit state and 'ginn log {realm}' for the "
        "reason; a realm that crashes five times in 60 s is given up (AS-028)",
        source="FR-081, AS-028 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-SERVICE-CONTROL",
        exit_code=1,
        summary="the runit service of realm '{realm}' did not take the {action} request: {detail}",
        remedy="run 'ginn status {realm}' and 'ginn doctor'; the realm may still be running, so no "
        "fallback stopped it. Run the command again once the runit checks pass",
        source="FR-081 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-SERVICE-MISSING",
        exit_code=1,
        summary="the runit service directory of realm '{realm}' is missing: {detail}",
        remedy="run 'ginn doctor' to see the runit service of the realm; 'ginn delete {realm}' "
        "removes a realm whose service directory is gone",
        source="FR-081, DR-010 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-NO-INIT",
        exit_code=1,
        summary="services-on needs systemd in the realm: image '{image}' of realm '{realm}' has "
        "no /sbin/init",
        remedy="create the realm with services off, or pick a distribution whose image ships "
        "systemd; the create is rolled back and no container is left",
        source="FR-080, ADR-003, F3 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-RELAY-UNAVAILABLE",
        exit_code=1,
        summary="the exec relay of realm '{realm}' does not answer after 'sv up': {detail}",
        remedy="run 'ginn doctor' and follow its runsvdir checks; if the '_exec' service is "
        "missing, run 'ginn services {realm} on' to install it (or 'ginn start {realm}' when the "
        "realm is stopped); then run the command again",
        source="FR-049, FR-083, ADR-014 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-RELAY-REJECTED",
        exit_code=1,
        summary="the exec relay of realm '{realm}' refused the request: {reason}",
        remedy="this is a bug or a mismatch between the ginn CLI and the exec relay, not a fault "
        "in your command; report it with this message, and reinstall or update ginn so both parts "
        "match, then run the command again",
        source="FR-049, NFR-SEC-005, ADR-014 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-RELAY-TIMEOUT",
        exit_code=1,
        summary="the exec relay of realm '{realm}' did not confirm the start in {seconds} s",
        remedy="run 'ginn status {realm}'; if the command may have started, check its result "
        "before you run it again, then retry",
        source="FR-049, ADR-014 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-RELAY-LOST",
        exit_code=1,
        summary="the exec relay of realm '{realm}' lost the command before its exit status "
        "arrived, so the exit status is unknown",
        remedy="the command may still be running in the realm: wait for it to finish, run 'ginn "
        "status {realm}', and check what the command did before you run it again",
        source="FR-049, FR-124, ADR-014 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-RELAY-BUSY",
        exit_code=75,
        summary="the exec relay is at its limit of {limit} running commands, so realm '{realm}' "
        "got no worker",
        remedy="wait for a running 'ginn exec' or 'ginn enter' to finish, then retry the command",
        source="FR-049, ADR-014 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-RELAY-ORPHAN",
        exit_code=0,
        summary="processes of a command in realm '{realm}' survived the kill after its client was "
        "lost: pids {pids}",
        remedy="the exec lock is released, so other commands can run; check the listed pids with "
        "'ps -p {pids}' and stop any that the interrupted command left running with 'kill'",
        source="FR-123, FR-124, ADR-014 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-RELAY-SYNC",
        exit_code=0,
        summary="the relay service could not follow the realm states after the operation was "
        "recorded: {detail}",
        remedy="the operation keeps its result; the next start, stop or services change syncs the "
        "relay again, and if it keeps failing, run 'ginn doctor'",
        source="FR-084, ADR-014 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-RECOVERED",
        exit_code=0,
        summary="recovery settled an interrupted {op} of realm '{realm}' as {outcome}: {reason}",
        remedy="run 'ginn status {realm}'; an interrupted create is resumed with "
        "'ginn create --resume {realm}', and a broken realm is removed with 'ginn delete {realm}'",
        source="FR-122 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-RECOVERY-UNSETTLED",
        exit_code=0,
        summary="recovery could not settle an interrupted operation: {detail}",
        remedy="run 'ginn doctor' and inspect the journal file named above; do not delete the "
        "realm until it is settled",
        source="FR-122 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-BUILD-RESIDUAL",
        exit_code=0,
        summary="packages used only to build an AUR package are left in realm '{realm}': "
        "{packages}",
        remedy="check why they were kept ({reason}), then remove them with 'ginn remove {realm} "
        "PACKAGE' if they are not needed",
        source="FR-043 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-MANIFEST-UNRECORDED",
        exit_code=0,
        summary="packages changed in a root session of realm '{realm}' are not recorded: {detail}",
        remedy="run 'ginn install {realm} PACKAGE' for each package the session installed, "
        "to record it in the Install Manifest",
        source="FR-042 AC-1, DR-007 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-BIN-NAME-INVALID",
        exit_code=2,
        summary="binary name '{binary}' is not valid",
        remedy="use a plain file name of letters, digits, dot, plus, underscore or hyphen, with no "
        "slash; the names 'ginn', 'ginn-*' and 'podman' are reserved",
        source="FR-047 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-BIN-NOT-EXPORTED",
        exit_code=3,
        summary="binary '{binary}' is not exported from realm '{realm}'",
        remedy="check the realm and binary names; export it with 'ginn export --bin <realm> "
        "<binary>'",
        source="FR-047 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-BIN-EXISTS",
        exit_code=4,
        summary="'{path}' exists and is not a launcher of realm '{realm}'",
        remedy="unexport the other launcher first, or choose another name; ginn never overwrites "
        "a file it did not write",
        source="FR-047; DR-010 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-E-EXPORT-LIST",
        exit_code=1,
        summary="cannot list the desktop entries of realm '{realm}': {reason}",
        remedy="check that the realm runs with 'ginn status {realm}'; nothing was exported or "
        "removed",
        source="FR-044; FR-045 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-AGENT-FILTER",
        exit_code=0,
        summary="the SSH agent filter is not in line for realm '{realm}': {detail}",
        remedy="the realm's SSH grant may be missing or stale; start the realm again from a "
        "session with a running SSH agent, or run 'ginn doctor' to see the filter",
        source="FR-067 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-NVIDIA-STALE",
        exit_code=0,
        summary="the NVIDIA libraries do not match the kernel driver, so realm '{realm}' gets no "
        "GPU grant: {detail}",
        remedy="reinstall the NVIDIA driver so that its libraries and kernel module match, then "
        "create the realm again to get the GPU grant",
        source="FR-069, R-005 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-PROXY-RELEASE",
        exit_code=0,
        summary="the D-Bus proxy of realm '{realm}' was not released: {detail}",
        remedy="the command finished; run the realm again, or stop it, so the proxy is stopped",
        source="FR-068, ADR-007 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-DBUS-PROXY",
        exit_code=0,
        summary="the session bus filter for '{realm}' did not start, so the app has no session "
        "bus: {detail}",
        remedy="the app runs without a session bus; check that the host session bus is running "
        "and that xdg-dbus-proxy is installed, then launch the app again",
        source="FR-068, ADR-010 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-EXPORT-SKIPPED",
        exit_code=0,
        summary="desktop entry '{app}' of realm '{realm}' is not exported: {reason}",
        remedy="fix the entry in the realm or remove its package; the next sync follows the realm",
        source="FR-044 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-EXPORT-SYNC",
        exit_code=0,
        summary="the host menu of realm '{realm}' is not fully synced: {reason}",
        remedy="the realm and its packages are kept as they are; run 'ginn _sync {realm}' to retry "
        "the menu",
        source="FR-044 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-ICON-SKIPPED",
        exit_code=0,
        summary="icon of desktop entry '{app}' in realm '{realm}' is not copied: {reason}",
        remedy="the host menu shows the entry without this icon",
        source="FR-044 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-SERVICES-PENDING",
        exit_code=0,
        summary="realm '{realm}' is created stopped with services on: its container is made, and "
        "nothing is started or installed in it",
        remedy="a services-on realm starts through its runit entry ('ginn start'); its Integration "
        "Package Set comes with 'ginn rebuild', which a plain start does not run",
        source="FR-010, FR-080, ADR-003 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-CGROUP-LEFT",
        exit_code=0,
        summary="the cgroup directory of realm '{realm}' is not removed: {reason}",
        remedy="it holds no ginn state; once no process runs in it, 'rmdir' of that directory "
        "removes it, and a realm created again reuses it",
        source="FR-019, ADR-003 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-REBUILD-DEFERRED",
        exit_code=0,
        summary="the pending change of realm '{realm}' is not applied: its rebuild failed "
        "({reason}); the realm starts on its current root",
        remedy="the change stays pending; fix the cause and run 'ginn start {realm}' to retry the "
        "rebuild, or 'ginn rebuild {realm}' to rebuild now",
        source="FR-021 AC-2, NFR-031 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-REBUILD-PENDING",
        exit_code=0,
        summary="realm '{realm}': the next start rebuilds it into a new container, because "
        "{change}",
        remedy="its packages and Realm Home are kept, and the rebuild pulls the pinned image and "
        "replays the packages without a prompt (CL-19); run 'ginn rebuild {realm}' to do it now",
        source="FR-066, FR-083, CL-19 (ginn-defined; not an SRS code)",
    ),
    ErrorSpec(
        code="GINN-W-IDLE-STOP",
        exit_code=0,
        summary="realm '{realm}' will not stop when it is idle: {detail}",
        remedy="the realm keeps running until 'ginn stop {realm}'; the idle stop (FR-018) is armed "
        "by the next start, once the native helpers are installed (make -C native install)",
        source="FR-018, NFR-002 (ginn-defined; not an SRS code)",
    ),
)

CATALOGUE: dict[str, ErrorSpec] = {spec.code: spec for spec in _SPECS}

EXIT_CATEGORIES: tuple[tuple[int, str], ...] = (
    (0, "success. `GINN-W-*` warnings also keep 0: they are notices, not exceptions."),
    (
        1,
        "operational failure: GINN-E-PKG-MANAGER, GINN-E-DOCTOR-FAILED, GINN-E-ATTACH-FAILED, "
        "GINN-E-REALM-USER, GINN-E-AGENT-FILTER, GINN-E-DBUS-PROXY, GINN-E-NVIDIA-STALE, "
        "GINN-E-NVIDIA-CDI, GINN-E-HOST-DEVICE, GINN-E-SECCOMP-SOURCE, GINN-E-SUPERVISOR-DOWN, "
        "GINN-E-SERVICE-TIMEOUT, GINN-E-SERVICE-CONTROL, GINN-E-SERVICE-MISSING, GINN-E-NO-INIT, "
        "GINN-E-RELAY-UNAVAILABLE, GINN-E-RELAY-REJECTED, GINN-E-RELAY-TIMEOUT, GINN-E-RELAY-LOST",
    ),
    (2, "input validation: usage, names, options, paths"),
    (3, "not found: GINN-E-NO-SUCH-REALM"),
    (
        4,
        "conflict with existing state: name taken, undo refused, unacknowledged host change, "
        "interrupted operation not yet recovered, a realm in a state its verb does not allow "
        "(GINN-E-REALM-STATE), a removal that would take other packages (GINN-E-PKG-DEPENDENTS), "
        "a realm whose shares and toggles need a Custom tier (GINN-E-TIER-NOT-CUSTOM)",
    ),
    (65, "data error (EX_DATAERR): image or package signature not verified"),
    (69, "network failure"),
    (70, "internal error (EX_SOFTWARE)"),
    (73, "not enough disk space"),
    (
        75,
        "realm, subordinate ID file or Host Setup busy, the exec relay at its worker limit "
        "(GINN-E-RELAY-BUSY), or a service still running at undo (EX_TEMPFAIL)",
    ),
    (
        77,
        "Host Setup must run as root, has not run for this user, or refuses an unsafe path it "
        "cannot repair (EX_NOPERM)",
    ),
    (
        EXEC_GINN_FAILURE,
        "ginn-side failure of `ginn exec` and `ginn enter`, marked by a GINN-E line or JSON error "
        "object on stderr; a 125 from podman or from the command is passed through unchanged",
    ),
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
