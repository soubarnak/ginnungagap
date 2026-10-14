# SPDX-License-Identifier: Apache-2.0
"""Start and finish of a services-on realm under its runit service (FR-081, AS-028, NFR-033).

`start_service` is the work of `ginn _service-run <realm>`, which runit runs as the realm's `run`
script (services/runit_user.py). It starts the realm through ops/lifecycle.start_realm, which
attaches the grants and writes `running`, and then returns the ginn-wait argv. The caller execs
that argv, so ginn-wait supervises the conmon of the container. The exec happens after the realm
lock is released, so the start's journal and audit entries are complete.

`finish_service` is the work of `ginn _service-finish <realm> <exit> <signal>`, which runit runs as
the `finish` script. A stop request (exit 0, from ginn-wait's SIGTERM) moves the realm to `stopped`
and leaves the filter. Any other end is a crash. The crash times are counted in the realm's crash
log (services/supervise.py). A crash waits RESTART_DELAY_S, and runsv restarts run. After
CRASH_LIMIT crashes within CRASH_WINDOW_S the realm is given up: its `down` file is written, runsv
is told not to restart it, and a running realm moves to `failed` (DR-020, AS-028).

The crash log, the `down` file and the runsv control write are the supervisor's own files, and the
realm lock is not taken for them. So a busy realm or an unacknowledged host change cannot stop a
give-up. Only the record move and its audit entry go through ops/_mutating.py (DR-032).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Final

from ginn.domain import RealmName, RealmState, can_transition
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops.lifecycle import HostUser, LifecycleHooks, start_realm
from ginn.ops.security_ops import leave_agent, reconcile_proxy
from ginn.podman.adapter import Podman
from ginn.services.runit_user import (
    WAIT_HELPER,
    conmon_pidfile,
    crash_log,
    mark_down,
    service_dir,
)
from ginn.services.supervise import (
    CRASH_LIMIT,
    CRASH_WINDOW_S,
    RESTART_DELAY_S,
    note_crash,
    read_crashes,
    signal_runsv_down,
    write_crashes,
)
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths
from ginn.store.records import RealmStore

WAIT_ARG_SEPARATOR: Final = "--"
"""The separator between the global podman flags and the container in the ginn-wait argv."""


@dataclass(frozen=True)
class ServiceHooks:
    """The seams of the supervised start and finish. The defaults are the real ones.

    `wait_helper` is the ginn-wait path, which the start checks is executable. `sleep` is the
    restart delay of a crash (AS-028), so a test does not wait 5 s.
    """

    lifecycle: LifecycleHooks = field(default_factory=LifecycleHooks)
    wait_helper: str = WAIT_HELPER
    sleep: Callable[[float], None] = time.sleep


class ServiceExit(Enum):
    """What the finish script did with one end of run."""

    STOPPED = "stopped"
    """A stop request: the realm reads stopped, and runsv does not restart it."""
    RESTART = "restart"
    """A crash that is not the fifth in 60 s: runsv restarts run after the delay."""
    GAVE_UP = "gave-up"
    """The fifth crash in 60 s: left stopped, or failed if it was running (AS-028)."""


def start_service(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    global_argv: Sequence[str],
    session: HostUser,
    hooks: ServiceHooks,
    warnings: list[GinnWarning] | None = None,
) -> list[str]:
    """Start services-on realm `name` for its runit service. Return the ginn-wait argv to exec.

    `global_argv` is the podman binary with its global flags (PodmanCli.global_argv), so the stop
    that ginn-wait runs uses the same storage and runtime. The helper and the podman binary must be
    absolute and executable, or the start is refused before podman runs. A start that podman
    already serves returns its argv without removing the pidfile, which holds a live conmon.
    """
    helper = hooks.wait_helper
    if not os.path.isabs(helper) or not os.access(helper, os.X_OK):
        raise _internal("ginn-wait is not an executable at its installed path", helper=helper)
    if not global_argv or not os.path.isabs(global_argv[0]):
        raise _internal("podman is not an absolute path, so ginn-wait cannot stop the realm")
    pidfile = conmon_pidfile(env.paths, name)
    lifecycle = replace(hooks.lifecycle, before_start=lambda: _fresh_pidfile(env.paths, name))
    record = start_realm(
        env,
        podman,
        name,
        session=session,
        hooks=lifecycle,
        warnings=warnings,
        supervised=True,
    )
    if record.container_id is None:
        raise _internal("the started realm names no container", realm=name.value)
    return [helper, str(pidfile), *global_argv, WAIT_ARG_SEPARATOR, record.container_id]


def finish_service(
    env: OpEnv,
    name: RealmName,
    *,
    exit_code: int,
    signal_no: int,
    hooks: ServiceHooks,
    warnings: list[GinnWarning] | None = None,
) -> ServiceExit:
    """Record one end of run of realm `name` and say what runsv does next (AS-028, NFR-033).

    `exit_code` and `signal_no` are the two arguments runsv passes to finish: the exit code of run,
    or -1 when run did not exit normally, and the signal that ended run (runsv(8)). The FR-006
    warnings of a stop are appended to `warnings` when it is given.
    """
    if exit_code == 0:
        _record_stopped(env, name, exit_code, signal_no, warnings)
        crash_log(env.paths, name).unlink(missing_ok=True)
        return ServiceExit.STOPPED
    log = crash_log(env.paths, name)
    stamps, give_up = note_crash(read_crashes(log), env.clock())
    if not give_up:
        _mkdir_owned(log.parent)
        write_crashes(log, stamps)
        hooks.sleep(RESTART_DELAY_S)
        return ServiceExit.RESTART
    # The stop of runsv comes first, so a later failure below cannot leave runsv restarting run.
    signal_runsv_down(service_dir(env.paths, name))
    mark_down(env.paths, name)
    log.unlink(missing_ok=True)
    _record_failed(env, name, exit_code, signal_no)
    return ServiceExit.GAVE_UP


def _fresh_pidfile(paths: OwnedPaths, name: RealmName) -> None:
    """Remove the stale conmon pidfile before podman starts the container (FR-081, ginn-wait).

    ginn-wait reads the pidfile when it starts, and a pid reused after a reboot is not detected
    (native/ginn-wait.c), so the file the last conmon left is removed here.
    """
    path = conmon_pidfile(paths, name)
    try:
        _mkdir_owned(path.parent)
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise _internal(f"cannot clear the conmon pidfile: {exc.strerror or exc}") from exc


def _record_stopped(
    env: OpEnv,
    name: RealmName,
    exit_code: int,
    signal_no: int,
    warnings: list[GinnWarning] | None,
) -> None:
    """A stop request: move the realm to stopped, then leave the filter and the proxy (FR-067)."""
    store = RealmStore(env.paths, env.clock)
    params = {"exit": exit_code, "signal": signal_no}
    with mutating(env, "stop", name, params) as mutation:
        record = store.load(name)
        if record.state is not RealmState.STOPPED and can_transition(
            record.state, RealmState.STOPPED, record.previous_state
        ):
            store.set_state(name, RealmState.STOPPED)
        _release(env, name, mutation)
    if warnings is not None:
        warnings.extend(mutation.warnings)


def _record_failed(env: OpEnv, name: RealmName, exit_code: int, signal_no: int) -> None:
    """A give-up: a running realm moves to failed, as DR-020 allows. The audit entry is written.

    A stopped realm stays stopped, because DR-020 has no stopped-to-failed move. The entry says so.
    """
    store = RealmStore(env.paths, env.clock)
    params = {
        "exit": exit_code,
        "signal": signal_no,
        "outcome": ServiceExit.GAVE_UP.value,
        "crash_limit": CRASH_LIMIT,
        "window_s": CRASH_WINDOW_S,
    }
    with mutating(env, "service-exit", name, params) as mutation:
        record = store.load(name)
        if can_transition(record.state, RealmState.FAILED, record.previous_state):
            store.set_state(name, RealmState.FAILED)
        else:
            mutation.add_params(state_kept=record.state.value)


def _release(env: OpEnv, name: RealmName, mutation: Mutation) -> None:
    """After a stop: leave the filter and stop a proxy with no app left. Failures are warnings."""
    try:
        leave_agent(env, name)
    except (GinnError, OSError) as err:
        mutation.warnings.append(warn("GINN-W-AGENT-FILTER", realm=name.value, detail=_detail(err)))
    try:
        reconcile_proxy(env, name)
    except (GinnError, OSError) as err:
        mutation.warnings.append(
            warn("GINN-W-PROXY-RELEASE", realm=name.value, detail=_detail(err))
        )


def _detail(err: BaseException) -> str:
    return err.message if isinstance(err, GinnError) else str(err)


def _mkdir_owned(directory: Path) -> None:
    directory.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)


def _internal(detail: str, **values: object) -> GinnError:
    err = make("GINN-E-INTERNAL")
    err.details.update({"detail": detail, **values})
    return err
