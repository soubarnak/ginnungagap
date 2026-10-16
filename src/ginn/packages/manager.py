# SPDX-License-Identifier: Apache-2.0
"""Package manager verbs of a realm, run as container root (FR-012, FR-040, FR-041, FR-042, FR-120).

Every verb is the argv that `DistroDef.argv` builds from the distribution data (NFR-060), as one
list. No argv is built from a string, and none goes through a shell (NFR-SEC-005). Each call runs
as container root (ADR-008, FR-042) with `LC_ALL=C.UTF-8` (ADR-011, the Locale rule), so the output
that ginn parses is in English.

A non-zero status is classified in this order (`failure`, `network_failure`):
1. A signature match is GINN-E-PKG-SIGNATURE (FR-012 AC-1). It wins over every other text.
2. A match of the shared classifier (ops/retry.py, with the distribution's `network_error`
   pattern as `extra`) is a transient network failure. `run_step` retries it through the caller's
   hook with the FR-120 delays, and after the last retry it is GINN-E-NETWORK.
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

Check output and the db-check result are read by the rules the distribution data names
(`check_updates_count`, `check_updates_exit_for_updates`, `db_check_ok_exit` and the stdout flag).
No distribution is named in this module. `database_inconsistent` is the update's db check (FR-120
AC-2).
"""

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from ginn.distros import DistroDef, Verb
from ginn.distros.loader import (
    CountRule,
    DbStatus,
    UpdateStatus,
    arch_check_status,
    db_check_status,
)
from ginn.domain import InstallMethod, InstallSource, ManifestEntry
from ginn.errors import GinnError, make
from ginn.ops.retry import is_network
from ginn.packages.manifest import installed, removed
from ginn.podman.adapter import ExecTarget, Podman, RetryHook, with_network_retry
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


RELAY_NAMES_PER_EXEC: Final = 200
"""The most package names one exec on the relay route carries (ADR-014, FR-049).

The relay takes 256 argv items and 64 KiB per request (native/ginn-exec-frame.h). The verb's words
and the exec options take about 20 items, so 200 names fit with room to spare.
"""
RELAY_NAME_BYTES: Final = 40 * 1024
"""The most name bytes one relay exec carries, which keeps the request under 64 KiB with margin."""


def name_batches(names: Sequence[str], target: ExecTarget) -> list[list[str]]:
    """Split the names of one package verb into the calls that the exec route can carry (ADR-014).

    The direct route has no such limit, so it keeps one call whatever the count. A services-on
    target takes at most RELAY_NAMES_PER_EXEC names and RELAY_NAME_BYTES of name bytes per exec, and
    a longer list is split in order. A services-on target is split even when its call runs direct
    inside the supervisor leaf; the split is only more calls, with the same result. An empty list is
    one empty batch, as before.
    """
    listed = list(names)
    if not target.services or not listed:
        return [listed]
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for name in listed:
        cost = len(name.encode("utf-8")) + 1
        full = len(current) == RELAY_NAMES_PER_EXEC or size + cost > RELAY_NAME_BYTES
        if current and full:
            batches.append(current)
            current, size = [], 0
        current.append(name)
        size += cost
    batches.append(current)
    return batches


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
    """True when a non-zero status is a transient network failure (FR-120, ADR-011, T8.1).

    The shared classifier of ops/retry.py decides. The distribution's `network_error` pattern is
    its `extra`, and its signature pattern is excluded: a signature match wins, so its output is
    not retried. Stale metadata is not checked here, because a stale 404 names no network marker,
    and a network marker in a stale line is still a network failure that a retry can fix.
    """
    return is_network(
        f"{result.stderr}\n{result.stdout}",
        exit_code=result.exit_code,
        extra=definition.network_error,
        signature=definition.signature_error,
    )


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
    target: ExecTarget,
    user: str = ROOT_USER,
    env: Mapping[str, str] = LOCALE,
    workdir: str | None = None,
    timeout_s: float | None = None,
    max_output: int | None = None,
    stdout_tail: bool = False,
    on_ack: Callable[[int], None] | None = None,
) -> ExecResult:
    """Run one package-manager argv, and return its result. Container root by default.

    `target` says where the exec runs (ADR-014), and `on_ack` gets the podman pid once it has
    started (FR-122). A retried attempt calls `on_ack` again.

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
                target=target,
                user=user,
                env=env,
                workdir=workdir,
                timeout_s=timeout_s,
                max_output=max_output,
                stdout_tail=stdout_tail,
                on_ack=on_ack,
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


def refresh_for_verb(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    verb: Verb,
    *,
    realm: str,
    retry_hook: RetryHook | None,
    target: ExecTarget,
    on_ack: Callable[[int], None] | None = None,
) -> bool:
    """Run `refresh` before `verb` when the distribution's data lists `verb` in `refresh_before`.

    Return True when the refresh ran, so the caller can journal it. The install (ops/packages.py)
    and the replay of a rebuild (ops/rebuild.py) both call it, so they keep one rule (ADR-011):
    Debian and Kali refresh their lists before an install, and Arch and Fedora do not. A failed
    refresh raises as `run_step` does, so it is never read as a missing package (FR-022).
    """
    if verb not in definition.refresh_before:
        return False
    run_step(
        podman,
        container_id,
        definition,
        definition.argv(Verb.REFRESH),
        realm=realm,
        step="refresh",
        retry_hook=retry_hook,
        target=target,
        on_ack=on_ack,
    )
    return True


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
    target: ExecTarget,
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
        target=target,
    )
    return parse_names(result.stdout, realm=realm)


def explicit_manifest(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    *,
    realm: str,
    services: bool,
    manifest: Sequence[ManifestEntry],
    names: Sequence[str],
    installed_now: bool,
) -> list[ManifestEntry] | None:
    """The Install Manifest that the realm's explicit-package list gives for `names`.

    Read only: nothing is saved, so the caller decides what to write. After an install, each name
    the realm lists as explicit is recorded as `ginn`. After a remove, each name it no longer lists
    is dropped. None when the list cannot be read or is over the caps, and the caller keeps the
    manifest it had. A result equal to `manifest` means nothing changes.
    """
    try:
        explicit = explicit_packages(
            podman,
            container_id,
            definition,
            realm=realm,
            target=ExecTarget(realm=realm, services=services, mutating=False),
        )
    except Exception:  # noqa: BLE001 - the caller's own error is the one that counts
        return None
    if installed_now:
        present = [raw for raw in names if raw in explicit]
        if not present:
            return list(manifest)
        return installed(manifest, present, source=InstallSource.REPO, method=InstallMethod.GINN)
    gone = [raw for raw in names if raw not in explicit]
    if not gone:
        return list(manifest)
    return removed(manifest, gone)


def remove_passes(
    batches: Sequence[Sequence[str]],
    remove: Callable[[Sequence[str]], None],
    *,
    still_present: Callable[[], frozenset[str]] | None,
) -> None:
    """Remove each batch, retrying the refused ones after the pass (ADR-014, T7.10 review).

    A batch that the manager refuses with GINN-E-PKG-MANAGER is kept, and tried again after the
    pass. Passes repeat while a pass removes something, and at most once per batch, so a batch that
    needs a later batch removed first (A needed by B, with B in a later batch) succeeds on the next
    pass. A pass that removes nothing, or the last pass, raises the last refusal.

    When `still_present` is given, it is read once before the removal and again before each batch.
    A name that was present at the start and is gone now was taken by an earlier removal as a
    dependent, so it is not passed again (dnf may exit non-zero for it). A name that was never
    present is passed as it is. A read that fails with a GinnError leaves the removal unfiltered: if
    the first read fails, no batch is filtered, and if a later read fails, its batch is not. Any
    other error stops the removal at once and is raised.
    """
    pending = [list(batch) for batch in batches]
    initial = _read_present(still_present)
    last: GinnError | None = None
    for _ in range(len(pending)):
        refused_now: list[list[str]] = []
        removed_any = False
        for batch in pending:
            names = batch
            if initial is not None and still_present is not None:
                present = _read_present(still_present)
                if present is not None:
                    names = [raw for raw in batch if raw not in initial or raw in present]
            if not names:
                continue
            try:
                remove(names)
            except GinnError as err:
                if err.code != "GINN-E-PKG-MANAGER":
                    raise
                refused_now.append(batch)
                last = err
                continue
            removed_any = True
        if not refused_now:
            return
        if not removed_any:
            raise last if last is not None else make("GINN-E-INTERNAL")
        pending = refused_now
    if last is None:
        raise make("GINN-E-INTERNAL")
    raise last


def _read_present(still_present: Callable[[], frozenset[str]] | None) -> frozenset[str] | None:
    """The names present now, or None when there is no reader or its read fails (remove_passes)."""
    if still_present is None:
        return None
    try:
        return still_present()
    except GinnError:
        return None


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
    target: ExecTarget,
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
            target=target,
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


SCRATCH_PREFIX: Final = "/tmp/ginn-check-"  # noqa: S108 - a path inside the realm, not the host
"""The start of an Arch check's scratch directory in the realm (ADR-011). A uuid4 hex follows it."""


@dataclass(frozen=True)
class CheckResult:
    """What check-updates found in one realm (FR-024). `count` is None when it is not read."""

    status: UpdateStatus
    count: int | None

    @property
    def updates(self) -> bool:
        """True when the realm has at least one package with a newer version."""
        return self.status is UpdateStatus.UPDATES


def _exec(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    argv: Sequence[str],
    *,
    realm: str,
    step: str,
    retry_hook: RetryHook | None,
    target: ExecTarget,
) -> ExecResult:
    """One check step as container root under the locale. A non-zero status is returned, not raised.

    A transient network failure is raised as GINN-E-NETWORK inside the attempt, so the retry hook
    sees it and waits the FR-120 delay, as `run_step` does.
    """

    def attempt() -> ExecResult:
        result = podman.exec(container_id, argv, target=target, user=ROOT_USER, env=LOCALE)
        if network_failure(definition, result):
            raise _network(realm, step, result)
        return result

    return with_network_retry(attempt, retry_hook)


def _listed(definition: DistroDef, stdout: str, *, realm: str, step: str) -> int | None:
    """How many packages a check's output lists, or None when the data does not count them.

    The output is bounded first (`_bounded`). The rule is the data's `check_updates_count` (FR-024):
    `nonblank-lines` counts each non-blank line, which is Arch's one line per upgradable package
    (ADR-011). `lines-matching` counts the lines that `check_updates_count_regex` finds, which is
    how Debian and Kali mark each upgradable package. `unread` gives None, which is Fedora until M4.
    """
    _bounded(stdout, realm=realm, step=step)
    if definition.check_count is CountRule.NONBLANK_LINES:
        return sum(1 for line in stdout.splitlines() if line.strip())
    pattern = definition.check_count_regex
    if definition.check_count is CountRule.LINES_MATCHING and pattern is not None:
        return sum(1 for line in stdout.splitlines() if pattern.search(line))
    return None


def _fatal(definition: DistroDef, stderr: str) -> bool:
    """True when a stderr line of a check matches a fatal-warning pattern of the distribution."""
    return any(
        pattern.search(line) is not None
        for line in stderr.splitlines()
        for pattern in definition.check_fatal_warnings
    )


def _single(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    argv: Sequence[str],
    *,
    realm: str,
    retry_hook: RetryHook | None,
    target: ExecTarget,
) -> CheckResult:
    """A one-step check: Debian, Kali and Fedora. Exit 0 is none, or updates if rows are listed.

    The data may name one more exit status that means updates (`check_exit_for_updates`, dnf5's
    100). Any other non-zero status is a failure, so apt's 100 on an error stays one.
    """
    result = _exec(
        podman,
        container_id,
        definition,
        argv,
        realm=realm,
        step="check",
        retry_hook=retry_hook,
        target=target,
    )
    listed = _listed(definition, result.stdout, realm=realm, step="check")
    if _fatal(definition, result.stderr):
        raise failure(definition, result, realm=realm, step="check")
    updates_exit = definition.check_exit_for_updates
    if updates_exit is not None and result.exit_code == updates_exit:
        return CheckResult(UpdateStatus.UPDATES, listed)
    if result.exit_code != 0:
        raise failure(definition, result, realm=realm, step="check")
    if listed:
        return CheckResult(UpdateStatus.UPDATES, listed)
    return CheckResult(UpdateStatus.NONE, listed)


def _layered(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    steps: Sequence[Sequence[str]],
    *,
    realm: str,
    retry_hook: RetryHook | None,
    target: ExecTarget,
) -> CheckResult:
    """The Arch check of ADR-011: setup steps, then the scratch sync, then the query.

    Each setup step must exit 0. The sync must exit 0, or the query does not run. The query's
    result goes to `arch_check_status`, which fails closed. Every other outcome is an error.
    """
    *setup, sync, query = steps
    for argv in setup:
        result = _exec(
            podman,
            container_id,
            definition,
            argv,
            realm=realm,
            step="check-setup",
            retry_hook=retry_hook,
            target=target,
        )
        if result.exit_code != 0:
            raise failure(definition, result, realm=realm, step="check-setup")
    sync_result = _exec(
        podman,
        container_id,
        definition,
        sync,
        realm=realm,
        step="check-sync",
        retry_hook=retry_hook,
        target=target,
    )
    if sync_result.exit_code != 0:
        raise failure(definition, sync_result, realm=realm, step="check-sync")
    query_result = _exec(
        podman,
        container_id,
        definition,
        query,
        realm=realm,
        step="check-query",
        retry_hook=retry_hook,
        target=target,
    )
    listed = _listed(definition, query_result.stdout, realm=realm, step="check-query")
    status = arch_check_status(
        sync_result.exit_code,
        query_result.exit_code,
        query_result.stdout,
        query_result.stderr,
        definition.check_fatal_warnings,
    )
    if status is UpdateStatus.ERROR:
        raise failure(definition, query_result, realm=realm, step="check-query")
    return CheckResult(status, listed)


def _remove_scratch(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    scratch: str,
    *,
    realm: str,
    retry_hook: RetryHook | None,
    target: ExecTarget,
) -> None:
    """Remove the scratch directory. `rm -rf` does not follow the `local` link (ADR-011)."""
    result = _exec(
        podman,
        container_id,
        definition,
        ["rm", "-rf", "--", scratch],
        realm=realm,
        step="check-cleanup",
        retry_hook=retry_hook,
        target=target,
    )
    if result.exit_code != 0:
        detail = f"the scratch directory {printable(scratch)} is not removed"
        raise refused(realm, "check-cleanup", detail)


def run_check(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    *,
    realm: str,
    retry_hook: RetryHook | None = None,
    tmpdir: str | None = None,
    target: ExecTarget,
) -> CheckResult:
    """Run check-updates in a running realm and return what it found (FR-024, ADR-011).

    The check changes no installed package. Arch checks a scratch copy of the sync database under
    `tmpdir`, which is made here when the caller gives none. The scratch directory is removed after
    the last step, also when a step failed. A removal that fails after a clean check is an error.
    A distribution whose data lists `refresh` before check-updates runs it first (Debian and Kali).
    Any failure raises: the caller gets GINN-E-PKG-MANAGER, GINN-E-PKG-SIGNATURE or GINN-E-NETWORK.
    """
    scratch = tmpdir
    if definition.check_updates_needs_tmpdir and scratch is None:
        scratch = f"{SCRATCH_PREFIX}{uuid.uuid4().hex}"
    steps = definition.check_updates(scratch)
    if Verb.CHECK_UPDATES in definition.refresh_before:
        run_step(
            podman,
            container_id,
            definition,
            definition.argv(Verb.REFRESH),
            realm=realm,
            step="refresh",
            retry_hook=retry_hook,
            target=target,
        )
    if not definition.check_updates_needs_tmpdir:
        return _single(
            podman,
            container_id,
            definition,
            steps[0],
            realm=realm,
            retry_hook=retry_hook,
            target=target,
        )
    if scratch is None:
        raise refused(realm, "check", "the scratch directory is not set")
    try:
        result = _layered(
            podman,
            container_id,
            definition,
            steps,
            realm=realm,
            retry_hook=retry_hook,
            target=target,
        )
    except BaseException:
        try:
            _remove_scratch(
                podman,
                container_id,
                definition,
                scratch,
                realm=realm,
                retry_hook=retry_hook,
                target=target,
            )
        except GinnError:
            pass  # the check's own error is the one raised; the leftover is a scratch copy
        raise
    _remove_scratch(
        podman,
        container_id,
        definition,
        scratch,
        realm=realm,
        retry_hook=retry_hook,
        target=target,
    )
    return result


def database_inconsistent(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    *,
    realm: str,
    target: ExecTarget,
) -> bool:
    """True when the db-check verb reports an inconsistent package database (FR-120 AC-2, ADR-011).

    The verb runs as container root under the locale, and it changes nothing. The data reads its
    result (`db_check_status`). A distribution with no db-check verb is never inconsistent. A
    result that says nothing about the database raises GINN-E-PKG-MANAGER: podman could not run the
    command, or its output is over the caps. The caller then keeps the realm's prior state and notes
    the failure, so an unknown result never marks a realm broken.
    """
    if not definition.has_db_check:
        return False
    result = podman.exec(
        container_id,
        definition.argv(Verb.DB_CHECK),
        target=target,
        user=ROOT_USER,
        env=LOCALE,
    )
    check_output(result.stdout, realm=realm, step="db-check")
    status = db_check_status(
        result.exit_code,
        result.stdout,
        ok_exit=definition.db_check_ok_exit,
        stdout_inconsistent=definition.db_check_stdout_inconsistent,
    )
    if status is DbStatus.UNKNOWN:
        err = refused(
            realm,
            "db-check",
            f"the check did not run (exit {result.exit_code}): {decisive_line(result)}",
        )
        err.details["exit"] = result.exit_code
        raise err
    return status is DbStatus.INCONSISTENT


__all__ = [
    "LOCALE",
    "MAX_NAMES",
    "ROOT_USER",
    "SCRATCH_PREFIX",
    "CheckResult",
    "database_inconsistent",
    "explicit_packages",
    "failure",
    "network_failure",
    "parse_names",
    "check_output",
    "parse_removals",
    "printable",
    "refused",
    "removal_dependents",
    "run_check",
    "run_step",
    "shown",
]
