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
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ginn.cli.output import Output
from ginn.errors import GinnError, warn
from ginn.integrity.fingerprint import HOST_ROOT
from ginn.ops._mutating import OpEnv
from ginn.ops.create import CreateHooks
from ginn.ops.delete import DeleteHooks
from ginn.ops.lifecycle import HostUser, LifecycleHooks
from ginn.ops.packages import PackageHooks
from ginn.ops.recovery import (
    OUTCOME_BLOCKED,
    OUTCOME_BROKEN,
    OUTCOME_CORRUPT,
    OUTCOME_RECOVERED,
    RecoveryOutcome,
    recover,
)
from ginn.podman.adapter import Podman
from ginn.podman.cli import PodmanCli
from ginn.runtime.attach import MOUNTINFO
from ginn.runtime.enter import EnterHooks
from ginn.runtime.mounts import Exists
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


def create_hooks() -> CreateHooks:
    s = seams()
    return CreateHooks(
        helper=s.helper, integrate=s.integrate, exists=s.exists, mountinfo=s.mountinfo
    )


def lifecycle_hooks() -> LifecycleHooks:
    s = seams()
    return LifecycleHooks(helper=s.helper, exists=s.exists, mountinfo=s.mountinfo)


def enter_hooks() -> EnterHooks:
    return EnterHooks(lifecycle=lifecycle_hooks())


def delete_hooks() -> DeleteHooks:
    return DeleteHooks()


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
    paths = owned_paths()
    return Realms(
        paths=paths,
        env=op_env(paths),
        podman=podman(paths, output),
        host=host_user(),
        gid=os.getgid(),
    )


def settle_interrupted(realms: Realms, output: Output, *, read_only: bool = False) -> None:
    """Run ops/recovery.py before a realm command, and report each outcome (FR-122).

    `read_only` is True for `status` and `list`. A failure of the recovery, a GinnError or any
    other exception (an OSError from the journal scan included), then becomes a warning, and the
    command goes on, so the user still sees the realms. Otherwise the exception propagates, and a
    command that changes a realm does not run.
    """
    try:
        outcomes = recover(realms.env, realms.podman)
    except Exception as exc:
        if not read_only:
            raise
        detail = f"{exc.code}: {exc.message}" if isinstance(exc, GinnError) else _describe(exc)
        output.warn(warn("GINN-W-RECOVERY-UNSETTLED", detail=detail))
        return
    for outcome in outcomes:
        _report(outcome, output)


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _report(outcome: RecoveryOutcome, output: Output) -> None:
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
