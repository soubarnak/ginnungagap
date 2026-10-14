# SPDX-License-Identifier: Apache-2.0
"""Start, stop, status and list a realm (FR-016, FR-020, FR-025, FR-064, FR-066, FR-081).

`start_realm` and `stop_realm` are mutating operations. Each runs inside `mutating()`, which takes
the realm lock, writes the journal entry and appends one audit entry (AGENTS.md, DR-032, FR-123,
FR-124). `realm_status` and `list_realms` are read-only (FR-020). They take no lock, write no
journal or audit entry, and write no file. Each record write is atomic (DR-035), so a read sees
the old record or the new one.

Start (FR-016, ADR-008 amendment). The grants of this start are planned first. Then the record is
saved with those grants and no identities (journal step `grants-reset`), so a crash after the
container starts cannot leave an identity from the last start that looks current (T4.6). Then the
container starts (`started`), and the grants attach (`attached`). The grant list is saved on the
record, so enter and launch reuse the list fixed here (FR-066). A missing source is recorded in
`share_skipped`, and the start goes on (FR-064). The record reads `running` only after the attach
succeeds (`running`), because DR-020 has no state between the two. A failed attach stops the
container again, leaves the realm stopped, and fails the start with GINN-E-ATTACH-FAILED.

Journal step names, in order, and stable: start writes `grants-reset`, `started`, `attached` and
`running`; stop writes `stopped`. A journal entry that stops at `grants-reset` or `started` has a
container that may run while the record says stopped, and recovery (T2.8) reads it that way.

Stop (FR-016). `podman stop` waits `LifecycleHooks.stop_timeout_s` seconds (30 by default, AS-023).
Stop leaves `restart_required` as it is, since only a start applies a changed grant list.

Failure after the container starts. The stop that follows a failed start uses a zero timeout, as
create's rollback does. The record is written stopped only when that stop succeeds, or when podman
lists the container absent, created or exited. Otherwise the record keeps the state it had, and the
original error is raised with a note; a failure to write the record is a note too, so the attach
error stays the one raised. A running or failed realm whose stop fails is recorded stopped under
the same rule (DR-020 allows running to stopped). Any other state, such as paused, counts as
running.

Refusals. A realm whose settings are services-on is started and stopped only by its `sv` service
(ADR-003, FR-081). Until T7.4 both raise GINN-E-NOT-SUPPORTED-YET (exit 2) before any write. A start
whose record names container options that its container was not made with raises the same code,
naming T8.3, before any write (ADR-006; a tier change needs a new container). A state that DR-020
does not allow for the verb raises GINN-E-REALM-STATE (exit 4). An unknown name raises
GINN-E-NO-SUCH-REALM before any write (FR-025). A container that podman does not list is not
running. A podman error while listing or inspecting a container is not read as "not running": it
propagates, and nothing is written.

Idempotence. Start of a realm that podman runs already returns the record and writes nothing. It
keeps `restart_required` as stored, so the caller can report a pending restart (FR-066; M6 sets
the flag). Stop of a stopped record asks podman: when the container runs, `podman stop` runs and
the record stays stopped; otherwise nothing is written. Neither takes a journal or audit entry
when it writes nothing.

Crash. A record that says `running` while podman does not run its container is a crash
(DR-020, running to failed). Start then goes failed to running. Stop takes it to stopped. A
running or failed record that names no container id is refused by start and stop with
GINN-E-INTERNAL, before any state write. Its remedy is `ginn delete`, which finds the container by
its labels (T4.7).

Warnings. The FR-006 warning of a start or a stop is known only after the body has run, so it
is appended to the `warnings` list of the call, once the operation has finished. A call that
writes nothing has none. A failed operation raises, and its warning goes into the error's notes.

Status and list. `RealmStatus.to_json()` gives these keys, in this order, and a golden test pins
them: name, state, container_state, state_mismatch, distro, tier, services, generation,
image_digest, created_at, updated_at, restart_required, share_skipped, disk, exported_apps, error.
- state: the record's state, verbatim (DR-020). It is never rewritten from podman's view.
- container_state: podman's state of the record's container ("created", "running", "exited",
  ...). "absent" means podman lists no such container. "unavailable" means podman did not answer;
  "unknown" is podman's own state for a container it does not model. Null means the container is
  not probed: the state is not running, stopped or failed, or the record has no container id.
- state_mismatch: true when podman's view disagrees with the record (a running record whose
  container is not running, a stopped record whose container runs, a failed record whose
  container runs). Podman runs a container in any state except absent, created and exited, so a
  paused container counts as running. Null when the container is not probed, when podman did not
  answer, or when podman reports its own "unknown" state: ginn cannot compare that one.
- disk: `realm_home_bytes` is the allocated size of the Realm Home (FR-020), and
  `realm_home_partial` is true when the walk stopped early. The walk does not cross a file system
  boundary, and it does not follow links. `container_bytes` is null: the CLI adapter never asks
  podman for a size (no `--size`), so the container layer size is a gap. The walk is
  `LifecycleHooks.tree_usage`, which T8.2 may replace.
- exported_apps: null until export/ exists (M5).
- error: null for a readable record. A corrupt or unreadable record keeps its name, and `error`
  holds the catalogue code and the reason; every other value is null.

Requirements not covered here. The CLI wiring is a later task. The grant list is not diffed
against the previous one at start, so a grant that is removed from the host is only skipped, not
reported as removed (FR-066, M6). The forced-stop log (FR-016 AC-2) is for services-on only.
The grants of a start follow the record's tier and Custom permissions (FR-061..063, runtime/mounts).
"""

import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from ginn.domain import RealmName, RealmState, transition
from ginn.domain.permissions import effective_toggles
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops.security_ops import (
    leave_agent,
    nvidia_plan,
    reconcile_proxy,
    sync_agent,
    write_nvidia,
)
from ginn.podman.adapter import Podman
from ginn.podman.models import ContainerInfo, ContainerState
from ginn.runtime.attach import (
    MOUNTINFO,
    AttachResult,
    admission,
    attach_at_start,
    records_from_plan,
    share_skipped,
)
from ginn.runtime.env import SessionEnv, session_env
from ginn.runtime.mounts import AttachPlan, Exists, attach_plan
from ginn.runtime.realm_security import (
    SecurityHooks,
    agent_enabled,
    session_extras,
    stored_gpg_extra,
)
from ginn.runtime.spec import LABEL_REALM
from ginn.security import agent
from ginn.security.caps import option_keys
from ginn.store._time import utc_from_epoch, utc_iso
from ginn.store.records import RealmRecord, RealmStore

STOP_TIMEOUT_S: Final = 30
"""Seconds `podman stop` waits before it kills the realm (FR-016, AS-023)."""
ROLLBACK_STOP_TIMEOUT_S: Final = 0
"""A start that failed has no state to save, so its stop does not wait (as create's rollback)."""
USAGE_ENTRY_BUDGET: Final = 100_000
"""Entries the Realm Home walk reads before it stops and reports a partial total (FR-020)."""
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
"""Opens a directory without following a link at its last component (du, FR-020)."""
_PROBED: Final = frozenset({RealmState.RUNNING, RealmState.STOPPED, RealmState.FAILED})
"""The states whose container podman is asked about in a status."""
_START_FROM: Final = frozenset({RealmState.STOPPED, RealmState.FAILED})
_STOP_FROM: Final = frozenset({RealmState.RUNNING, RealmState.FAILED})
_WITH_CONTAINER: Final = frozenset({RealmState.RUNNING, RealmState.FAILED})
"""The states that name a container ginn must stop or check. A record in one of them with no
container id is refused by start and stop, and is left for `ginn delete` (T4.7)."""
_ABSENT: Final = "absent"
_UNAVAILABLE: Final = "unavailable"
"""The container_state of a status whose podman query failed. It is not a podman state, so it
never equals ContainerState.UNKNOWN, which is podman's own "unknown"."""
_PODMAN_UNKNOWN: Final = ContainerState.UNKNOWN.value
"""podman's own state for a container that ginn does not model. Its mismatch is null (cannot
compare), and it is shown as "unknown", never as "unavailable"."""
_HALTED: Final = frozenset({ContainerState.CREATED, ContainerState.EXITED})
"""The container states that mean the process is not running. Any other state, such as paused or
stopping, counts as running: a stop is then asked of podman, which is the safe reading."""


def _allocated(st: os.stat_result) -> int:
    """Bytes the file system allocates for `st`, as du counts them. Size when blocks are unknown."""
    blocks: int | None = getattr(st, "st_blocks", None)
    return st.st_size if blocks is None else blocks * 512


def _same_inode(opened: os.stat_result, listed: os.stat_result) -> bool:
    return opened.st_dev == listed.st_dev and opened.st_ino == listed.st_ino


def _lstat_entry(entry: os.DirEntry[str]) -> os.stat_result:
    """The entry's own stat, without following a link. A seam for the walk's tests."""
    return entry.stat(follow_symlinks=False)


def _tree_usage(root: Path, *, budget: int = USAGE_ENTRY_BUDGET) -> tuple[int, bool]:
    """Allocated bytes under `root`, the way du counts them (FR-020). The flag is True when partial.

    A directory is opened with O_DIRECTORY and O_NOFOLLOW, and its descriptor must be the inode
    that the lstat found. Its entries are read with scandir on that descriptor. A link counts as
    itself, and a file with several names counts once. An entry on another device is skipped, as
    `du -x` skips it. The walk stops reading once `budget` entries are read, and each path that
    cannot be read makes the flag True.
    """
    try:
        root_st = os.lstat(root)
    except OSError:
        return 0, True
    dev = root_st.st_dev
    total = _allocated(root_st)
    seen: set[tuple[int, int]] = {(root_st.st_dev, root_st.st_ino)}
    entries = 1  # the root itself
    partial = False
    pending: list[tuple[str, os.stat_result]] = []
    if stat.S_ISDIR(root_st.st_mode):
        pending.append((os.fspath(root), root_st))
    while pending:
        if entries >= budget:
            return total, True
        path, st = pending.pop()
        try:
            fd = os.open(path, _DIRECTORY_FLAGS)
        except OSError:
            partial = True
            continue
        try:
            if not _same_inode(os.fstat(fd), st):
                partial = True
                continue
            with os.scandir(fd) as listing:
                for entry in listing:
                    if entries >= budget:
                        partial = True
                        break
                    entries += 1
                    try:
                        child = _lstat_entry(entry)
                    except OSError:
                        partial = True
                        continue
                    key = (child.st_dev, child.st_ino)
                    if child.st_dev != dev or key in seen:
                        continue
                    seen.add(key)
                    total += _allocated(child)
                    if stat.S_ISDIR(child.st_mode):
                        pending.append((os.path.join(path, entry.name), child))
        except OSError:
            partial = True
        finally:
            os.close(fd)
    return total, partial


@dataclass(frozen=True)
class HostUser:
    """The host user whose realm starts: the uid, the home path and the host environment.

    The record does not keep these, so each start takes them. `host_env` is read for its
    allowlisted names only (runtime/env.py). Its values are never stored.
    """

    uid: int
    host_home: Path
    host_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleHooks:
    """The seams for the parts that touch the host or wait. The defaults are the real ones.

    `exists`, `mountinfo` and `helper` are the probes and paths that runtime/ reads, as in
    CreateHooks. `stop_timeout_s` is the wait before a kill. `tree_usage` measures a Realm Home
    and returns its bytes and whether the walk was partial (FR-020).
    """

    exists: Exists = os.path.exists
    mountinfo: Path = MOUNTINFO
    helper: str | None = None
    stop_timeout_s: int = STOP_TIMEOUT_S
    tree_usage: Callable[[Path], tuple[int, bool]] = _tree_usage
    security: SecurityHooks = field(default_factory=SecurityHooks)
    """The seams of the security helpers: the filter, the proxy, gpg and the NVIDIA probe."""
    before_start: Callable[[], None] | None = None
    """Runs when the start is about to call `podman start`, not when it returns early for a
    container that runs. A services-on start removes the stale conmon pidfile here (FR-081)."""


@dataclass(frozen=True)
class DiskUsage:
    """The disk use of one realm. `container_bytes` is None until podman reports a size."""

    realm_home_bytes: int
    realm_home_partial: bool
    container_bytes: int | None

    def to_json(self) -> dict[str, object]:
        return {
            "realm_home_bytes": self.realm_home_bytes,
            "realm_home_partial": self.realm_home_partial,
            "container_bytes": self.container_bytes,
        }


@dataclass(frozen=True)
class RealmStatus:
    """One realm as `status` and `list` show it. Keys: see `to_json` and the module docstring."""

    name: str
    state: str | None
    container_state: str | None
    state_mismatch: bool | None
    distro: str | None
    tier: str | None
    services: bool | None
    generation: int | None
    image_digest: str | None
    created_at: str | None
    updated_at: str | None
    restart_required: bool | None
    share_skipped: list[str] | None
    disk: DiskUsage | None
    error: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state,
            "container_state": self.container_state,
            "state_mismatch": self.state_mismatch,
            "distro": self.distro,
            "tier": self.tier,
            "services": self.services,
            "generation": self.generation,
            "image_digest": self.image_digest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "restart_required": self.restart_required,
            "share_skipped": None if self.share_skipped is None else list(self.share_skipped),
            "disk": None if self.disk is None else self.disk.to_json(),
            "exported_apps": None,
            "error": self.error,
        }


def _text(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_status_text(status: RealmStatus) -> str:
    """The text that `ginn status` prints for one realm (FR-064 AC-1, FR-066 AC-1). Pure.

    One `key: value` line per field, in `to_json` order. Each share skip is its own line, verbatim,
    as in `share skipped: ~/Downloads (missing)`. A set restart flag is the line `restart required`.
    A corrupt record shows its `error` line last.
    """
    lines = [
        f"{key}: {_text(value)}"
        for key, value in status.to_json().items()
        if key not in ("share_skipped", "disk", "error")
    ]
    if status.disk is not None:
        partial = " (partial)" if status.disk.realm_home_partial else ""
        lines.append(f"disk: realm home {status.disk.realm_home_bytes} bytes{partial}")
    lines.extend(status.share_skipped or [])
    if status.restart_required:
        lines.append("restart required")
    if status.error is not None:
        lines.append(f"error: {status.error}")
    return "\n".join(lines)


def _internal(detail: str, **values: object) -> GinnError:
    err = make("GINN-E-INTERNAL")
    err.details.update({"detail": detail, **values})
    return err


def _no_container(name: RealmName, state: RealmState) -> GinnError:
    """A running or failed record that names no container. Start and stop cannot act on it.

    The remedy is `ginn delete`, which finds the realm's containers by their labels (T4.7). The
    record is not written here.
    """
    err = _internal("the realm record names no container", realm=name.value, state=state.value)
    err.remedy = f"run 'ginn delete {name.value}', which removes the realm's containers by label"
    return err


def _load(store: RealmStore, name: RealmName) -> RealmRecord:
    """Read a record. A missing file is GINN-E-NO-SUCH-REALM. Any other read error is
    GINN-E-INTERNAL, as a corrupt record is (FR-020), so no caller sees a raw OSError."""
    try:
        return store.load(name)
    except FileNotFoundError:
        raise make("GINN-E-NO-SUCH-REALM", realm=name.value) from None
    except OSError as exc:
        err = _internal("cannot read the realm record", realm=name.value)
        err.details["reason"] = exc.strerror or str(exc)
        raise err from exc


def _save(env: OpEnv, store: RealmStore, record: RealmRecord, **changes: Any) -> RealmRecord:
    """Write `record` with `changes` and a fresh `updated_at`. Returns the record written."""
    saved = replace(record, updated_at=utc_from_epoch(env.clock()), **changes)
    store.save(saved)
    return saved


def _refuse_unsupported(record: RealmRecord, verb: str) -> None:
    """Refuse a realm this build cannot run yet. Raises before any write (ADR-003, FR-081).

    A services-on realm is started and stopped only by its sv service (ADR-003), so ginn does not
    start or stop it with podman here.
    """
    if record.settings.services:
        raise make(
            "GINN-E-NOT-SUPPORTED-YET",
            realm=record.name.value,
            feature=f"{verb} of a services-on realm",
            milestone="M7 (T7.4)",
        )


def _refuse_stale_options(record: RealmRecord) -> None:
    """Refuse a start whose tier options differ from the ones the container was made with.

    A create-time option (a device, a security option or a group) is fixed at `podman create`.
    `created_options` keeps the names of the toggle-driven options of the container (ADR-006), and
    a tier or permission change that would change them is refused by `ginn tier` and `ginn perms`.
    This check covers a record that was changed another way. A new container is made by T8.3, which
    keeps the Install Manifest's packages, so the start is refused until then, before any write.
    """
    kvm_present = record.kvm_at_create
    toggles = effective_toggles(record.settings.tier, record.permissions.toggles)
    wanted = option_keys(toggles, kvm_present=kvm_present)
    if wanted != record.created_options or not record.create_grants_match(toggles):
        raise make(
            "GINN-E-NOT-SUPPORTED-YET",
            realm=record.name.value,
            feature="a start with tier options that the container was not made with",
            milestone="M8 (T8.3)",
        )


def nvidia_plan_write(env: OpEnv, hooks: LifecycleHooks) -> None:
    """Refresh the CDI spec of a GPU realm from the host (FR-069, R-005). A stale driver raises."""
    write_nvidia(env.paths, nvidia_plan(env.paths, hooks.security))


_LEFT_DETAIL: Final = "a listener at the filter socket is not this filter; the SSH grant is left"


def _agent_warning(name: RealmName, detail: str) -> GinnWarning:
    return warn("GINN-W-AGENT-FILTER", realm=name.value, detail=detail)


def _detail(err: BaseException) -> str:
    return err.message if isinstance(err, GinnError) else str(err)


def _leave_agent_quietly(env: OpEnv, name: RealmName, err: GinnError) -> None:
    """After a failed start, leave the filter. A failure here is a note on the start's error."""
    try:
        leave_agent(env, name)
    except (GinnError, OSError) as leave_err:
        err.add_note(f"agent filter after failed start: {_detail(leave_err)}")


def _release_after_stop(env: OpEnv, name: RealmName, mutation: Mutation) -> None:
    """After a stop: leave the filter, and stop a proxy with no app left (FR-067, ADR-007).

    The stop has happened, so a failure here is a warning and never an error (F).
    """
    try:
        leave_agent(env, name)
    except (GinnError, OSError) as err:
        mutation.warnings.append(_agent_warning(name, _detail(err)))
    try:
        reconcile_proxy(env, name)
    except (GinnError, OSError) as err:
        mutation.warnings.append(
            warn("GINN-W-PROXY-RELEASE", realm=name.value, detail=_detail(err))
        )


def _host_user(env: OpEnv) -> HostUser:
    """The host user of this process, used when a caller passes no session."""
    return HostUser(uid=env.paths.uid, host_home=Path.home(), host_env=os.environ)


def _running(podman: Podman, name: RealmName, container_id: str | None) -> bool:
    """True when podman lists the container and its state is not created or exited.

    A container that podman does not list is a definite answer: it is not running. A podman error
    while listing propagates, and so does an inspect error on a listed container. Neither says
    what the state is, so no step, stop or state write follows from it.
    """
    if container_id is None:
        return False
    if not any(info.id == container_id for info in podman.ps({LABEL_REALM: name.value})):
        return False
    return podman.inspect(container_id).state not in _HALTED


def _confirmed_not_running(podman: Podman, name: RealmName, container_id: str) -> bool:
    """True when podman lists the container absent, created or exited. Any other state, such as
    paused or stopping, counts as still running. A podman error is no confirmation, so the caller
    keeps the state it found."""
    try:
        listed = podman.ps({LABEL_REALM: name.value})
    except GinnError:
        return False
    return all(info.state in _HALTED for info in listed if info.id == container_id)


@dataclass(frozen=True)
class _Grants:
    """The grants of one start: the plan, the session it was made from, and the Owned Paths."""

    plan: AttachPlan
    session: SessionEnv
    owned: list[Path]


def _resolve_gpg(record: RealmRecord, hooks: LifecycleHooks) -> RealmRecord:
    """Resolve the gpg extra socket of an Agent Proxy realm once, at its start (NFR-004).

    The record keeps the value, so each enter and launch reads it instead of running `gpgconf`.
    A realm without the agent toggle needs no socket, so its record is returned as it is.
    """
    if not agent_enabled(record.settings.tier, record.permissions.toggles):
        return record
    return replace(record, gpg_extra=hooks.security.gpg_extra(), gpg_extra_known=True)


def _plan_grants(env: OpEnv, record: RealmRecord, user: HostUser, hooks: LifecycleHooks) -> _Grants:
    owned = env.paths.all_owned_roots()
    extras = session_extras(
        user.host_env,
        agent_on=agent_enabled(record.settings.tier, record.permissions.toggles),
        uid=user.uid,
        realm=record.name.value,
        runtime=env.paths.runtime,
        hooks=hooks.security,
        gpu=record.gpu_cdi,
        gpg_extra=stored_gpg_extra(
            hooks.security, known=record.gpg_extra_known, stored=record.gpg_extra
        ),
    )
    session = session_env(
        user.host_env,
        home=user.host_home,
        uid=user.uid,
        owned_paths=owned,
        extras=extras,
        toggles=effective_toggles(record.settings.tier, record.permissions.toggles),
    )
    plan = attach_plan(
        record.settings.tier,
        host_home=user.host_home,
        uid=user.uid,
        user=env.user,
        owned_paths=owned,
        runtime_root=env.paths.runtime,
        session_binds=session.binds,
        env=user.host_env,
        exists=hooks.exists,
        permissions=record.permissions,
    )
    return _Grants(plan=plan, session=session, owned=owned)


def start_realm(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    session: HostUser | None = None,
    hooks: LifecycleHooks | None = None,
    warnings: list[GinnWarning] | None = None,
    supervised: bool = False,
) -> RealmRecord:
    """Start realm `name` and attach its grants. Return the record that now reads running.

    A realm that podman runs already comes back as stored, with its `restart_required` flag. See
    the module docstring for the states it accepts, the refusals and the journal steps. The FR-006
    warnings of the start (a host change found after the start) are appended to `warnings` when it
    is given, after the operation has finished.

    `supervised` is True only for the start that the realm's runit service makes (`ginn
    _service-run`, FR-081). Only such a start runs a services-on realm; a services-off realm is
    refused, because its runit service does not exist.
    """
    hooks = LifecycleHooks() if hooks is None else hooks
    current = _load(RealmStore(env.paths, env.clock), name)
    if supervised:
        if not current.settings.services:
            raise _internal("the supervisor starts only a services-on realm", realm=name.value)
    else:
        _refuse_unsupported(current, "start")
    user = _host_user(env) if session is None else session
    if not user.host_home.is_absolute():
        raise ValueError(f"host home must be absolute, not {user.host_home}")
    if current.state is RealmState.RUNNING and _running(podman, name, current.container_id):
        return current
    _refuse_stale_options(current)
    params = {"generation": current.generation}
    with mutating(env, "start", name, params) as mutation:
        record = _start(env, podman, name, user, hooks, mutation)
    if warnings is not None:
        warnings.extend(mutation.warnings)
    return record


def _start(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    user: HostUser,
    hooks: LifecycleHooks,
    mutation: Mutation,
) -> RealmRecord:
    """The body of a start. It runs under the realm lock, after the journal entry is written."""
    store = RealmStore(env.paths, env.clock)
    record = _load(store, name)
    if record.container_id is None and record.state in _WITH_CONTAINER:
        # Checked before the crash write below, so no state is written for this record.
        raise _no_container(name, record.state)
    if record.state is RealmState.RUNNING:
        if _running(podman, name, record.container_id):
            return record
        record = store.set_state(name, RealmState.FAILED)  # a crash: running to failed (DR-020)
    if record.state not in _START_FROM:
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state=record.state.value,
            action="start",
            allowed="stopped or failed",
        )
    transition(record.state, RealmState.RUNNING, record.previous_state)
    cid = record.container_id
    if cid is None:
        raise _internal("the realm has no container to start", realm=name.value)
    record = _resolve_gpg(record, hooks)
    grants = _plan_grants(env, record, user, hooks)
    # Written before the container starts: the identities of the last start are cleared, so a
    # crash after this point cannot leave a grant that looks attached with a stale identity (T4.6).
    record = _save(env, store, record, grants=records_from_plan(grants.plan, grants.session))
    mutation.step("grants-reset")
    if record.gpu_cdi:
        # The CDI spec is written before the container starts, so podman finds the device (ADR-012).
        nvidia_plan_write(env, hooks)
    if hooks.before_start is not None:
        hooks.before_start()
    try:
        podman.start(cid)
        info = podman.inspect(cid)
        if info.state is not ContainerState.RUNNING or info.pid < 2:
            raise _internal("the realm is not running after start", state=info.state.value)
        mutation.step("started")
        if agent_enabled(record.settings.tier, record.permissions.toggles):
            # A missing helper is a skip: the start has no SSH grant, and it warns (ADR-008).
            if any(skip.reason == agent.HELPER_MISSING for skip in grants.session.skipped):
                helper = hooks.security.agent_helper
                detail = f"{helper} is not installed; the SSH grant is skipped"
                mutation.warnings.append(_agent_warning(name, detail))
            # The filter listens before the grants attach, or its socket is skipped (FR-067).
            action = sync_agent(
                env, name=name, host_env=user.host_env, uid=user.uid, hooks=hooks.security
            )
            if action is agent.Action.LEFT:
                mutation.warnings.append(_agent_warning(name, _LEFT_DETAIL))
        grants = _plan_grants(env, record, user, hooks)
        result, skips = _attach(env, podman, record, cid, info.pid, user, hooks, grants)
    except GinnError as err:
        _back_to_stopped(env, podman, name, cid, err)
        _leave_agent_quietly(env, name, err)
        raise
    mutation.step("attached")
    running = _save(
        env,
        store,
        record,
        state=RealmState.RUNNING,
        previous_state=None,
        grants=list(result.records),
        share_skipped=skips,
        restart_required=False,
    )
    mutation.step("running")
    return running


def _attach(
    env: OpEnv,
    podman: Podman,
    record: RealmRecord,
    cid: str,
    pid: int,
    user: HostUser,
    hooks: LifecycleHooks,
    grants: _Grants,
) -> tuple[AttachResult, list[str]]:
    """Attach the planned grants of this start. Return the result and its share skips."""
    result = attach_at_start(
        podman,
        cid,
        pid=pid,
        grants=records_from_plan(grants.plan, grants.session),
        session=grants.session,
        realm_home=env.paths.homes / record.name.value,
        host_home=user.host_home,
        admit=admission(
            home=user.host_home,
            owned_paths=grants.owned,
            runtime_root=env.paths.runtime,
            env=user.host_env,
        ),
        helper=hooks.helper,
        mountinfo=hooks.mountinfo,
    )
    return result, share_skipped(grants.plan, grants.session, result)


def _back_to_stopped(env: OpEnv, podman: Podman, name: RealmName, cid: str, err: GinnError) -> None:
    """After a failed start: stop the container again, then record the realm as stopped (ADR-008).

    The record is written stopped only when the stop succeeds, or when podman lists the container
    absent, created or exited. Otherwise the state stays as found. A failure to write the record
    is a note on `err`, so the attach error (GINN-E-ATTACH-FAILED) is the error the caller sees.
    """
    try:
        podman.stop(cid, ROLLBACK_STOP_TIMEOUT_S)
    except GinnError as stop_err:
        err.add_note(f"stop after failed start: {stop_err.code}")
        if not _confirmed_not_running(podman, name, cid):
            return
    try:
        store = RealmStore(env.paths, env.clock)
        if _load(store, name).state is not RealmState.STOPPED:
            store.set_state(name, RealmState.STOPPED)
    except GinnError as write_err:
        err.add_note(f"record not set stopped: {write_err.code}")
    except OSError as write_err:
        err.add_note(f"record not set stopped: {write_err}")


def stop_realm(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    hooks: LifecycleHooks | None = None,
    warnings: list[GinnWarning] | None = None,
) -> RealmRecord:
    """Stop realm `name` and return its record. A stopped record stays stopped (FR-016).

    A stopped record whose container podman runs is stopped with `podman stop`, and its record is
    kept as it is. A stopped record whose container is not running writes nothing. The FR-006
    warnings of a stop are appended to `warnings` when it is given, once the operation has finished.
    """
    hooks = LifecycleHooks() if hooks is None else hooks
    current = _load(RealmStore(env.paths, env.clock), name)
    _refuse_unsupported(current, "stop")
    if current.state is RealmState.STOPPED and not _running(podman, name, current.container_id):
        return current
    params = {"generation": current.generation}
    with mutating(env, "stop", name, params) as mutation:
        record = _stop(env, podman, name, hooks, mutation)
    if warnings is not None:
        warnings.extend(mutation.warnings)
    return record


def _stop(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    hooks: LifecycleHooks,
    mutation: Mutation,
) -> RealmRecord:
    """The body of a stop. It runs under the realm lock, after the journal entry is written."""
    store = RealmStore(env.paths, env.clock)
    record = _load(store, name)
    cid = record.container_id
    if record.state is RealmState.STOPPED:
        if cid is not None:
            podman.stop(cid, hooks.stop_timeout_s)
            mutation.step("stopped")
        return record
    if record.state not in _STOP_FROM:
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state=record.state.value,
            action="stop",
            allowed="running or failed",
        )
    if cid is None:
        raise _no_container(name, record.state)
    try:
        podman.stop(cid, hooks.stop_timeout_s)
    except GinnError:
        # A running or failed realm whose container podman lists absent, created or exited is
        # recorded stopped (DR-020). Any other failure leaves the record as found.
        if not _confirmed_not_running(podman, name, cid):
            raise
    mutation.step("stopped")
    stopped = store.set_state(name, RealmState.STOPPED)
    _release_after_stop(env, name, mutation)
    return stopped


def _probe(
    podman: Podman, label_filter: Mapping[str, str] | None
) -> dict[str, ContainerInfo] | None:
    """The ginn containers that podman lists, by id. None when podman does not answer."""
    try:
        infos = podman.ps(label_filter)
    except GinnError:
        return None
    return {info.id: info for info in infos if LABEL_REALM in info.labels}


def _probed(record: RealmRecord) -> bool:
    return record.container_id is not None and record.state in _PROBED


def _container_state(
    record: RealmRecord, probed: Mapping[str, ContainerInfo] | None
) -> tuple[str | None, ContainerInfo | None]:
    """podman's view of the record's container, and its info when podman lists it."""
    cid = record.container_id
    if cid is None or not _probed(record):
        return None, None
    if probed is None:
        return _UNAVAILABLE, None
    info = probed.get(cid)
    if info is None:
        return _ABSENT, None
    return info.state.value, info


def _mismatch(state: RealmState, container_state: str | None) -> bool | None:
    """True when podman's view disagrees with the record. Null when it cannot be compared.

    Null for a container that is not probed, for a podman query that failed, and for podman's own
    "unknown" state: ginn cannot say whether such a container runs. Otherwise podman runs the
    container in every state except absent, created and exited (_HALTED), so a paused or stopping
    container counts as running, as in _running and _confirmed_not_running.
    """
    if container_state is None or container_state in (_UNAVAILABLE, _PODMAN_UNKNOWN):
        return None
    expected_running = state is RealmState.RUNNING
    podman_runs = container_state != _ABSENT and container_state not in _HALTED
    return podman_runs != expected_running


def _disk(
    env: OpEnv, record: RealmRecord, info: ContainerInfo | None, hooks: LifecycleHooks
) -> DiskUsage:
    """The Realm Home's allocated bytes and the container's size, when podman reports one."""
    home = env.paths.homes / record.name.value
    if os.path.lexists(home):
        home_bytes, partial = hooks.tree_usage(home)
    else:
        home_bytes, partial = 0, False
    container_bytes = None if info is None else info.size_rw
    return DiskUsage(home_bytes, partial, container_bytes)


def _describe(
    env: OpEnv,
    record: RealmRecord,
    probed: Mapping[str, ContainerInfo] | None,
    hooks: LifecycleHooks,
    *,
    disk: bool,
) -> RealmStatus:
    """The status of one readable record. Nothing is written (FR-020)."""
    container_state, info = _container_state(record, probed)
    return RealmStatus(
        name=record.name.value,
        state=record.state.value,
        container_state=container_state,
        state_mismatch=_mismatch(record.state, container_state),
        distro=record.settings.distro.value,
        tier=record.settings.tier.value,
        services=record.settings.services,
        generation=record.generation,
        image_digest=record.image_digest,
        created_at=utc_iso(record.created_at),
        updated_at=utc_iso(record.updated_at),
        restart_required=record.restart_required,
        share_skipped=list(record.share_skipped),
        disk=_disk(env, record, info, hooks) if disk else None,
    )


def _corrupt(name: RealmName, err: GinnError) -> RealmStatus:
    """The entry for a record that cannot be read. Only the name and the reason are known."""
    reason = err.details.get("reason")
    error = err.code if reason is None else f"{err.code}: {reason}"
    return RealmStatus(
        name=name.value,
        state=None,
        container_state=None,
        state_mismatch=None,
        distro=None,
        tier=None,
        services=None,
        generation=None,
        image_digest=None,
        created_at=None,
        updated_at=None,
        restart_required=None,
        share_skipped=None,
        disk=None,
        error=error,
    )


def realm_status(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    hooks: LifecycleHooks | None = None,
    disk: bool = True,
) -> RealmStatus:
    """The status of realm `name`. Read-only: no lock, no journal, no audit (FR-020).

    A missing realm raises GINN-E-NO-SUCH-REALM. A corrupt or unreadable record raises
    GINN-E-INTERNAL; `list_realms` reports it as an entry instead.
    """
    hooks = LifecycleHooks() if hooks is None else hooks
    record = _load(RealmStore(env.paths, env.clock), name)
    probed = _probe(podman, {LABEL_REALM: name.value}) if _probed(record) else None
    return _describe(env, record, probed, hooks, disk=disk)


def list_realms(
    env: OpEnv,
    podman: Podman,
    *,
    hooks: LifecycleHooks | None = None,
    disk: bool = True,
) -> list[RealmStatus]:
    """The status of every realm, sorted by name. Read-only (FR-020).

    A record that cannot be read is listed with its error, and the rest of the list is still shown.
    A record removed after the listing is left out.
    """
    hooks = LifecycleHooks() if hooks is None else hooks
    store = RealmStore(env.paths, env.clock)
    names = sorted(store.list_names(), key=lambda realm: realm.value)
    probed = _probe(podman, None)
    statuses: list[RealmStatus] = []
    for name in names:
        try:
            record = _load(store, name)
        except GinnError as err:
            if err.code == "GINN-E-NO-SUCH-REALM":
                continue
            statuses.append(_corrupt(name, err))
            continue
        statuses.append(_describe(env, record, probed, hooks, disk=disk))
    return statuses


__all__ = [
    "STOP_TIMEOUT_S",
    "DiskUsage",
    "HostUser",
    "LifecycleHooks",
    "RealmStatus",
    "list_realms",
    "realm_status",
    "render_status_text",
    "start_realm",
    "stop_realm",
]
