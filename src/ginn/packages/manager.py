# SPDX-License-Identifier: Apache-2.0
"""Package manager verbs of a realm, run as container root (FR-012, FR-040, FR-041, FR-042, FR-120).

Every verb is the argv that `DistroDef.argv` builds from the distribution data (NFR-060), as one
list. No argv is built from a string, and none goes through a shell (NFR-SEC-005). Each call runs
as container root (ADR-008, FR-042) with `LC_ALL=C.UTF-8` (ADR-011, the Locale rule), so the output
that ginn parses is in English.

A non-zero status is classified in this order (`failure`, `network_failure`):
1. A signature match is GINN-E-PKG-SIGNATURE (FR-012 AC-1). It wins over every other text.
2. A match of the distribution's `network_error` pattern is a transient network failure. `run_step`
   retries it through the caller's hook with the FR-120 delays, and after the last retry it is
   GINN-E-NETWORK.
3. Anything else is GINN-E-PKG-MANAGER. A stale-metadata match adds `stale_metadata` to its details
   (ADR-011). Stale metadata is not retried, because a retry cannot fix it.

Bounds, fail closed. Package-manager output comes from the realm, so ginn reads a list or a dry run
under caps: `MAX_OUTPUT_CHARS` in total and `MAX_LINE_CHARS` per line, and `MAX_NAMES` names.
Above a cap the output is refused with GINN-E-PKG-MANAGER, and nothing is decided from it. Any name
or line that reaches a message goes through `printable` and `shown`, so a control character from
the realm is written escaped, and a long list shows `MAX_SHOWN` names and a count.

Removal pre-check (ADR-011). On Debian, Kali and Fedora the removal dry run runs first, and
`removal_dependents` reads its output. The line shapes it reads are not verified on a host until
M4, and `parse_removals` lists them. A requested name that the parser does not find makes the check
fail, so an unread plan never lets a removal go ahead.
"""

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

from ginn.distros import DistroDef, Verb
from ginn.errors import GinnError, make
from ginn.podman.adapter import Podman, RetryHook, with_network_retry
from ginn.podman.models import ExecResult

ROOT_USER: Final = "0:0"
"""The user of every package-manager call: container root inside the realm (ADR-008, FR-042)."""
LOCALE: Final = MappingProxyType({"LC_ALL": "C.UTF-8"})
"""The environment of every package-manager call whose output ginn parses (ADR-011, Locale)."""
MAX_OUTPUT_CHARS: Final = 1_048_576
"""Most characters of package-manager output that ginn reads. More is refused, not scanned."""
MAX_LINE_CHARS: Final = 4_096
"""Longest line of package-manager output that ginn reads. A longer line is refused."""
MAX_NAMES: Final = 5_000
"""Most package names one list or plan may hold. A longer one is refused (FR-042 AC-1)."""
MAX_SHOWN: Final = 20
"""Most names one message shows. The rest are counted."""
MAX_NAME_CHARS: Final = 64
"""Longest name one message shows, after escaping. A longer name is cut and marked."""
_DECISIVE_LIMIT: Final = 300
_NO_OUTPUT: Final = "no output"
_LIMIT_REASONS: Final = ("timed out", "printed more than")
_OUTPUT_TAIL: Final = 2000
"""The most characters of a failed step's output kept in its error details (the last ones)."""
"""The adapter's reasons for a bounded exec that was stopped (podman/cli.py `_run_bounded`)."""
_APT_REMOVAL: Final = re.compile(r"Remv\s+([^\s:\[]+)")
"""An apt simulate line for one removal: `Remv <name> [<version>]` (apt-get -s remove)."""
_APT_HEADING: Final = re.compile(r"will be removed", re.IGNORECASE)
"""The apt heading of a removal list: `The following packages will be REMOVED:`."""
_REMOVAL_WORD: Final = re.compile(r"\b(?:removed|removing)\b", re.IGNORECASE)
"""The dnf headings of a removal list: `Removing:` and `Removing dependent packages:`."""


def printable(text: str, limit: int = MAX_NAME_CHARS) -> str:
    """`text` with each non-printable character written as `repr` writes it, cut to `limit`.

    A name or a line of output can hold a control character from the realm. It is written escaped,
    so it cannot move the terminal. A cut name ends in `...`.
    """
    escaped = repr(text)[1:-1]
    return escaped if len(escaped) <= limit else f"{escaped[:limit]}..."


def shown(names: Sequence[str]) -> str:
    """The first `MAX_SHOWN` names, each through `printable`, then a count of the rest."""
    head = ", ".join(printable(name) for name in names[:MAX_SHOWN])
    rest = len(names) - MAX_SHOWN
    return f"{head} and {rest} more" if rest > 0 else head


def decisive_line(result: ExecResult) -> str:
    """The first non-blank line of stderr, else of stdout, escaped and cut to a short length."""
    for text in (result.stderr, result.stdout):
        for line in text.splitlines():
            if line.strip():
                return printable(line.strip()[:_DECISIVE_LIMIT], _DECISIVE_LIMIT)
    return _NO_OUTPUT


def failure(definition: DistroDef, result: ExecResult, *, realm: str, step: str) -> GinnError:
    """The error for a package-manager call that exited non-zero and is not transient (FR-040).

    A signature match is GINN-E-PKG-SIGNATURE (FR-012 AC-1). Anything else is GINN-E-PKG-MANAGER.
    The error keeps the step, the exit status and the decisive line in its details.
    """
    text = f"{result.stderr}\n{result.stdout}"
    if definition.signature_error.search(text) is not None:
        err = make("GINN-E-PKG-SIGNATURE", realm=realm)
    else:
        err = make("GINN-E-PKG-MANAGER", realm=realm)
    if definition.stale_metadata.search(text) is not None:
        err.details["stale_metadata"] = True
    err.details.update({"step": step, "exit": result.exit_code, "detail": decisive_line(result)})
    err.details["output"] = text[-_OUTPUT_TAIL:]
    return err


def network_failure(definition: DistroDef, result: ExecResult) -> bool:
    """True when a non-zero status is a transient network failure (FR-120, ADR-011).

    A signature match wins: its output is not retried. Stale metadata is not checked here, because
    a stale 404 names no network marker, and a network marker in a stale line is still a network
    failure that a retry can fix.
    """
    if result.exit_code == 0:
        return False
    text = f"{result.stderr}\n{result.stdout}"
    if definition.signature_error.search(text) is not None:
        return False
    return definition.network_error.search(text) is not None


def _network(realm: str, step: str, result: ExecResult) -> GinnError:
    """GINN-E-NETWORK for a transient failure, with the step and the decisive line."""
    err = make("GINN-E-NETWORK")
    err.details.update({"realm": realm, "step": step, "exit": result.exit_code})
    err.details["detail"] = decisive_line(result)
    return err


def run_step(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    argv: Sequence[str],
    *,
    realm: str,
    step: str,
    retry_hook: RetryHook | None,
    user: str = ROOT_USER,
    env: Mapping[str, str] = LOCALE,
    workdir: str | None = None,
    timeout_s: float | None = None,
    max_output: int | None = None,
    stdout_tail: bool = False,
) -> ExecResult:
    """Run one package-manager argv, and return its result. Container root by default.

    `user`, `env` and `workdir` are for the AUR build (packages/aur.py), which runs as the realm
    user in its build directory (FR-043). `timeout_s` and `max_output` bound the step (the adapter
    kills the client at either): a step that hits one is GINN-E-PKG-MANAGER, with the limit as its
    detail. A transient network failure is raised as GINN-E-NETWORK inside the attempt, so the retry
    hook sees it and waits the FR-120 delay (AS-023). Any other non-zero status raises the error of
    `failure`. Only a zero status returns.
    """

    def attempt() -> ExecResult:
        try:
            result = podman.exec(
                container_id,
                argv,
                user=user,
                env=env,
                workdir=workdir,
                timeout_s=timeout_s,
                max_output=max_output,
                stdout_tail=stdout_tail,
            )
        except GinnError as exc:
            reason = exc.details.get("reason")
            if exc.code == "GINN-E-INTERNAL" and isinstance(reason, str):
                if reason.startswith(_LIMIT_REASONS):
                    detail = f"the step stopped at its limit: {reason}"
                    raise refused(realm, step, detail) from exc
            raise
        if network_failure(definition, result):
            raise _network(realm, step, result)
        return result

    result = with_network_retry(attempt, retry_hook)
    if result.exit_code != 0:
        raise failure(definition, result, realm=realm, step=step)
    return result


def refused(realm: str, step: str, detail: str) -> GinnError:
    """GINN-E-PKG-MANAGER for output that is not read, with the step and the reason."""
    err = make("GINN-E-PKG-MANAGER", realm=realm)
    err.details.update({"step": step, "detail": detail})
    return err


def check_output(text: str, *, realm: str, step: str, max_chars: int = MAX_OUTPUT_CHARS) -> None:
    """Refuse output over the caps of this module, before anything is read from it (fail closed).

    `max_chars` raises the total cap for a listing that is parsed on purpose (the AUR closure)."""
    _bounded(text, realm=realm, step=step, max_chars=max_chars)


def _bounded(text: str, *, realm: str, step: str, max_chars: int = MAX_OUTPUT_CHARS) -> None:
    """Refuse output over the total or the line cap, before any line is scanned."""
    if len(text) > max_chars:
        detail = f"the output is over {max_chars} characters, so it is not read"
        raise refused(realm, step, detail)
    longest = max((len(line) for line in text.splitlines()), default=0)
    if longest > MAX_LINE_CHARS:
        detail = f"a line of {longest} characters is over {MAX_LINE_CHARS}, so it is not read"
        raise refused(realm, step, detail)


def parse_names(text: str, *, realm: str, step: str = "list-explicit") -> frozenset[str]:
    """The package names of a list verb: one name per line or per word, blanks ignored.

    Refuses output over the caps (`_bounded`), and a list of more than `MAX_NAMES` names.
    """
    _bounded(text, realm=realm, step=step)
    names = frozenset(text.split())
    if len(names) > MAX_NAMES:
        raise refused(realm, step, f"more than {MAX_NAMES} package names, so it is not read")
    return names


def explicit_packages(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    *,
    realm: str,
    retry_hook: RetryHook | None = None,
) -> frozenset[str]:
    """The explicitly installed package names, from the `list-explicit` verb (DR-007).

    A failure raises the error of `failure`, so a caller that diffs two lists sees the failure and
    not an empty list.
    """
    result = run_step(
        podman,
        container_id,
        definition,
        definition.argv(Verb.LIST_EXPLICIT),
        realm=realm,
        step="list-explicit",
        retry_hook=retry_hook,
    )
    return parse_names(result.stdout, realm=realm)


def _opens_list(line: str) -> str | None:
    """`apt` or `dnf` when `line` opens a removal list, else None.

    A heading starts in column 0 and ends with a colon. Both tests are linear in the length of the
    line, and `_bounded` has already capped that length.
    """
    if not line or line[0].isspace() or not line.rstrip().endswith(":"):
        return None
    if _APT_HEADING.search(line) is not None:
        return "apt"
    if _REMOVAL_WORD.search(line) is not None:
        return "dnf"
    return None


def parse_removals(text: str, *, realm: str, step: str = "remove-check") -> frozenset[str]:
    """The package names that a removal dry run lists (ADR-011 pre-check).

    Three shapes are read, and a line of any other shape is not a removal:
    - an apt simulate line `Remv <name> [<version>]`, which names one package;
    - a row under an apt heading (`The following packages will be REMOVED:`). Apt prints several
      names on one row, so every word of the row is a name;
    - a row under a dnf heading (`Removing:`, `Removing dependent packages:`). The first word of the
      row is the name. A row is indented, and a line that is not indented, or a blank line, ends
      the list.

    Fail closed: an apt heading with no `Remv` line means the output is not in a shape ginn reads,
    so it is refused. The shapes are not verified on a host until M4.
    """
    _bounded(text, realm=realm, step=step)
    names: set[str] = set()
    opened: str | None = None
    apt_heading = False
    apt_remv = False
    for line in text.splitlines():
        removal = _APT_REMOVAL.match(line)
        if removal is not None:
            names.add(removal.group(1))
            apt_remv = True
            opened = None
            continue
        kind = _opens_list(line)
        if kind is not None:
            apt_heading = apt_heading or kind == "apt"
            opened = kind
            continue
        if opened is not None and line[:1].isspace() and line.strip():
            words = line.split()
            names.update(words if opened == "apt" else words[:1])
            continue
        opened = None
    if apt_heading and not apt_remv:
        raise refused(realm, step, "an apt heading lists packages, but no Remv line is printed")
    if len(names) > MAX_NAMES:
        raise refused(realm, step, f"more than {MAX_NAMES} package names, so the plan is not read")
    return frozenset(names)


def removal_dependents(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    names: Sequence[str],
    *,
    realm: str,
    retry_hook: RetryHook | None = None,
) -> frozenset[str]:
    """The packages that removing `names` would also remove, from the `remove-check` dry run.

    The dry run exits non-zero when it changes nothing (dnf exits 1 after the `--assumeno` answer),
    so its status is not read here. Its output is. When a requested name is not in the parsed plan,
    the plan is not read as one, and the call raises GINN-E-PKG-MANAGER with the names it missed.
    """
    result = with_network_retry(
        lambda: podman.exec(
            container_id,
            definition.argv(Verb.REMOVE_CHECK, names),
            user=ROOT_USER,
            env=LOCALE,
        ),
        retry_hook,
    )
    listed = parse_removals(f"{result.stdout}\n{result.stderr}", realm=realm)
    missed = sorted(set(names) - listed)
    if missed:
        err = refused(realm, "remove-check", f"the removal dry run does not list {shown(missed)}")
        err.details["exit"] = result.exit_code
        raise err
    return frozenset(listed - set(names))


__all__ = [
    "LOCALE",
    "MAX_NAMES",
    "ROOT_USER",
    "explicit_packages",
    "failure",
    "network_failure",
    "parse_names",
    "check_output",
    "parse_removals",
    "printable",
    "refused",
    "removal_dependents",
    "run_step",
    "shown",
]
