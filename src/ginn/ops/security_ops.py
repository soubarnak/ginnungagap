# SPDX-License-Identifier: Apache-2.0
"""The realm operations of the security helpers: the Agent Proxy filter, the D-Bus proxy and the
NVIDIA spec (FR-067, FR-068, FR-069, ADR-007, ADR-012).

Agent filter. One filter serves the realms that have the agent toggle and run (FR-067). Its record
is `state/agent-filter.json`, and it names the realms that need the filter, as `members`. A realm
joins when it starts, before its grants attach, and it leaves when it stops, is deleted, or is
settled by recovery. Every sync also drops a member that no longer needs the filter (no record, or
a record that does not run, and no operation in flight), so a missed leave cannot keep the filter
running. The filter stops only when no member is left. Only a starting realm brings an upstream (its
own SSH_AUTH_SOCK). A stop, a delete and a recovery never read the environment, and never restart
the filter onto another agent. A plain OSError of the filter's state is raised as GINN-E-INTERNAL.
All of this runs under the host-wide lock, `store.locks.global_lock`, which is waited for with a
short retry (not a single try). The lock order of every operation is the realm lock first, then
this one, and this one never takes a realm lock.

D-Bus proxy. One proxy per realm, started with the first app ginn launches in it, and stopped with
the last (ADR-007, NFR-002). A launch is lock-free for the realm: it takes only the short lock of
the realm's proxy state, `runtime/proxy/<realm>.lock`, which guards `runtime/proxy/<realm>.json`. A
launch that finds the lock busy waits for it, so two launches never fail with a busy realm, and a
launch while an install, a sync or a permission change holds the realm lock proceeds at once. A
proxy that does not start is a warning (GINN-W-DBUS-PROXY): the app runs with no bus (ADR-010). The
proxy state is written only when it changed, so an idle release writes nothing (NFR-004).

NVIDIA. `write_nvidia` writes the CDI spec and the ld drop-in under the ginn data directory, only
when their bytes differ from what is on disk (once per digest, ADR-012). A stale driver is
GINN-E-NVIDIA-STALE from `nvidia.plan`, and only a realm that holds the grant meets it.
"""

import fcntl
import json
import os
import shutil
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from ginn.domain import RealmName, RealmState
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.ops._mutating import OpEnv
from ginn.runtime.realm_security import AGENT_DIR, PROXY_DIR, SecurityHooks, bus_plan, ssh_listen
from ginn.security import agent, dbus_filter
from ginn.security._proc import Child, Proc, stop_group
from ginn.security.nvidia import NvidiaPlan, OsProbe, Status, plan
from ginn.store.locks import global_lock
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths, write_bytes_atomic
from ginn.store.records import RealmStore, write_json_atomic

AGENT_STATE: Final = "agent-filter.json"
"""The host-wide filter record, in the Owned state directory."""
AGENT_LOG: Final = "agent-filter.log"
"""The filter's stderr, in the Owned state directory (not a pipe, see security/_proc.py)."""
AGENT_LOCK_WAIT_S: Final = 10.0
"""How long a command waits for the host-wide lock before it gives up (a short, bounded wait)."""
PROXY_LOCK_WAIT_S: Final = 10.0
"""How long a launch waits for another launch's proxy state. A proxy start is bounded by its own
START_TIMEOUT_S, so this is longer than one start."""
LOCK_STEP_S: Final = 0.02
"""Seconds between two tries of a busy short lock."""


def agent_state_path(paths: OwnedPaths) -> Path:
    return paths.state / AGENT_STATE


def _proxy_dir(paths: OwnedPaths) -> Path:
    return paths.runtime / PROXY_DIR


def proxy_state_path(paths: OwnedPaths, name: RealmName) -> Path:
    """The state of a realm's D-Bus proxy: `runtime/proxy/<realm>.json` (ADR-004, CL-14)."""
    return _proxy_dir(paths) / f"{name.value}.json"


def proxy_log_path(paths: OwnedPaths, name: RealmName) -> Path:
    return paths.state / f"dbus-{name.value}.log"


def read_agent_record(paths: OwnedPaths) -> agent.AgentRecord:
    """The stored filter record, or an empty one when none was written. A malformed file raises."""
    path = agent_state_path(paths)
    if not path.is_file():
        return agent.AgentRecord()
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return agent.AgentRecord.from_dict(data)
    except (ValueError, TypeError) as exc:
        err = make("GINN-E-INTERNAL")
        err.details.update({"path": str(path), "reason": f"agent filter state: {exc}"})
        raise err from exc


@contextmanager
def _agent_lock(paths: OwnedPaths, op: str) -> Iterator[None]:
    """The host-wide lock, waited for in short steps for AGENT_LOCK_WAIT_S (not a single try)."""
    deadline = time.monotonic() + AGENT_LOCK_WAIT_S
    while True:
        held = global_lock(paths, op)
        try:
            held.__enter__()
        except GinnError as err:
            if err.code != "GINN-E-REALM-BUSY" or time.monotonic() >= deadline:
                raise
            time.sleep(LOCK_STEP_S)
            continue
        break
    try:
        yield
    finally:
        held.__exit__(None, None, None)


@contextmanager
def _proxy_lock(paths: OwnedPaths, name: RealmName) -> Iterator[None]:
    """The short lock of one realm's proxy state, waited for up to PROXY_LOCK_WAIT_S."""
    directory = _proxy_dir(paths)
    directory.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
    fd = os.open(directory / f"{name.value}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + PROXY_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise make(
                        "GINN-E-DBUS-PROXY",
                        realm=name.value,
                        detail="the proxy state is held by another command",
                    ) from None
                time.sleep(LOCK_STEP_S)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _filter_spec(
    runtime: Path, host_env: Mapping[str, str], uid: int, hooks: SecurityHooks
) -> agent.FilterSpec | None:
    """The filter helper for the caller's host agent, or None when its session has no SSH agent."""
    planned = agent.plan_realm(
        host_env,
        uid=uid,
        helper=hooks.agent_helper,
        listen=ssh_listen(runtime),
        gpg_extra=None,
        file_type=hooks.file_type,
    )
    return planned.filter


def _store_if_changed(paths: OwnedPaths, stored: agent.AgentRecord, new: agent.AgentRecord) -> None:
    """Write the filter record only when it changed, so an idle command writes nothing."""
    if new.to_dict() != stored.to_dict():
        paths.state.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
        write_json_atomic(agent_state_path(paths), new.to_dict())


def _needs_filter(env: OpEnv, member: str) -> bool:
    """True when realm `member` still needs the filter (FR-067), so a sync keeps it.

    A running realm needs it. So does a realm with a live journal entry, an operation in flight,
    because its record may still read stopped while it joins. A corrupt journal file, and an entry
    whose process has died, do not keep it: recovery settles those and calls `leave_agent` itself.
    A realm whose record is gone does not, and neither does a stopped one. A read that fails is
    treated as a need, so a damaged record never silently stops the filter.
    """
    try:
        realm = RealmName(member)
    except ValueError:
        return False
    try:
        journal = env.journal.load(realm)
        if journal is not None and journal.is_live():
            return True
        record = RealmStore(env.paths, env.clock).load(realm)
    except GinnError as err:
        return err.code != "GINN-E-NO-SUCH-REALM"
    except OSError:
        return True
    return record.state is RealmState.RUNNING


def _agent_sync(
    env: OpEnv,
    name: RealmName,
    *,
    adding: bool,
    host_env: Mapping[str, str],
    uid: int,
    hooks: SecurityHooks,
) -> agent.Action:
    """Add or remove realm `name` from the filter's members, and bring the filter in line.

    A plain OSError (a record that cannot be read, a directory that cannot be made) is raised as
    GINN-E-INTERNAL, so a caller's error handling and rollback see one error type (FR-067).
    """
    try:
        with _agent_lock(env.paths, f"agent filter for {name.value}"):
            stored = read_agent_record(env.paths)
            # A member that no longer needs the filter is dropped here, so no sync keeps it alive.
            members = {m for m in stored.members if m == name.value or _needs_filter(env, m)}
            if adding:
                members.add(name.value)
                spec = _filter_spec(env.paths.runtime, host_env, uid, hooks)
                if spec is not None:
                    (env.paths.runtime / AGENT_DIR).mkdir(mode=OWNED_DIR_MODE, exist_ok=True)
            else:
                members.discard(name.value)
                spec = None
            launch = hooks.agent_launch or agent.filter_launcher(
                ssh_listen(env.paths.runtime), str(env.paths.state / AGENT_LOG)
            )
            new, action = agent.sync(
                stored, spec, development_running=sorted(members), launch=launch
            )
            _store_if_changed(env.paths, stored, new)
    except OSError as exc:
        err = make("GINN-E-INTERNAL")
        err.details.update({"reason": f"agent filter state: {exc.strerror or exc}"})
        raise err from exc
    return action


def sync_agent(
    env: OpEnv,
    *,
    name: RealmName,
    host_env: Mapping[str, str],
    uid: int,
    hooks: SecurityHooks,
) -> agent.Action:
    """Join realm `name` to the filter, which it needs at start (FR-067). Return its action.

    `host_env` and `uid` are the starting caller's session: its SSH_AUTH_SOCK is the upstream. A
    caller without one keeps a running filter, and starts none (its Development grant is skipped).
    """
    return _agent_sync(env, name, adding=True, host_env=host_env, uid=uid, hooks=hooks)


def leave_agent(env: OpEnv, name: RealmName) -> agent.Action:
    """Remove realm `name` from the filter's members, at stop, delete and recovery (FR-067).

    The filter stops when no member is left. This never reads the environment and never starts a
    filter, so it needs no session (the upstream comes from a starting caller only).
    """
    return _agent_sync(env, name, adding=False, host_env={}, uid=0, hooks=SecurityHooks())


def _read_proxy(paths: OwnedPaths, name: RealmName) -> dbus_filter.ProxyState:
    """The realm's proxy state, or an empty one when no app was launched. Malformed state raises."""
    path = proxy_state_path(paths, name)
    if not path.is_file():
        return dbus_filter.ProxyState(realm=name.value)
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return dbus_filter.ProxyState.from_dict(data)
    except (ValueError, TypeError) as exc:
        err = make("GINN-E-INTERNAL")
        err.details.update({"path": str(path), "reason": f"proxy state: {exc}"})
        raise err from exc


def _write_proxy(paths: OwnedPaths, name: RealmName, state: dbus_filter.ProxyState) -> None:
    path = proxy_state_path(paths, name)
    path.parent.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
    write_json_atomic(path, state.to_dict())


def _write_proxy_if_changed(
    paths: OwnedPaths, name: RealmName, stored: dbus_filter.ProxyState, new: dbus_filter.ProxyState
) -> None:
    """Write the proxy state only when it differs from the state that was read (NFR-004)."""
    if new.to_dict() != stored.to_dict():
        _write_proxy(paths, name, new)


def _proxy_detail(err: Exception) -> str:
    """The reason a proxy did not start, as the detail of its GINN-W-DBUS-PROXY warning (FR-068)."""
    if isinstance(err, GinnError):
        return err.message
    if isinstance(err, OSError):
        return f"cannot prepare the proxy socket: {err.strerror or err}"
    return str(err)


def start_app(
    env: OpEnv,
    name: RealmName,
    *,
    host_env: Mapping[str, str],
    uid: int,
    hooks: SecurityHooks,
    notify: Callable[[GinnWarning], None] | None = None,
) -> Proc:
    """Register this process as an app of realm `name`, and start its D-Bus proxy if none runs.

    Return the process record, which `finish_app` takes back. Only the proxy state's short lock is
    taken, never the realm lock (ADR-010). The proxy starts under that lock. The state is written
    with the proxy when the start succeeded (FR-068, ADR-007). A proxy that does not start, or whose
    socket path cannot be prepared (an OSError or a ValueError), is a GINN-W-DBUS-PROXY warning,
    passed to `notify` when it is given: the app is still registered,
    and it runs with no proxy, so it fails closed on its bus. A realm with no bus (no session
    address, no proxy binary, or an AS-012 refusal) is the same, with no warning. A proxy started
    whose state cannot be written is stopped again, so no proxy runs without a record.
    """
    proc = Proc.of(os.getpid())
    if proc is None:
        raise make("GINN-E-INTERNAL")
    with _proxy_lock(env.paths, name):
        stored = _read_proxy(env.paths, name)
        state, action = dbus_filter.launched(stored, proc)
        started: Child | None = None
        if action is dbus_filter.Action.START:
            proxy = bus_plan(
                host_env, uid=uid, realm=name.value, runtime=env.paths.runtime, hooks=hooks
            )
            if isinstance(proxy, dbus_filter.ProxyPlan):
                socket_dir = _proxy_dir(env.paths) / name.value
                try:
                    socket_dir.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
                    launch = hooks.proxy_launch or dbus_filter.launcher_for(
                        name.value, proxy.listen, str(proxy_log_path(env.paths, name))
                    )
                    started = dbus_filter.start_proxy(proxy, launch)
                except (GinnError, OSError, ValueError) as err:
                    if notify is not None:
                        notify(
                            warn("GINN-W-DBUS-PROXY", realm=name.value, detail=_proxy_detail(err))
                        )
        try:
            if started is not None:
                state = dbus_filter.with_proxy(state, started.proc)
            _write_proxy_if_changed(env.paths, name, stored, state)
        except BaseException:
            if started is not None:
                stop_group(started.proc)
            raise
    return proc


def finish_app(env: OpEnv, name: RealmName, proc: Proc) -> None:
    """Forget the app that `start_app` registered, and stop the proxy when it was the last one."""
    with _proxy_lock(env.paths, name):
        stored = _read_proxy(env.paths, name)
        state, action = dbus_filter.exited(stored, proc)
        if action is dbus_filter.Action.STOP:
            state = dbus_filter.stop_proxy(state)
        _write_proxy_if_changed(env.paths, name, stored, state)


def reconcile_proxy(env: OpEnv, name: RealmName) -> None:
    """Drop the apps that are gone and stop a proxy with no app left, at a stop (ADR-007)."""
    with _proxy_lock(env.paths, name):
        stored = _read_proxy(env.paths, name)
        state, action = dbus_filter.reconcile(stored)
        if action is dbus_filter.Action.STOP:
            state = dbus_filter.stop_proxy(state)
        _write_proxy_if_changed(env.paths, name, stored, state)


def forget_proxy(env: OpEnv, name: RealmName) -> None:
    """Stop the realm's proxy, and remove its state, its socket directory and its log (delete)."""
    with _proxy_lock(env.paths, name):
        state = _read_proxy(env.paths, name)
        dbus_filter.stop_proxy(state)
        proxy_state_path(env.paths, name).unlink(missing_ok=True)
        shutil.rmtree(_proxy_dir(env.paths) / name.value, ignore_errors=True)
        proxy_log_path(env.paths, name).unlink(missing_ok=True)


def nvidia_plan(paths: OwnedPaths, hooks: SecurityHooks) -> NvidiaPlan:
    """The NVIDIA grant of this host. A stale driver raises GINN-E-NVIDIA-STALE (R-005)."""
    probe = OsProbe() if hooks.nvidia_probe is None else hooks.nvidia_probe
    return plan(probe, str(paths.data))


def _write_once(path: Path, text: str, mode: int) -> bool:
    """Write `text` to `path` unless the regular file already holds exactly it (ADR-012)."""
    data = text.encode("utf-8")
    if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
        path.parent.mkdir(mode=mode, parents=True, exist_ok=True)
        write_bytes_atomic(path, data)
        return True
    return False


def write_nvidia(paths: OwnedPaths, result: NvidiaPlan) -> bool:
    """Write the CDI spec and the ld drop-in of a ready grant, once per digest (ADR-012).

    Return True when a file was written. A plan that is not READY writes nothing.
    """
    if result.status is not Status.READY:
        return False
    spec = _write_once(Path(result.spec_path), result.spec_text, OWNED_DIR_MODE)
    ld = _write_once(Path(result.ld_path), result.ld_text, OWNED_DIR_MODE)
    return spec or ld


__all__ = [
    "AGENT_STATE",
    "agent_state_path",
    "finish_app",
    "forget_proxy",
    "leave_agent",
    "nvidia_plan",
    "proxy_state_path",
    "read_agent_record",
    "reconcile_proxy",
    "start_app",
    "sync_agent",
    "write_nvidia",
]
