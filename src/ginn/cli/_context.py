# SPDX-License-Identifier: Apache-2.0
"""The production objects of the realm commands, and recovery before them (IR-010, FR-122).

Each realm command takes its stores, adapter, host user and hooks from this module, so the CLI
builds the same objects as the tests and the host gate. Every builder is a module-level function
that the commands look up at call time, so a test replaces one with monkeypatch. The host seams
(the host tree, the helper path, the path probe, the mount table and the create integration
switch) are one `Seams` value that `seams()` returns. One replacement reaches every hook.

Recovery. IR-010 defines no `ginn recover`, so `settle_interrupted` runs ops/recovery.py before
each command that touches realms (FR-122). A command that changes a realm stops when recovery
raises, because that realm may be half changed. `status` and `list` go on after such a raise,
and a warning names it, so the user still sees the realm (FR-122 AC-1). The outcomes are reported
as follows:
- recovered and broken settle an interrupted operation: GINN-W-RECOVERED.
- blocked and corrupt cannot be settled yet, and their journal file stays:
  GINN-W-RECOVERY-UNSETTLED.
- live, busy and finished are normal, so they are verbose lines only.

Tightened directories (FR-025) are verbose lines too, through `finish`.
"""

from __future__ import annotations

import os
import pwd
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from ginn.cli.output import Output
from ginn.errors import GinnError, make, warn
from ginn.integrity.fingerprint import HOST_ROOT
from ginn.ops._mutating import OpEnv
from ginn.ops.create import CreateHooks
from ginn.ops.delete import DeleteHooks
from ginn.ops.lifecycle import HostUser, LifecycleHooks
from ginn.ops.packages import PackageHooks
from ginn.ops.rebuild import RebuildHooks
from ginn.ops.recovery import (
    OUTCOME_BLOCKED,
    OUTCOME_BROKEN,
    OUTCOME_CORRUPT,
    OUTCOME_RECOVERED,
    RecoveryOutcome,
    recover,
    sweep_estimates,
    sweep_scratch,
)
from ginn.ops.service import ServiceHooks
from ginn.podman.adapter import Podman
from ginn.podman.cli import PodmanCli
from ginn.runtime.attach import MOUNTINFO
from ginn.runtime.enter import EnterHooks
from ginn.runtime.mounts import Exists
from ginn.services.runit_user import WAIT_HELPER
from ginn.store.paths import OwnedPaths

_SETTLED: Final = frozenset({OUTCOME_RECOVERED, OUTCOME_BROKEN})
_UNSETTLED: Final = frozenset({OUTCOME_BLOCKED, OUTCOME_CORRUPT})


@dataclass(frozen=True)
class Seams:
    """The host seams of one invocation. The defaults are the real ones.

    `root_dir` is the tree the integrity check reads (AS-020). `helper` is the ginn-rebind path,
    where None means the installed helper. `integrate` False skips the refresh and the Integration
    Package Set of a create; only the host gate sets it (CreateHooks). `exists` and `mountinfo`
    are the probes that runtime/ reads.
    """

    root_dir: Path = HOST_ROOT
    helper: str | None = None
    wait_helper: str | None = None
    """The ginn-wait path, where None means the installed helper (FR-081)."""
    integrate: bool = True
    exists: Exists = os.path.exists
    mountinfo: Path = MOUNTINFO


def seams() -> Seams:
    """The host seams. Tests and the host gate replace this function."""
    return Seams()


def owned_paths() -> OwnedPaths:
    """The Owned Paths of the running user, from the environment (DR-010).

    Raises GINN-E-HOST-NOT-SET-UP or GINN-E-RUNTIME-DIR-UNSAFE when the host is not set up.
    """
    return OwnedPaths.from_env()


def _outside_bin(path: str, paths: OwnedPaths | None) -> bool:
    """False for a path that resolves into the ginn bin directory, the launchers' own folder."""
    if paths is None:
        return True
    return not os.path.realpath(path).startswith(os.path.realpath(paths.bin) + os.sep)


def _on_path(name: str, paths: OwnedPaths | None) -> str | None:
    """The executable `name` that PATH holds in an absolute directory, with links resolved.

    A copy inside the ginn bin directory is skipped, so a launcher never runs another launcher.
    Relative PATH entries are skipped: they depend on the working directory. None when there is no
    such executable.
    """
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(entry, name)
        if not os.path.isabs(entry) or not os.path.isfile(candidate):
            continue
        if not os.access(candidate, os.X_OK) or not _outside_bin(candidate, paths):
            continue
        return os.path.realpath(candidate)
    return None


def ginn_program(paths: OwnedPaths | None = None) -> str:
    """The absolute path of the ginn command that an exported launcher runs (FR-046, FR-047).

    The `ginn` that PATH holds outside the bin directory (see `_on_path`). With none, `sys.argv[0]`
    when it is absolute and outside the bin directory. The bare name `ginn` is the last resort.
    """
    found = _on_path("ginn", paths)
    if found is not None:
        return found
    if os.path.isabs(sys.argv[0]) and _outside_bin(sys.argv[0], paths):
        return os.path.realpath(sys.argv[0])
    return "ginn"


def podman_program(paths: OwnedPaths | None = None) -> str:
    """The absolute path of the podman that every runtime call runs (ADR-002, FR-011).

    PodmanCli starts `podman` with the PATH it is given, so a bare name would let a file earlier on
    PATH (a launcher in the bin directory, say) stand in for podman. The first `podman` outside the
    bin directory is used. With none, the bare name, which then fails at the call as it always did.
    """
    return _on_path("podman", paths) or "podman"


def account_name() -> str:
    """The account name of the running user, or the uid when it has no passwd entry."""
    uid = os.getuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def host_user() -> HostUser:
    """The host user: uid, home, and the environment that the session allowlist reads (FR-048)."""
    return HostUser(uid=os.getuid(), host_home=Path.home(), host_env=os.environ)


def podman(paths: OwnedPaths, output: Output) -> PodmanCli:
    """The podman adapter. Every runtime command goes to stderr in verbose mode (NFR-042)."""
    return PodmanCli.from_paths(paths, binary=podman_program(paths), verbose=output.verbose_line)


def op_env(paths: OwnedPaths) -> OpEnv:
    """The stores of a mutating operation, for the running user (DR-032)."""
    return OpEnv.for_paths(paths, account_name(), root_dir=seams().root_dir)


_output: Output | None = None
"""The Output of the command that runs now. `realms()` sets it, because the commands call
`realms(ctx.output)` before they ask for a hook. The hook builders fall back to it."""
_paths: OwnedPaths | None = None
"""The Owned Paths of the command that runs now. `realms()` sets it, as it sets `_output`, so
`lifecycle_hooks()` finds the ginn command of a rebuild without a `paths` argument."""


class _StepReport:
    """The progress lines of one operation: one per finished journal step (FR-107).

    A step's detail, such as the figures of the `space-checked` step (FR-121), reaches `detail`
    before the step is reported. The line of that step then carries the detail's keys.
    """

    def __init__(self, output: Output, operation: str) -> None:
        self._output = output
        self._operation = operation
        self._details: dict[str, Mapping[str, object]] = {}

    def detail(self, step: str, detail: Mapping[str, object]) -> None:
        self._details[step] = dict(detail)

    def __call__(self, step: str) -> None:
        detail = self._details.pop(step, None)
        self._output.progress(step, f"{self._operation}: step {step} done", detail)


def _step_reporter(output: Output | None, operation: str) -> _StepReport | None:
    """The on_step hook of an operation: one progress line per finished journal step (FR-107)."""
    if output is None:
        return None
    return _StepReport(output, operation)


def create_hooks(
    paths: OwnedPaths | None = None,
    output: Output | None = None,
    *,
    operation: str = "create",
) -> CreateHooks:
    """The create seams. `paths` finds the ginn command that a services-on realm's scripts run.

    `output` receives the progress line of each step. None means the Output of `realms()`.
    `operation` names the operation in each line: an estimate passes "estimate" (FR-107).
    """
    s = seams()
    report = _step_reporter(output or _output, operation)
    return CreateHooks(
        helper=s.helper,
        integrate=s.integrate,
        exists=s.exists,
        mountinfo=s.mountinfo,
        program=ginn_program(paths),
        on_step=report,
        on_step_detail=None if report is None else report.detail,
    )


def _base_lifecycle(s: Seams) -> LifecycleHooks:
    """The lifecycle seams of the host seams, without a rebuild. A rebuild gets the same ones."""
    return LifecycleHooks(helper=s.helper, exists=s.exists, mountinfo=s.mountinfo)


def rebuild_hooks(paths: OwnedPaths | None = None, output: Output | None = None) -> RebuildHooks:
    """The rebuild seams (FR-021, FR-022, ADR-003).

    A services-on rebuild writes the ginn command into the runit scripts, so the rebuild needs the
    same lookup that create uses (`ginn_program`). `paths` is that lookup's, and None takes the
    paths that `realms()` was given. Its stop and start of a running realm, and its runit start of
    a services-on gN+1, use the host's helper and probes, as a start does (`lifecycle`). Each
    journal step gives one progress line (FR-107), to `output` or else the Output of `realms()`.
    """
    s = seams()
    report = _step_reporter(output or _output, "rebuild")
    return RebuildHooks(
        helper=s.helper,
        exists=s.exists,
        mountinfo=s.mountinfo,
        program=ginn_program(_paths if paths is None else paths),
        on_step=report,
        on_step_detail=None if report is None else report.detail,
        lifecycle=_base_lifecycle(s),
    )


def lifecycle_hooks(paths: OwnedPaths | None = None) -> LifecycleHooks:
    """The lifecycle seams, with the seams of a rebuild that a start may run (FR-066, FR-083).

    `paths` is the lookup of the ginn command for the rebuild (`rebuild_hooks`), and None takes the
    paths that `realms()` was given. The rebuild reads the same helper and probes as the start.
    """
    return replace(_base_lifecycle(seams()), rebuild=rebuild_hooks(paths))


def service_hooks() -> ServiceHooks:
    """The supervised start and finish of a services-on realm (FR-081)."""
    s = seams()
    helper = WAIT_HELPER if s.wait_helper is None else s.wait_helper
    return ServiceHooks(lifecycle=lifecycle_hooks(), wait_helper=helper)


def podman_global(podman: Podman) -> list[str]:
    """The podman binary and global flags of the adapter, for ginn-wait's stop (FR-081)."""
    if not isinstance(podman, PodmanCli):
        raise make("GINN-E-INTERNAL")
    return podman.global_argv()


def enter_hooks() -> EnterHooks:
    return EnterHooks(lifecycle=lifecycle_hooks())


def delete_hooks(output: Output | None = None) -> DeleteHooks:
    """The delete seams. `output` receives the progress line of each step (FR-107).

    None means the Output of `realms()`.
    """
    return DeleteHooks(on_step=_step_reporter(output or _output, "delete"))


def package_hooks(paths: OwnedPaths) -> PackageHooks:
    """The hooks of an install or a remove. The menu entries it writes run the absolute ginn
    command, as `ginn _sync` does (FR-044)."""
    return PackageHooks(lifecycle=lifecycle_hooks(), program=ginn_program(paths))


@dataclass(frozen=True)
class Realms:
    """What one realm command works with: the paths, the stores, the adapter and the host user."""

    paths: OwnedPaths
    env: OpEnv
    podman: Podman
    host: HostUser
    gid: int


def realms(output: Output) -> Realms:
    """Build the objects of one realm command from the builders above."""
    global _output, _paths
    _output = output
    paths = owned_paths()
    _paths = paths
    return Realms(
        paths=paths,
        env=op_env(paths),
        podman=podman(paths, output),
        host=host_user(),
        gid=os.getgid(),
    )


def settle_interrupted(
    realms: Realms, output: Output, *, read_only: bool = False, sweep: bool = False
) -> None:
    """Run ops/recovery.py before a realm command, and report each outcome (FR-122).

    `sweep` runs first the sweep of a killed Kali estimate's leftovers (ops/recovery.py
    `sweep_estimates`), and the sweep of the scratch directories of a killed create
    (`sweep_scratch`, F3, F6). Only the commands that touch the Kali image pass it: create and its
    resume, delete, rebuild and update. Every other command skips it, so an app launch or a runit
    restart pays no podman call for it (NFR-004). `read_only` is True for a command that writes no
    state, such as `status`, `list` or an update check, and a read-only command never sweeps. A
    failure of the recovery, a GinnError or any other exception (an OSError from the journal scan
    included), then becomes a warning, and the command goes on, so the user still sees the realms.
    Otherwise
    the exception propagates, and a command that changes a realm does not run.
    """
    try:
        swept: list[RecoveryOutcome] = []
        if sweep and not read_only:
            swept = [*sweep_estimates(realms.env, realms.podman), *sweep_scratch(realms.env)]
        outcomes = [*swept, *recover(realms.env, realms.podman)]
    except Exception as exc:
        if not read_only:
            raise
        detail = f"{exc.code}: {exc.message}" if isinstance(exc, GinnError) else _describe(exc)
        output.warn(warn("GINN-W-RECOVERY-UNSETTLED", detail=detail))
        return
    for outcome in outcomes:
        _report(outcome, output)


def sweep_leftovers(realms: Realms, output: Output) -> None:
    """Run only the sweep of a killed Kali estimate's leftovers, and report it (FR-122, FR-014).

    The Kali estimate calls this after its own work (cli/commands/create.py `_estimate`). The
    estimate reads no realm state, so `recover` does not run, and the host gate of the estimate
    still refuses before any podman call (FR-006). A non-GinnError exception propagates, as in
    `settle_interrupted` for a command that changes state.
    """
    for outcome in sweep_estimates(realms.env, realms.podman):
        _report(outcome, output)


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _report(outcome: RecoveryOutcome, output: Output) -> None:
    if outcome.op in ("estimate", "scratch"):
        # A sweep of a killed Kali estimate's leftovers (ops/recovery.py sweep_estimates), or of the
        # scratch directories of a killed create (sweep_scratch). The leftovers hold no realm, and
        # the next command tries again. So every outcome is a verbose line, a blocked one too: a
        # warning would name a journal file and a realm that do not exist.
        output.verbose_line(f"recovery: {outcome.path}: {outcome.outcome}: {outcome.reason}")
        return
    if outcome.outcome in _SETTLED:
        output.warn(
            warn(
                "GINN-W-RECOVERED",
                op=outcome.op or "operation",
                realm=outcome.realm,
                outcome=outcome.outcome,
                reason=outcome.reason,
            )
        )
    elif outcome.outcome in _UNSETTLED:
        output.warn(warn("GINN-W-RECOVERY-UNSETTLED", detail=f"{outcome.path}: {outcome.reason}"))
    else:
        output.verbose_line(f"recovery: {outcome.path}: {outcome.outcome}: {outcome.reason}")
    for line in outcome.warnings:
        output.verbose_line(f"recovery: {outcome.path}: {line}")


def finish(realms: Realms, output: Output) -> None:
    """Write the directories that the adapter tightened (FR-025) as verbose lines."""
    if isinstance(realms.podman, PodmanCli):
        for line in realms.podman.tightened:
            output.verbose_line(line)
