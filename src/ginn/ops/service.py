# SPDX-License-Identifier: Apache-2.0
"""Start and finish of a services-on realm under its runit service (FR-081, AS-028, NFR-033).

`start_service` is the work of `ginn _service-run <realm>`, which runit runs as the realm's `run`
script (services/runit_user.py). It starts the realm through ops/lifecycle.start_realm, which
attaches the grants and writes `running`, and then returns the ginn-wait argv. The caller execs
that argv, so ginn-wait supervises the conmon of the container. The exec happens after the realm
lock is released, so the start's journal and audit entries are complete.

`finish_service` is the work of `ginn _service-finish <realm> <exit> <signal>`, which runit runs as
the `finish` script. A stop request is an exit 0 (from ginn-wait's SIGTERM), or an exit 1 while
runsv has its `want down` mark, which `sv down` sets (CL-24). Either records the realm stopped and
leaves the filter. Any other end with the mark keeps the realm's state and counts no crash. Any
other end is a crash, and a crash during a start counts the same as a crash of a running realm.
The crash times are counted in the realm's crash log (services/supervise.py). A crash waits
RESTART_DELAY_S, and runsv restarts run. After CRASH_LIMIT crashes within CRASH_WINDOW_S the realm
is given up: its `down` file is written, runsv is told not to restart it, and a stopped or running
realm moves to `failed` (DR-020, CL-24, AS-028).

The crash log, the `down` file and the runsv control write are the supervisor's own files, and the
realm lock is not taken for them. So a busy realm or an unacknowledged host change cannot stop a
give-up. Only the record move and its audit entry go through ops/_mutating.py (DR-032).
"""

from __future__ import annotations

import os
import sys
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
from ginn.ops.lifecycle import HostUser, LifecycleHooks, pending_rebuild_notice, start_realm
from ginn.ops.recovery import OP_REBUILD
from ginn.ops.security_ops import leave_agent, reconcile_proxy
from ginn.podman.adapter import Podman
from ginn.podman.models import ContainerState
from ginn.services import autostart
from ginn.services.exec_relay import relay_failure, sync_exec_service
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
from ginn.services.supervisor import SupervisorHooks
from ginn.services.supervisor import status as runit_status
from ginn.store._time import utc_from_epoch
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths
from ginn.store.records import RealmRecord, RealmStore

WAIT_ARG_SEPARATOR: Final = "--"
"""The separator between the global podman flags and the container in the ginn-wait argv."""
_STOP_HOLDERS: Final = frozenset({"stop", "delete", "rebuild"})
"""The operations that stop a services-on realm under the lock and record its stop themselves. A
rebuild stops its building container this way, and it writes the record itself at the swap."""


@dataclass(frozen=True)
class ServiceHooks:
    """The seams of the supervised start and finish. The defaults are the real ones.

    `wait_helper` is the ginn-wait path, which the start checks is executable. `sleep` is the
    restart delay of a crash (AS-028), so a test does not wait 5 s. `supervisor` is the sv control
    that reads the `want down` mark of a finish (CL-24).
    """

    lifecycle: LifecycleHooks = field(default_factory=LifecycleHooks)
    wait_helper: str = WAIT_HELPER
    sleep: Callable[[float], None] = time.sleep
    supervisor: SupervisorHooks = field(default_factory=SupervisorHooks)


class ServiceExit(Enum):
    """What the finish script did with one end of run."""

    STOPPED = "stopped"
    """A stop request: runsv does not restart run. The realm reads stopped, unless the request
    ended with a code other than 0 or 1, which keeps the realm's state (CL-24)."""
    RESTART = "restart"
    """A crash that is not the fifth in 60 s: runsv restarts run after the delay."""
    GAVE_UP = "gave-up"
    """The fifth crash in 60 s: the realm reads failed, from stopped or running (CL-24, AS-028)."""


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
    record = RealmStore(env.paths, env.clock).load(name)
    if record.state is RealmState.REBUILDING:
        return _start_building(env, podman, record, helper=helper, global_argv=global_argv)
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


def _start_building(
    env: OpEnv,
    podman: Podman,
    record: RealmRecord,
    *,
    helper: str,
    global_argv: Sequence[str],
) -> list[str]:
    """`ginn _service-run` while a services-on rebuild is building its container (ADR-003).

    The rebuild holds the realm lock and the record, so this start takes neither and writes nothing.
    The runit service starts the container that the rebuild names in `building_container_id`, with
    podman, and the rebuild waits for it to run. The ginn-wait argv is the same as a normal start's,
    so the container is supervised, and `sv down` stops it. This is the one path that starts a
    services-on container without the record reading running. It runs only while the record reads
    rebuilding, and only while the rebuild that names the container is alive: its journal entry
    must be a live `rebuild` entry (FR-122). A rebuild killed before recovery therefore starts no
    container when runsv restarts the service, and recovery removes the orphan. A rebuild whose
    container never starts is refused by its wait (ops/rebuild.py). This start is a deliberate
    exception to decision 2 of the start amendment (ADR-003).
    """
    name = record.name
    cid = record.building_container_id
    if not record.settings.services or cid is None:
        raise _internal("the rebuild names no container for its supervised start", realm=name.value)
    journal = env.journal.load(name)
    if journal is None or journal.entry is None or journal.entry.op != OP_REBUILD:
        raise _internal("no rebuild journal entry names the building container", realm=name.value)
    if not journal.is_live():
        raise _internal("the rebuild that builds the container is not running", realm=name.value)
    pidfile = conmon_pidfile(env.paths, name)
    _fresh_pidfile(env.paths, name)
    podman.start(cid)
    info = podman.inspect(cid)
    if info.state is not ContainerState.RUNNING or info.pid < 2:
        raise _internal(
            "the building container is not running after start",
            realm=name.value,
            state=info.state.value,
        )
    return [helper, str(pidfile), *global_argv, WAIT_ARG_SEPARATOR, cid]


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

    The stop mark is checked before the crash is counted. The rule, by the exit code of run and the
    `want down` mark that `sv down` sets (CL-24, ADR-014):

    - Exit 0 is a stop request, with or without the mark: the realm reads stopped.
    - Exit 1 with the mark is a stop request too. It is ginn-wait's own 1 (conmon exited on its
      own), so the container is gone: the realm reads stopped.
    - Any other code with the mark (-1, ginn-wait 2 to 5, or a ginn exit from `_service-run`) is a
      stop request that proves nothing about the container. For example, ginn-wait 4 means
      `podman stop` failed while conmon runs. The realm keeps its state, and GINN-W-STOP-UNSETTLED
      is reported. A `_service-run` refusal that exits 1 has the code of ginn-wait's 1, so it takes
      the stop line and records stopped. That is correct when the refusal comes before podman starts
      the container, since then no container runs.
    - Without the mark, a non-zero end is a crash. A status that cannot be read is no proof of a
      stop, so such an end is a crash too.

    A stop request of either kind counts no crash, clears the crash count and gives no give-up.

    While a rebuild is in progress (its record reads rebuilding), the end of run writes no record:
    the rebuild stops its building container with `sv down` and settles the record at its swap or
    rollback, and recovery does so for an interrupted rebuild. The supervisor's own files are still
    written (the crash log, and the `down` file on a give-up), so runsv does what it is told.
    """
    rebuilding = _rebuilding(env, name)
    if exit_code == 0:
        _finish_stop(env, name, exit_code, signal_no, warnings, rebuilding)
        return ServiceExit.STOPPED
    if _user_stop(env.paths, name, hooks.supervisor):
        if exit_code == 1:
            _finish_stop(env, name, exit_code, signal_no, warnings, rebuilding)
        else:
            _finish_kept_stop(env, name, exit_code, warnings)
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
    if not rebuilding:
        _record_failed(env, name, exit_code, signal_no)
    notice = _sync_relay(env.paths)  # NFR-001: a realm given up is no longer running (ADR-014)
    if notice is not None:
        _finish_notice(notice, warnings)
    return ServiceExit.GAVE_UP


def _finish_stop(
    env: OpEnv,
    name: RealmName,
    exit_code: int,
    signal_no: int,
    warnings: list[GinnWarning] | None,
    rebuilding: bool,
) -> None:
    """A stop request that exited 0: record the realm stopped, clear the crash count (FR-081)."""
    if not rebuilding:
        _record_stopped(env, name, exit_code, signal_no, warnings)
    # A clean stop clears the crash count, so the count is cleared before the relay is synced:
    # a failed sync must not leave stale crashes to count toward a give-up (FR-081).
    crash_log(env.paths, name).unlink(missing_ok=True)
    notice = _sync_relay(env.paths)  # NFR-001: the relay follows a realm that stopped (ADR-014)
    if notice is not None:
        _finish_notice(notice, warnings)


def _finish_kept_stop(
    env: OpEnv, name: RealmName, exit_code: int, warnings: list[GinnWarning] | None
) -> None:
    """A stop request that ended with a code other than 0 or 1 (CL-24): no crash is counted, and the
    record keeps its state, since the container may still run. The stop request still clears the
    crash count. The kept state is a GINN-W-STOP-UNSETTLED warning, sent as the other finish
    notices are (the caller's list, or the runit log)."""
    crash_log(env.paths, name).unlink(missing_ok=True)
    _finish_notice(warn("GINN-W-STOP-UNSETTLED", realm=name.value, exit_code=exit_code), warnings)


def _user_stop(paths: OwnedPaths, name: RealmName, hooks: SupervisorHooks) -> bool:
    """True when runsv marks the realm `want down`, which only `sv down` sets (CL-24). Read only.

    A status that cannot be read, such as an sv that is not installed, is not a stop: the end is
    then counted as a crash, so a give-up is never skipped on a guess (FR-081).
    """
    try:
        return runit_status(paths, name, hooks=hooks).want_down
    except GinnError:
        return False


def _sync_relay(paths: OwnedPaths, podman: Podman | None = None) -> GinnWarning | None:
    """Sync the relay after an operation that has been recorded, and return a failure (ADR-014).

    The outcome is already recorded, so a failed sync does not change it. The failure comes back as
    a GINN-W-RELAY-SYNC warning, and the next sync retries it.
    """
    try:
        sync_exec_service(paths, podman)
    except Exception as exc:  # noqa: BLE001 - the outcome is recorded, so the failure is a warning
        return relay_failure(exc)
    return None


def _finish_notice(notice: GinnWarning, warnings: list[GinnWarning] | None) -> None:
    """Pass a finish's warning to its caller, which prints it; with no list, write it to stderr.

    The CLI passes a list, so the warning is printed as every other warning is. A finish with no
    list has no user, so its stderr is the runit log of the service (FR-081).
    """
    if warnings is not None:
        warnings.append(notice)
    else:
        _runit_log(notice)


def _runit_log(notice: GinnWarning) -> None:
    """Write a warning to stderr as the CLI's text output does. A finish has no user, so its
    stderr is the runit log of the service (FR-081)."""
    sys.stderr.write(f"{notice.code}: {notice.message}\n  fix: {notice.remedy}\n")


def _rebuilding(env: OpEnv, name: RealmName) -> bool:
    """True while the realm's record reads rebuilding (ops/rebuild.py, DR-034). Read only."""
    try:
        return RealmStore(env.paths, env.clock).load(name).state is RealmState.REBUILDING
    except GinnError:
        return False


_SERVICES_STATES: Final = frozenset({RealmState.STOPPED, RealmState.RUNNING})
"""The states that a services change is recorded in (the same as a tier change, FR-066)."""


def set_services_mode(
    env: OpEnv,
    name: RealmName,
    *,
    on: bool,
    warnings: list[GinnWarning] | None = None,
    podman: Podman | None = None,
) -> RealmRecord:
    """`ginn services <realm> on|off`: set the services mode (FR-083, ADR-003). Return the record.

    The mode is fixed in the container, so a change is a create-time change (ADR-006). It is
    recorded in the settings now, under the realm lock, and the next start rebuilds the realm into a
    container of the new mode (ops/rebuild.py, FR-021). The installed packages and the Realm Home
    are kept by that rebuild. A running realm gets `restart_required`, as a tier change does.

    Turning services off while the realm's container is supervised (its runit service exists)
    writes that service's `down` file now. Autostart needs services on (DR-012), so the record
    sets autostart off with it, and runit must not start the realm at boot in between. The flag it
    displaces is kept in the record, and a change cancelled before the rebuild (services back on,
    on the same container) restores the flag and its `down` file (FR-083). A missing service
    directory counts as already down. The runit service directory itself is created or removed by
    the rebuild, after its swap (ADR-003).

    Setting the mode the realm already has is a no-op: the record is returned and nothing is
    written. A realm that is neither stopped nor running is refused with GINN-E-REALM-STATE before
    any write. The change's GINN-W-REBUILD-PENDING notice is appended to `warnings` (CL-19).

    The relay service follows the change at once (ADR-014). `podman` is the adapter whose podman
    command the service renders, and None applies only the running state of an installed service.
    """
    store = RealmStore(env.paths, env.clock)
    current = store.load(name)  # GINN-E-NO-SUCH-REALM before any audit entry
    if current.settings.services == on:
        return current
    with mutating(env, "services", name, {"services": on}) as mutation:
        record = _services_body(env, store, name, on, mutation)
    # After the journal entry is finished: a body error would roll the change back, and a relay
    # failure must not fail a change that is already saved (ADR-014, the _mutating.py contract).
    notice = _sync_relay(env.paths, podman)
    if warnings is not None:
        warnings.extend(mutation.warnings)
        if notice is not None:
            warnings.append(notice)
    return record


def _has_service_dir(paths: OwnedPaths, name: RealmName) -> bool:
    """True when the realm's runit service directory exists (ADR-003). Read only."""
    return service_dir(paths, name).is_dir()


def _services_body(
    env: OpEnv, store: RealmStore, name: RealmName, on: bool, mutation: Mutation
) -> RealmRecord:
    record = store.load(name)
    if record.settings.services == on:
        return record
    if record.state not in _SERVICES_STATES:
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state=record.state.value,
            action="services",
            allowed="stopped or running",
        )
    mutation.step("checked")
    displaced: bool | None = None
    autostart_flag = False
    if not on and record.services_at_create:
        # The autostart flag is set off (DR-012), so it is kept here: a change that is cancelled
        # before the rebuild restores it (FR-083). A service directory that is gone is already down.
        displaced = record.settings.autostart
        if _has_service_dir(env.paths, name):
            autostart.apply(env.paths, name, False)  # DR-012: services off, so no autostart
        mutation.step("down-file")
    elif on and record.services_at_create and record.autostart_before_off is not None:
        # The change is cancelled: the container still runs services on, so the flag it displaced
        # comes back, and its `down` file follows it.
        autostart_flag = record.autostart_before_off
        if _has_service_dir(env.paths, name):
            autostart.apply(env.paths, name, autostart_flag)
        mutation.step("down-file")
    settings = replace(record.settings, services=on, autostart=autostart_flag)
    saved = replace(
        record,
        settings=settings,
        updated_at=utc_from_epoch(env.clock()),
        restart_required=record.restart_required or record.state is RealmState.RUNNING,
        autostart_before_off=displaced,
    )
    store.save(saved)
    mutation.step("saved")
    notice = pending_rebuild_notice(saved, "the services mode changed")
    if notice is not None:
        mutation.warnings.append(notice)
    return saved


_AUTOSTART_REFUSED: Final = frozenset({RealmState.CREATING, RealmState.DELETING, RealmState.BROKEN})
"""The states whose record is not settled enough to change autostart (DR-020)."""


def _refuse_autostart_state(record: RealmRecord) -> None:
    """Refuse autostart of a realm that is being created, deleted or is broken (DR-020)."""
    if record.state in _AUTOSTART_REFUSED:
        raise make(
            "GINN-E-REALM-STATE",
            realm=record.name.value,
            state=record.state.value,
            action="autostart",
            allowed="stopped, running or failed",
        )


def set_autostart(env: OpEnv, name: RealmName, *, on: bool) -> RealmRecord:
    """`ginn autostart <realm> on|off`: set autostart of a services-on realm (FR-084, DR-012).

    The rule is the settings rule (DR-012, domain/realm.py): autostart needs services on, so a
    services-off realm is refused before any write. A realm being created, deleted or broken is
    refused with GINN-E-REALM-STATE before any write too, and a missing service directory is
    GINN-E-SERVICE-MISSING. Under the realm lock, the `down` file of the runit service is made to
    match the flag first (services/autostart.py), then the record is saved, so a failure to write
    the
    file saves nothing. The next supervised start makes the file match again, so a give-up that
    wrote
    `down` does not keep an autostart realm from starting.
    """
    store = RealmStore(env.paths, env.clock)
    current = store.load(name)
    _refuse_autostart_state(current)  # before any write
    replace(current.settings, autostart=True)  # DR-012 for both directions, before any write
    with mutating(env, "autostart", name, {"autostart": on}) as mutation:
        record = store.load(name)
        _refuse_autostart_state(record)
        settings = replace(record.settings, autostart=on)
        if record.services_at_create:
            # A realm whose services change is pending has no service directory yet. The rebuild
            # that applies the change writes the `down` file from the record's autostart flag.
            autostart.apply(env.paths, name, on)
        mutation.step("down-file")
        saved = replace(record, settings=settings, updated_at=utc_from_epoch(env.clock()))
        store.save(saved)
        mutation.step("saved")
    return saved


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
    """A stop request: move the realm to stopped, then leave the filter and the proxy (FR-067).

    When a `ginn stop` or a `ginn delete` holds the realm lock, it runs `sv down` under that lock
    and records the stop itself, after this script has returned. So this script writes nothing and
    returns, and the caller still clears the crash log. Only those two holders are passed over. Any
    other holder is refused as before (GINN-E-REALM-BUSY).
    """
    store = RealmStore(env.paths, env.clock)
    params = {"exit": exit_code, "signal": signal_no}
    try:
        with mutating(env, "stop", name, params) as mutation:
            record = store.load(name)
            if record.state is not RealmState.STOPPED and can_transition(
                record.state, RealmState.STOPPED, record.previous_state
            ):
                store.set_state(name, RealmState.STOPPED)
            _release(env, name, mutation)
    except GinnError as err:
        if err.code == "GINN-E-REALM-BUSY" and err.details.get("op") in _STOP_HOLDERS:
            return
        raise
    if warnings is not None:
        warnings.extend(mutation.warnings)


def _record_failed(env: OpEnv, name: RealmName, exit_code: int, signal_no: int) -> None:
    """A give-up: a stopped or running realm moves to failed (CL-24, DR-020). The audit entry is
    written, and it names the move.

    The move is legal only as a give-up, so this is the one caller that passes `give_up`. A realm
    in any other state keeps it, and the entry says so.
    """
    store = RealmStore(env.paths, env.clock)
    params = {
        "exit": exit_code,
        "signal": signal_no,
        "outcome": ServiceExit.GAVE_UP.value,
        "crash_limit": CRASH_LIMIT,
        "window_s": CRASH_WINDOW_S,
    }
    try:
        with mutating(env, "service-exit", name, params) as mutation:
            record = store.load(name)
            if can_transition(record.state, RealmState.FAILED, record.previous_state, give_up=True):
                store.set_state(name, RealmState.FAILED, give_up=True)
                mutation.add_params(state=f"failed (was {record.state.value})")
            else:
                mutation.add_params(state_kept=record.state.value)
    except GinnError as err:
        # A rebuild that holds the lock settles the record itself (see finish_service).
        if err.code == "GINN-E-REALM-BUSY" and err.details.get("op") == "rebuild":
            return
        raise


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
