# SPDX-License-Identifier: Apache-2.0
"""Enter a realm, exec a command in it, or run as container root (FR-042, FR-048, FR-049).

`enter_realm` serves `ginn enter` (no command: the distribution's login shell), `ginn exec` (a
command) and `--root` (container root inside the realm only, FR-042). It returns the exit status of
the inner process as podman reports it, because IR-011 says `ginn exec` returns the inner command's
exit code. A command that a signal ends comes back as 128 plus the signal number, which is podman's
convention; cli.py maps the same way a signal that ends podman itself (IR-011 names no other rule).

The steps, in order:

1. The record is read (FR-025: GINN-E-NO-SUCH-REALM). A Kali realm is refused with
   GINN-E-NOT-SUPPORTED-YET, and a state that does not allow enter with GINN-E-REALM-STATE
   (DR-020). Both come before any podman call.
2. The session is read from the host (runtime/env.py). Proxy values stay in podman's environment,
   and the argv carries only their names (ADR-004 amendment).
3. One `podman ps` answers whether the container runs. Its state and its pid come from that one
   listing, with no record pid and no /proc read. ADR-010's amendment of 2026-10-13 (section "T4.6
   enter runs one listing per enter") decides this path, and NFR-004 measures it. A realm that does
   not run is started by `start_realm`, which records the grants and attaches them (FR-017,
   FR-049). A services-off start has its grants attached already, so no re-attach follows it. A
   services-on start is made by its runit service under a sparse environment, so `_settle` attaches
   the session's grants after it (ADR-008 amendment).
4. A running realm is pre-checked (ADR-008 amendment): `needs_reattach` reads the host sources and
   changes nothing. Most enters find nothing changed and go straight to exec. When a grant changed,
   `mutating("reattach")` takes the realm lock (FR-123, DR-033), reads the state again, re-attaches
   the changed grants, and saves the record only when its grant list or its share list changed
   (DR-032). Inside the lock, a check that finds no work writes a `noop` step with its reason.
5. A realm lock held by a re-attach or a start (GINN-E-REALM-BUSY with that op) is waited for, up
   to `BUSY_WAIT_S`, in steps of `BUSY_STEP_S`. Each step repeats the lock-free check of steps 3
   and 4, and the command runs once the realm runs with its grants current. Any other holder, and a
   deadline that passes, raise GINN-E-REALM-BUSY. GINN-E-HOST-UNACKED and GINN-E-ATTACH-FAILED are
   never waited for.
6. `podman exec` runs the command as the realm user (`--user uid:gid`, or `0:0` under `--root`),
   with HOME and the workdir at the host home path (FR-048). Under `--root` both are `/root`
   (`ROOT_HOME`). The session environment is passed, and the proxy names go as `--env NAME`
   (ADR-004 amendment). A status of 125 is podman's own failure or the command's own 125, so enter
   reads the listing once more. If the container no longer runs, GINN-E-REALM-STATE is raised.
   Otherwise the status comes back unchanged. The exec is never retried. Under `--root`, the
   `root_session` hook (EnterHooks) wraps the exec: it receives the record, the container and the
   exec as a callable, and it returns the status of that exec. The hook records the packages that
   a root session installs (FR-042 AC-1). Without the hook, or without `--root`, nothing wraps it.

A failed re-attach raises GINN-E-ATTACH-FAILED for this enter only. The realm keeps running, since
enter never stops a realm (ADR-008 amendment). A services-on realm is stopped only by its `sv`
service (FR-081).

Launched apps. An enter or an exec as the user registers its own process in the realm's proxy
state, so the session bus proxy runs while the app does (FR-068, ADR-007). That registration takes
only the short lock of the proxy state, never the realm lock, so a launch is not refused while an
install, a sync or a permission change holds the realm (ADR-010). The release at the end is a
warning on failure: it never replaces the command's exit status or its error (IR-011). SIGTERM and
SIGHUP reach the command. Before its podman client starts, a signal ends the command through this
exit path. While the client runs, the signal is forwarded to the client, and its wait returns with
the command's status (128 plus the signal when the client died of it). The app is released after
that, so it is still released (FR-068). SIGINT and SIGQUIT keep their behaviour from T4.6.

Budgets. `PRECHECK_BUDGET_S` is the share of NFR-004's 300 ms p95 that the stat pre-check may use.
It is documented, not enforced: enter reports `listing`, `precheck` and `exec` through
`EnterHooks.observe`, and T4.9 measures the p95 over 100 launches with them included.
`BUSY_WAIT_S` bounds the wait for a lock holder (a re-attach or a start), not the proxy
registration.

Decisions and open items (see the report): a user exec's workdir is the host home, not the host
working directory; `--root` uses `/root` for HOME and the workdir, since a root write into the bound
Realm Home would land on a subordinate uid (FR-042); a 125 is returned unchanged, and the CLI maps a
ginn-side failure to EXEC_GINN_FAILURE (IR-011, T4.11); the pid comes from the listing, as ADR-010's
amendment of 2026-10-13 decides.
"""

import os
import signal
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Final

from ginn.distros import load as load_distro
from ginn.domain import RealmName, RealmState
from ginn.domain.permissions import effective_toggles
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops.lifecycle import HostUser, LifecycleHooks, start_realm
from ginn.ops.security_ops import finish_app, start_app
from ginn.podman.adapter import PODMAN_ERROR_EXIT, Podman
from ginn.podman.models import ContainerInfo, ContainerState
from ginn.runtime._enter_plan import exec_home, exec_user, login_argv, share_after
from ginn.runtime.attach import Admit, admission, needs_reattach, reattach
from ginn.runtime.env import SessionEnv, session_env
from ginn.runtime.realm_security import agent_enabled, session_extras, stored_gpg_extra
from ginn.runtime.spec import KALI, LABEL_REALM
from ginn.security._proc import Proc
from ginn.store._time import utc_from_epoch
from ginn.store.records import RealmRecord, RealmStore

PRECHECK_BUDGET_S: Final = 0.050
"""Seconds the stat pre-check may use on a running realm (ADR-008, ADR-010): the session read, one
identity read per grant and the admission checks. The podman listing is not in the budget; enter
reports it as `listing`, so T4.9 can tell the stat work from the podman cost. Documented only."""
BUSY_WAIT_S: Final = 2.0
"""Seconds an enter waits for a re-attach or a start that holds the realm lock (FR-123). A longer
hold is refused, so the user never waits more than this for another operation."""
BUSY_STEP_S: Final = 0.05
"""Seconds between two lock-free checks while an enter waits for a lock holder."""
_WAIT_FOR: Final = frozenset({"reattach", "start"})
"""The operations whose realm lock an enter waits for. Any other holder is refused at once."""
_ENTERABLE: Final = frozenset({RealmState.RUNNING, RealmState.STOPPED, RealmState.FAILED})
"""The record states an enter accepts. Any other state is refused with GINN-E-REALM-STATE."""
_HALTED: Final = frozenset({ContainerState.CREATED, ContainerState.EXITED})
"""The container states that mean the process is not running. Any other state is running."""
_KALI_MILESTONE: Final = "M10"
"""The milestone that adds Kali realms (docs/roadmap.md, T10.1)."""
_STOPPED_SINCE_LOOK: Final = "the realm stopped after the pre-check"
"""The `noop` reason when the realm stopped between the look and the lock."""


def _stdio_is_tty() -> bool:
    """True when stdin and stdout are both terminals, so the exec gets a TTY (FR-048)."""
    return os.isatty(0) and os.isatty(1)


def _signal_pid(pid: int, signum: int) -> None:
    """Send `signum` to process `pid`. A process that has already exited is no error (FR-068)."""
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        return


RootSession = Callable[[RealmRecord, str, Callable[[], int]], int]
"""Wraps the exec of a `--root` session: (record, container id, run) -> the exec's status."""


@dataclass(frozen=True)
class EnterHooks:
    """The seams of enter. The defaults are the real ones.

    `lifecycle` carries the probes and the helper path that runtime/ reads, and it is passed to
    `start_realm`. `isatty` decides whether the exec gets a TTY. `clock` times the steps and bounds
    the wait for a lock holder, and `sleep` separates two checks of that wait. `observe` receives
    `listing`, `precheck` and `exec`, each with the seconds it took (NFR-004, T4.9). `notify`
    receives each FR-006 warning of a re-attach or a start that this enter made, before the command
    runs, so the caller can show it first (the command's own output comes after). `root_session`
    wraps the exec of a `--root` session: it is called with the record, the container id and a
    callable that runs the exec, and it returns that exec's status. It is never called without
    `--root`, so the non-root path costs nothing (NFR-004). `kill` sends a signal to the command's
    client while it runs (FR-068); a test replaces it to see what is forwarded.
    """

    lifecycle: LifecycleHooks = field(default_factory=LifecycleHooks)
    isatty: Callable[[], bool] = _stdio_is_tty
    clock: Callable[[], float] = time.perf_counter
    sleep: Callable[[float], None] = time.sleep
    observe: Callable[[str, float], None] | None = None
    notify: Callable[[GinnWarning], None] | None = None
    root_session: RootSession | None = None
    kill: Callable[[int, int], None] = _signal_pid


@dataclass(frozen=True)
class _Look:
    """One lock-free look at a realm: its record, its session, and what it needs (ADR-008)."""

    record: RealmRecord
    session: SessionEnv
    admit: Admit
    running: bool
    pending: bool
    app: Proc | None = None
    """The process this command registered as a launched app, whose proxy `finish_app` releases."""


def _internal(detail: str) -> GinnError:
    err = make("GINN-E-INTERNAL")
    err.details["detail"] = detail
    return err


def _load(store: RealmStore, name: RealmName) -> RealmRecord:
    """Read a record. A read error other than a missing file is GINN-E-INTERNAL, as lifecycle's
    `_load` maps it (FR-020), so no caller sees a raw OSError."""
    try:
        return store.load(name)
    except OSError as exc:
        err = _internal("cannot read the realm record")
        err.details["reason"] = exc.strerror or str(exc)
        raise err from exc


def _observe(hooks: EnterHooks, name: str, seconds: float) -> None:
    if hooks.observe is not None:
        hooks.observe(name, seconds)


def _listed(podman: Podman, name: RealmName, cid: str | None) -> ContainerInfo | None:
    """The realm's container from one `podman ps`, or None when podman does not list it.

    A podman error propagates, and no caller reads it as "not running", so nothing is written and
    nothing is stopped on it.
    """
    if cid is None:
        return None
    return next((info for info in podman.ps({LABEL_REALM: name.value}) if info.id == cid), None)


def _running(info: ContainerInfo | None) -> bool:
    return info is not None and info.state not in _HALTED


def _waits_for_holder(err: GinnError) -> bool:
    """True for a realm-busy refusal whose holder is a re-attach or a start (FR-123)."""
    return err.code == "GINN-E-REALM-BUSY" and err.details.get("op") in _WAIT_FOR


def _look(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    session: HostUser,
    hooks: EnterHooks,
    root: bool = False,
    register: bool = True,
) -> _Look:
    """Read the record and the host without the realm lock: the hot path of enter (ADR-010).

    Refusals come first, so a refused realm causes no podman call. The session read and the
    admission are the stat pre-check, and the listing is timed apart from them. `register` is False
    for a look that only reads: it registers no app and tries no proxy, because the first look of
    the command has done both, and the app is released once, by that first look's owner.
    """
    record = _load(RealmStore(env.paths, env.clock), name)
    if record.settings.distro.value == KALI:
        raise make(
            "GINN-E-NOT-SUPPORTED-YET",
            realm=name.value,
            feature="a Kali realm",
            milestone=_KALI_MILESTONE,
        )
    if record.state not in _ENTERABLE:
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state=record.state.value,
            action="enter",
            allowed="stopped, failed or running",
        )
    # A command that runs as the user is a launched app: it registers, and starts the session bus
    # proxy when none runs, before the session is read, so the bus socket is there to re-attach
    # (FR-068, ADR-007). A refused realm has returned above, so it starts no proxy.
    app = (
        None
        if root or not register
        else start_app(
            env,
            name,
            host_env=session.host_env,
            uid=session.uid,
            hooks=hooks.lifecycle.security,
            notify=hooks.notify,
        )
    )
    try:
        return _look_session(env, podman, name, record, session=session, hooks=hooks, app=app)
    except BaseException:
        if app is not None:
            _release_app(env, name, app, hooks)
        raise


def _release_app(env: OpEnv, name: RealmName, app: Proc, hooks: EnterHooks) -> None:
    """Release the launched app. A failure is a warning, and never replaces the command's result."""
    try:
        finish_app(env, name, app)
    except (GinnError, OSError) as err:
        if hooks.notify is not None:
            detail = err.message if isinstance(err, GinnError) else str(err)
            hooks.notify(warn("GINN-W-PROXY-RELEASE", realm=name.value, detail=detail))


class _CommandClient:
    """The podman client of the command, from its start until its exit (FR-068).

    `pid` is None before the client starts and after it exits. Then a signal ends the command
    through its exit path, and while the client runs the signal is forwarded to it.
    """

    def __init__(self) -> None:
        self.pid: int | None = None

    def started(self, pid: int) -> None:
        self.pid = pid

    def exited(self) -> None:
        self.pid = None


@contextmanager
def _orderly_signals(kill: Callable[[int, int], None]) -> Iterator[_CommandClient]:
    """SIGTERM and SIGHUP leave the command through its exit path, so its app is released (L).

    Before the command's client starts, a signal raises SystemExit with the usual status for a
    signal (128 plus the number). While the client runs, the signal goes to the client through
    `kill`, and the handler returns. The client's wait then returns normally, so the command's own
    status comes back, and the release runs once the client is gone (FR-068). Nothing raises inside
    that wait, so the client is never killed without being signalled. The previous handlers are
    restored when the command ends. Only the main thread may set a handler, so any other thread
    runs without this.
    """
    client = _CommandClient()
    if threading.current_thread() is not threading.main_thread():
        yield client
        return

    def leave(signum: int, _frame: object) -> None:
        if client.pid is not None:
            kill(client.pid, signum)
            return
        raise SystemExit(128 + signum)

    saved = {sig: signal.getsignal(sig) for sig in _ORDERLY_SIGNALS}
    for sig in _ORDERLY_SIGNALS:
        signal.signal(sig, leave)
    try:
        yield client
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)


_ORDERLY_SIGNALS: Final = (signal.SIGTERM, signal.SIGHUP)


def _look_session(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    record: RealmRecord,
    *,
    session: HostUser,
    hooks: EnterHooks,
    app: Proc | None,
) -> _Look:
    """The session read, the admission and the pre-check of `_look`, after the refusals."""
    owned = env.paths.all_owned_roots()
    read_started = hooks.clock()
    extras = session_extras(
        session.host_env,
        agent_on=agent_enabled(record.settings.tier, record.permissions.toggles),
        uid=session.uid,
        realm=name.value,
        runtime=env.paths.runtime,
        hooks=hooks.lifecycle.security,
        gpu=record.gpu_cdi,
        gpg_extra=stored_gpg_extra(
            hooks.lifecycle.security, known=record.gpg_extra_known, stored=record.gpg_extra
        ),
    )
    realm_session = session_env(
        session.host_env,
        home=session.host_home,
        uid=session.uid,
        owned_paths=owned,
        extras=extras,
        toggles=effective_toggles(record.settings.tier, record.permissions.toggles),
    )
    read_seconds = hooks.clock() - read_started
    listing_started = hooks.clock()
    info = (
        _listed(podman, name, record.container_id) if record.state is RealmState.RUNNING else None
    )
    _observe(hooks, "listing", hooks.clock() - listing_started)
    admit = admission(
        home=session.host_home,
        owned_paths=owned,
        runtime_root=env.paths.runtime,
        env=session.host_env,
    )
    if not _running(info):
        return _Look(record, realm_session, admit, running=False, pending=False, app=app)
    check_started = hooks.clock()
    pending = needs_reattach(
        record.grants,
        realm_session,
        admit=admit,
        mountinfo=hooks.lifecycle.mountinfo,
    )
    _observe(hooks, "precheck", read_seconds + hooks.clock() - check_started)
    return _Look(record, realm_session, admit, running=True, pending=pending, app=app)


def _settle(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    look: _Look,
    *,
    session: HostUser,
    hooks: EnterHooks,
    notices: list[GinnWarning],
) -> RealmRecord:
    """Make the realm run with its grants current, and return its record (ADR-008, FR-017).

    The lock is taken only for a change: a running realm with nothing pending returns at once. A
    realm that stopped after the look is started, which takes the lock itself. The FR-006 warnings
    of a re-attach or a start are appended to `notices`.

    A services-on realm is started through its runit service (FR-081), and that start runs under
    runit's sparse environment, so it attaches no session grant. The grants of this session are
    attached here, lazily, through the same re-attach as a running realm (ADR-008 amendment).
    """
    if look.running and not look.pending:
        return look.record
    if look.running and _reattach_locked(
        env, podman, name, look, session=session, hooks=hooks, notices=notices
    ):
        return look.record
    record = start_realm(
        env, podman, name, session=session, hooks=hooks.lifecycle, warnings=notices
    )
    if not record.services_at_create:
        # The container's mode decides whether this start ran under runit, not the desired mode.
        return record
    after = _look_session(
        env,
        podman,
        name,
        _load(RealmStore(env.paths, env.clock), name),
        session=session,
        hooks=hooks,
        app=look.app,
    )
    if after.running and after.pending:
        _reattach_locked(env, podman, name, after, session=session, hooks=hooks, notices=notices)
    return _load(RealmStore(env.paths, env.clock), name)


def _await_holder(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    busy: GinnError,
    *,
    session: HostUser,
    hooks: EnterHooks,
    notices: list[GinnWarning],
    root: bool,
) -> RealmRecord:
    """Wait a bounded time for the re-attach or the start that holds the realm lock (FR-123).

    Each step sleeps, then repeats the lock-free look. The command runs as soon as the realm runs
    with nothing pending. Otherwise the lock is tried again. A refusal that is not a wait case, and
    the refusal of the deadline, are raised as they come. The looks here do not register an app:
    the command's first look did, once, and `enter_realm` releases that app when the command ends.
    """
    deadline = hooks.clock() + BUSY_WAIT_S
    while True:
        if hooks.clock() >= deadline:
            raise busy
        hooks.sleep(BUSY_STEP_S)
        look = _look(env, podman, name, session=session, hooks=hooks, root=root, register=False)
        if look.running and not look.pending:
            return look.record
        try:
            return _settle(env, podman, name, look, session=session, hooks=hooks, notices=notices)
        except GinnError as again:
            if not _waits_for_holder(again):
                raise
            busy = again


def enter_realm(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    session: HostUser,
    gid: int,
    argv: Sequence[str] | None = None,
    root: bool = False,
    hooks: EnterHooks | None = None,
) -> int:
    """Run `argv` in realm `name`, or the login shell when `argv` is None. Return its exit status.

    `session` is the host user with its environment. `gid` is the host user's group, which the
    realm user keeps under keep-id. `root` runs as container root inside the realm (FR-042). An
    empty `argv` is a usage error (IR-010). See the module docstring for the steps.
    """
    hooks = EnterHooks() if hooks is None else hooks
    if argv is not None and not argv:
        raise make("GINN-E-USAGE", detail="exec needs a command after '--'")
    # The signals are orderly from the look on, so an app registered there is released too.
    with _orderly_signals(hooks.kill) as client:
        look = _look(env, podman, name, session=session, hooks=hooks, root=root)
        try:
            return _enter_look(
                env,
                podman,
                name,
                look,
                session=session,
                gid=gid,
                argv=argv,
                root=root,
                hooks=hooks,
                client=client,
            )
        finally:
            app = look.app
            if app is not None:
                _release_app(env, name, app, hooks)


def _enter_look(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    look: _Look,
    *,
    session: HostUser,
    gid: int,
    argv: Sequence[str] | None,
    root: bool,
    hooks: EnterHooks,
    client: _CommandClient,
) -> int:
    """The enter after the look: settle the realm, then run the command (module docstring)."""
    notices: list[GinnWarning] = []
    try:
        record = _settle(env, podman, name, look, session=session, hooks=hooks, notices=notices)
    except GinnError as err:
        if not _waits_for_holder(err):
            raise
        record = _await_holder(
            env, podman, name, err, session=session, hooks=hooks, notices=notices, root=root
        )
    if hooks.notify is not None:
        for notice in notices:
            hooks.notify(notice)
    cid = record.container_id
    if cid is None:
        raise _internal("the realm has no container to enter")
    if argv is None:
        command = login_argv(load_distro(record.settings.distro.value).shell)
        action = "enter"
    else:
        command = list(argv)
        action = "exec"
    home = exec_home(session.host_home, root=root)

    def run() -> int:
        exec_started = hooks.clock()
        try:
            status = podman.exec_interactive(
                cid,
                command,
                user=exec_user(session.uid, gid, root=root),
                env={**look.session.env, "HOME": home},
                workdir=home,
                tty=hooks.isatty(),
                interactive=True,
                pass_env=tuple(look.session.pass_through),
                on_spawn=client.started,
                on_exit=client.exited,
            )
        finally:
            client.exited()
        _observe(hooks, "exec", hooks.clock() - exec_started)
        return status

    if root and hooks.root_session is not None:
        status = hooks.root_session(record, cid, run)
    else:
        status = run()
    if status == PODMAN_ERROR_EXIT and not _running(_listed(podman, name, cid)):
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state="stopped",
            action=action,
            allowed="running",
        )
    return status


def _noop(mutation: Mutation, reason: str) -> None:
    """Record that the lock held no work: a `noop` step, and the reason in the audit params."""
    mutation.add_params(noop=reason)
    mutation.step("noop")


def _reattach_locked(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    look: _Look,
    *,
    session: HostUser,
    hooks: EnterHooks,
    notices: list[GinnWarning],
) -> bool:
    """Re-attach the changed grants under the realm lock (FR-123, ADR-008 amendment).

    Return False when the realm no longer runs, because it stopped after the look. The caller then
    starts it, which takes the lock itself. The state is read again under the lock (DR-033), and the
    grant list is decided again from the same host reads. A check that finds no work is recorded as
    a `noop` step, so the audit entry says that nothing was attached. The record is saved only when
    its grant list or its share list changed (DR-032). A failure raises, and the realm is left as it
    was: a failed re-attach never stops the realm.
    """
    with mutating(env, "reattach", name, {"generation": look.record.generation}) as mutation:
        running = _reattach_in_lock(
            env, podman, name, look, session=session, hooks=hooks, mutation=mutation
        )
    notices.extend(mutation.warnings)
    return running


def _reattach_in_lock(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    look: _Look,
    *,
    session: HostUser,
    hooks: EnterHooks,
    mutation: Mutation,
) -> bool:
    """The body of a re-attach, under the lock. Return False when the realm no longer runs."""
    store = RealmStore(env.paths, env.clock)
    mountinfo = hooks.lifecycle.mountinfo
    info = _listed(podman, name, look.record.container_id)
    current = _load(store, name)
    cid = current.container_id
    if cid is None or info is None or cid != info.id:
        _noop(mutation, _STOPPED_SINCE_LOOK)
        return False
    if info.state in _HALTED or current.state is not RealmState.RUNNING:
        _noop(mutation, _STOPPED_SINCE_LOOK)
        return False
    if info.pid < 2:
        raise _internal("the running realm has no init process to attach into")
    if not needs_reattach(current.grants, look.session, admit=look.admit, mountinfo=mountinfo):
        _noop(mutation, "no grant changed since the pre-check")
        return True
    result = reattach(
        podman,
        cid,
        pid=info.pid,
        grants=current.grants,
        session=look.session,
        realm_home=env.paths.homes / name.value,
        host_home=session.host_home,
        admit=look.admit,
        helper=hooks.lifecycle.helper,
        mountinfo=mountinfo,
    )
    share = share_after(
        current.share_skipped,
        current.grants,
        result.records,
        look.session,
        result,
        session.host_home,
    )
    if result.records != current.grants or set(share) != set(current.share_skipped):
        store.save(
            replace(
                current,
                grants=result.records,
                share_skipped=share,
                updated_at=utc_from_epoch(env.clock()),
            )
        )
    mutation.step("reattached")
    return True


__all__ = [
    "BUSY_STEP_S",
    "BUSY_WAIT_S",
    "PRECHECK_BUDGET_S",
    "EnterHooks",
    "enter_realm",
]
