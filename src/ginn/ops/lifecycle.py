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

Services-on realms (ADR-003, FR-081). A realm whose settings are services-on is started and stopped
through its `sv` service, never by podman from this process. `start_realm` runs `sv up` and waits,
without the realm lock, for the supervised start (`ginn _service-run`, which calls it with
`supervised=True`) to record `running`. `stop_realm` runs `sv down` and waits for the supervised
process to record `stopped`, and it stops the container itself only when the record is not
`stopped` after that (a realm given up after crashes is `failed`, DR-020). The supervised start
takes the realm lock and journals the start and its audit entry, so this process writes nothing
while it waits (DR-032). The supervised start runs under runit's sparse environment, so it attaches
no session grant; enter attaches them (ADR-008 amendment).

Which path a realm takes is decided by its container, not by the desired settings.
`services_at_create` is the services mode the container was made with, and it is the one that picks
`sv` or podman for a start and a stop, and whether the idle watcher is armed. A create-time change
(a tier, a permission or a services mode that needs a new container, ADR-006) is recorded in the
settings first, and the container still has the old options. So a stop of a running realm uses its
container's path. A start of a stopped realm whose record differs from its container
(`stale_container`) first rebuilds the realm (ops/rebuild.py, `rebuild_for_start`, FR-066,
FR-083), and then starts it on the new generation. A supervised start never rebuilds: runit runs
it, and the rebuild stops and starts through runit. It starts the container the record names,
or, while a rebuild is in progress, the container that the rebuild is building (ops/service.py). A
realm that is not stopped and has such a difference is refused with GINN-E-REALM-STATE (exit 4),
and `ginn stop` comes first. A state that DR-020 does not allow for the verb raises
GINN-E-REALM-STATE (exit 4). An unknown name raises GINN-E-NO-SUCH-REALM before any write (FR-025).
A container that podman does not list is not running. A podman error while listing or inspecting a
container is not read as "not running": it propagates, and nothing is written.

Idempotence. Start of a realm that podman runs already returns the record and writes nothing. It
keeps `restart_required` as stored, so the caller can report a pending restart (FR-066; M6 sets
the flag). Stop of a stopped record asks podman: when the container runs, `podman stop` runs and
the record stays stopped; otherwise nothing is written. Neither takes a journal or audit entry
when it writes nothing.

Crash. A record that says `running` while podman does not run its container is a crash
(DR-020, running to failed). Start then goes failed to running. A stale start of a services-off
realm settles such a record as a crash before the rebuild, since a rebuild refuses a running
record (`_settle_crash`). Stop takes it to stopped. A
running or failed record that names no container id is refused by start and stop with
GINN-E-INTERNAL, before any state write. Its remedy is `ginn delete`, which finds the container by
its labels (T4.7).

Warnings. The FR-006 warning of a start or a stop is known only after the body has run, so it
is appended to the `warnings` list of the call, once the operation has finished. A call that
writes nothing has none. A failed operation raises, and its warning goes into the error's notes.

Status and list. `RealmStatus.to_json()` gives these keys, in this order, and a golden test pins
them: name, state, container_state, state_mismatch, state_reason, supervision, distro, tier,
services, autostart, generation, image_digest, created_at, updated_at, restart_required,
share_skipped, disk,
exported_apps, error. supervision is the runit state of a services-on realm (`sv status`, FR-081
AC-1), `unavailable` with its error code when runit does not answer, and None for services off.
- state: the record's state, verbatim (DR-020), except that a running services-off record whose
  container its idle watcher stopped reads `stopped` with state_reason `idle` (FR-018). That is a
  read; the record is not written. podman's view is never written into the record.
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
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ginn.domain import RealmName, RealmState, transition
from ginn.domain.permissions import effective_toggles
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.export.sync import SyncHooks, sync_realm
from ginn.export.sync import _scan_host as _host_entries
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops.security_ops import (
    leave_agent,
    nvidia_plan,
    reconcile_proxy,
    sync_agent,
    write_nvidia,
)
from ginn.podman.adapter import ExecTarget, Podman
from ginn.podman.models import ContainerInfo, ContainerState, ExecResult
from ginn.runtime.attach import (
    MOUNTINFO,
    AttachResult,
    admission,
    attach_at_start,
    records_from_plan,
    share_skipped,
)
from ginn.runtime.env import SessionEnv, session_env
from ginn.runtime.idle import IdleHooks, idle_stopped
from ginn.runtime.idle import arm as arm_idle_watcher
from ginn.runtime.mounts import AttachPlan, Exists, attach_plan, runtime_dir
from ginn.runtime.realm_security import (
    SecurityHooks,
    agent_enabled,
    session_extras,
    stored_gpg_extra,
)
from ginn.runtime.spec import LABEL_REALM
from ginn.security import agent
from ginn.security.caps import option_keys
from ginn.services import autostart, supervisor
from ginn.services.exec_relay import relay_failure, sync_exec_service
from ginn.services.supervisor import SupervisorHooks
from ginn.store._time import utc_from_epoch, utc_iso
from ginn.store.locks import realm_lock
from ginn.store.records import RealmRecord, RealmStore

if TYPE_CHECKING:
    from ginn.ops.rebuild import RebuildHooks

STOP_TIMEOUT_S: Final = 30
"""Seconds `podman stop` waits before it kills the realm (FR-016, AS-023)."""
START_WAIT_S: Final = 60.0
"""Seconds a services-on `start` waits for its supervised start to record `running` (FR-081)."""
START_POLL_S: Final = 0.5
"""Seconds between two reads of the record while a services-on start is awaited."""
RUN_USER_WAIT_S: Final = 10.0
"""Seconds a services-on start waits for `/run/user/<uid>` to belong to the realm user (T7.28).

The owner comes from the tmpfiles.d drop-in, which systemd-tmpfiles-setup applies at boot. That can
run after `podman start` returns, and the attach mkdirs under the directory as the realm user."""
RUN_USER_POLL_S: Final = 0.2
"""Seconds between two reads of the runtime dir's owner while that wait runs (T7.28)."""
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
_SUPERVISED_START_FROM: Final = _START_FROM | frozenset({RealmState.RUNNING})
"""The states a services-on start goes through sv from. RUNNING is one whose container died: the
supervised start records that as a crash (DR-020), and the running ones return before this."""
_SUPERVISED_STOP_FROM: Final = _STOP_FROM | frozenset({RealmState.STOPPED})
"""The states a services-on stop goes through sv from. STOPPED is idempotent (sv down is a
no-op)."""
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
    and returns its bytes and whether the walk was partial (FR-020). `supervisor` is the sv control
    of a services-on realm, and `start_wait_s` and `start_poll_s` bound the wait for its start.
    `run_user_wait_s` and `run_user_poll_s` bound the wait for the runtime dir's owner (T7.28).
    """

    exists: Exists = os.path.exists
    mountinfo: Path = MOUNTINFO
    supervisor: SupervisorHooks = field(default_factory=SupervisorHooks)
    start_wait_s: float = START_WAIT_S
    start_poll_s: float = START_POLL_S
    run_user_wait_s: float = RUN_USER_WAIT_S
    run_user_poll_s: float = RUN_USER_POLL_S
    helper: str | None = None
    stop_timeout_s: int = STOP_TIMEOUT_S
    tree_usage: Callable[[Path], tuple[int, bool]] = _tree_usage
    security: SecurityHooks = field(default_factory=SecurityHooks)
    """The seams of the security helpers: the filter, the proxy, gpg and the NVIDIA probe."""
    before_start: Callable[[], None] | None = None
    """Runs when the start is about to call `podman start`, not when it returns early for a
    container that runs. A services-on start removes the stale conmon pidfile here (FR-081)."""
    idle: IdleHooks = field(default_factory=IdleHooks)
    """The idle watcher of a services-off realm: the helper, its grace and how it is started
    (FR-018, runtime/idle.py)."""
    rebuild: "RebuildHooks | None" = None
    """The seams of the rebuild that a start runs when its record needs a new container (FR-066).
    None is the rebuild's own defaults."""


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
    state_reason: str | None
    supervision: str | None
    distro: str | None
    tier: str | None
    services: bool | None
    autostart: bool | None
    generation: int | None
    image_digest: str | None
    created_at: str | None
    updated_at: str | None
    restart_required: bool | None
    share_skipped: list[str] | None
    disk: DiskUsage | None
    exported_apps: int | None = None
    error: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state,
            "container_state": self.container_state,
            "state_mismatch": self.state_mismatch,
            "state_reason": self.state_reason,
            "supervision": self.supervision,
            "distro": self.distro,
            "tier": self.tier,
            "services": self.services,
            "autostart": self.autostart,
            "generation": self.generation,
            "image_digest": self.image_digest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "restart_required": self.restart_required,
            "share_skipped": None if self.share_skipped is None else list(self.share_skipped),
            "disk": None if self.disk is None else self.disk.to_json(),
            "exported_apps": self.exported_apps,
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


def pending_rebuild_notice(record: RealmRecord, reason: str) -> GinnWarning | None:
    """GINN-W-REBUILD-PENDING for a change that the next start rebuilds, or None (FR-066, CL-19).

    `reason` names the change in words, such as "the tier changed". Nothing is written here.
    """
    if not stale_container(record):
        return None
    return warn("GINN-W-REBUILD-PENDING", realm=record.name.value, change=reason)


def _options_stale(record: RealmRecord) -> bool:
    """True when a create-time option, or a GPU or fonts grant, differs from the container's.

    The options are compared the way `created_options` names them (security/caps.py `option_keys`).
    """
    toggles = effective_toggles(record.settings.tier, record.permissions.toggles)
    wanted = option_keys(toggles, kvm_present=record.kvm_at_create)
    return wanted != record.created_options or not record.create_grants_match(toggles)


def stale_container(record: RealmRecord) -> bool:
    """True when the record asks for a create-time state that its container was not made with.

    A create-time option (a device, a security option or a group, ADR-006), a GPU or fonts grant and
    the services mode are fixed at `podman create`. A tier, permission or services change that
    needs them is recorded in the settings at once, and it is applied by a rebuild at the next
    start (FR-066, FR-083). This is the test for that rebuild.
    """
    return _options_stale(record) or record.settings.services != record.services_at_create


def stale_services_only(record: RealmRecord) -> bool:
    """True when `stale_container` is true only through the services mode (ADR-015 Decision 7).

    Such a change keeps the tier, the permissions and the create-time options. A realm with a
    derived image is then rebuilt from that image (`reuse`), and it is not provisioned again.
    """
    return record.settings.services != record.services_at_create and not _options_stale(record)


def check_derived_image(podman: Podman, name: RealmName, cid: str, expected: str | None) -> None:
    """Refuse to start a container that was not made from the derived image its record names.

    `expected` is the record's `init_image_id`, or the `building_init_image_id` of a rebuild that
    starts its container. None means that the realm has no derived image, so nothing is checked
    (ADR-015 Decision 9). Podman fixes a container's image at create, so the container's `image_id`
    is the image it runs from, and it must be the recorded ID. A mismatch is GINN-E-IMAGE-DERIVED,
    and the container is left stopped. Nothing is written here.
    """
    if expected is None:
        return
    actual = podman.inspect(cid).image_id
    if actual != expected:
        err = make(
            "GINN-E-IMAGE-DERIVED",
            realm=name.value,
            detail=f"the container runs image {actual or 'none'}, not the recorded {expected}",
        )
        err.remedy = (
            f"run 'ginn rebuild {name.value}', which provisions the realm's derived image again "
            "from its current pin"
        )
        raise err


def _rebuild_stale(
    env: OpEnv,
    podman: Podman,
    record: RealmRecord,
    user: HostUser,
    hooks: LifecycleHooks,
    warnings: list[GinnWarning],
) -> RealmRecord:
    """Rebuild a stopped realm whose container is stale, and return its record (FR-066, FR-083).

    The rebuild is ops/rebuild.py, which imports this module, so it is imported here, when a start
    needs it. A running realm keeps its container until its next stop, so it is refused before any
    write. A services-off realm whose container is gone is settled as failed first
    (`_settle_crash`), so it reads as a failed one here. A failed one is stopped first, which
    records it stopped, and then rebuilt.

    A rebuild that fails leaves the old root (NFR-031), and the start goes on with that root, so the
    realm is not stranded by a pending change. The failure is GINN-W-REBUILD-DEFERRED, and the
    change stays pending (`restart_required`, FR-021 AC-2). Only the cancel (GINN-E-INTERRUPTED)
    stops the start, so a Ctrl-C never starts the old root by surprise.
    """
    if record.state is RealmState.RUNNING:
        raise make(
            "GINN-E-REALM-STATE",
            realm=record.name.value,
            state=record.state.value,
            action="start",
            allowed="stopped or failed",
        )
    store = RealmStore(env.paths, env.clock)
    if record.state is RealmState.FAILED:
        stop_realm(env, podman, record.name, hooks=hooks, warnings=warnings)
        record = _load(store, record.name)
    from ginn.ops.rebuild import RebuildHooks, rebuild_for_start

    rebuild_hooks = RebuildHooks() if hooks.rebuild is None else hooks.rebuild
    try:
        return rebuild_for_start(
            env,
            podman,
            record.name,
            session=user,
            hooks=rebuild_hooks,
            warnings=warnings,
            services_only=stale_services_only(record),
        )
    except GinnError as err:
        if err.code == "GINN-E-INTERRUPTED":
            raise
        warnings.append(
            warn("GINN-W-REBUILD-DEFERRED", realm=record.name.value, reason=err.message)
        )
        return _load(store, record.name)


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


def refuse_before_supervisor(
    env: OpEnv,
    name: RealmName,
    allowed: frozenset[RealmState],
    action: str,
    *,
    shown_text: str | None = None,
) -> None:
    """Refuse a services-on start, stop or delete before any `sv` call (FR-081, DR-032).

    `sv` starts or stops the container, so a refusal that comes after the sv call would leave the
    record and the container apart: a stop of an `updating` realm would kill the upgrade, and a
    refused journal entry would leave a record `running` with its container stopped. So the checks
    that `mutating()` makes at its entry are made here first: the state (DR-020), the host check
    (FR-006, `before_op`, which refuses a change that is not acknowledged), a journal entry of an
    unrecovered operation (FR-122), and the realm lock, which is taken and released at once. The
    host
    check and the lock are not held over the sv call. Each refusal is the one the locked body
    gives. The locked checks still run after the sv call, because the state can change in between.
    """
    record = _load(RealmStore(env.paths, env.clock), name)
    if record.state not in allowed:
        listed = shown_text or ", ".join(sorted(state.value for state in allowed))
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state=record.state.value,
            action=action,
            allowed=listed,
        )
    env.integrity.before_op(env.audit, env.user)
    for entry in env.journal.pending():
        if entry.realm == name.value:
            err = make("GINN-E-RECOVERY-PENDING", realm=name.value, op=entry.op)
            err.details["pid"] = entry.pid
            raise err
    with realm_lock(env.paths, name, action):
        return


def start_realm(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    session: HostUser | None = None,
    hooks: LifecycleHooks | None = None,
    warnings: list[GinnWarning] | None = None,
    supervised: bool = False,
    rebuild: bool = True,
) -> RealmRecord:
    """Start realm `name` and attach its grants. Return the record that now reads running.

    A realm that podman runs already comes back as stored, with its `restart_required` flag. See
    the module docstring for the states it accepts, the refusals and the journal steps. The FR-006
    warnings of the start (a host change found after the start) are appended to `warnings` when it
    is given, after the operation has finished.

    `supervised` is True only for the start that the realm's runit service makes (`ginn
    _service-run`, FR-081). Only such a start runs a services-on realm; a services-off realm is
    refused, because its runit service does not exist. Any other start of a services-on realm goes
    through its runit service (`start_through_sv`), and returns once the supervised start has
    recorded `running`. The FR-006 warnings of that start are written to the runit log, not here.

    `rebuild` is False only for the restart that a rebuild makes itself: that start runs the root
    the rebuild left, and never retries a lazy rebuild (ops/rebuild.py). A start that a lazy rebuild
    changed syncs the realm's menu once it has started, so a package the rebuild dropped leaves the
    menu (FR-044, FR-021 AC-1).
    """
    hooks = LifecycleHooks() if hooks is None else hooks
    current = _load(RealmStore(env.paths, env.clock), name)
    if supervised and not current.services_at_create:
        raise _internal("the supervisor starts only a services-on realm", realm=name.value)
    user = _host_user(env) if session is None else session
    if not user.host_home.is_absolute():
        raise ValueError(f"host home must be absolute, not {user.host_home}")
    if current.state is RealmState.RUNNING and _running(podman, name, current.container_id):
        if current.services_at_create:
            # A running realm needs no start, but its relay is synced, so a relay that is down comes
            # back (ADR-014, NFR-001). A failure is a warning, and the start still succeeds.
            early: list[GinnWarning] = []
            _sync_relay_noted(env, podman, early)
            if warnings is not None:
                warnings.extend(early)
        return current
    if current.state is RealmState.RUNNING and not current.services_at_create:
        _settle_idle_stop(env, podman, name)
        current = _load(RealmStore(env.paths, env.clock), name)
    notices: list[GinnWarning] = []
    rebuilt = False
    if rebuild and not supervised and stale_container(current):
        if current.state is RealmState.RUNNING and not current.services_at_create:
            # A container that is gone reads failed, as `_start` settles it (DR-020).
            _settle_crash(env, podman, name)
            current = _load(RealmStore(env.paths, env.clock), name)
        generation = current.generation
        current = _rebuild_stale(env, podman, current, user, hooks, notices)
        rebuilt = current.generation != generation
    if current.container_id is not None:
        # Before the sv call, and before any journal entry, so a container that is not the recorded
        # derived image is refused with nothing written. A services-on start would otherwise
        # show the mismatch as a supervisor restart loop (ADR-015 Decision 9).
        check_derived_image(podman, name, current.container_id, current.init_image_id)
    if current.services_at_create and not supervised:
        refuse_before_supervisor(
            env, name, _SUPERVISED_START_FROM, "start", shown_text="stopped or failed"
        )
        started = start_through_sv(env, podman, name, hooks)
        # The realm reads running now. The relay comes up before the sync below, which execs from
        # the session, so it is up before any exec the start needs (ADR-014, NFR-001).
        _sync_relay_noted(env, podman, notices)
        if rebuilt:
            notices.extend(_sync_rebuilt(env, podman, name, hooks))
        if warnings is not None:
            warnings.extend(notices)
        return started
    params = {"generation": current.generation}
    with mutating(env, "start", name, params) as mutation:
        if supervised:
            _follow_autostart(env, name)
        record = _start(env, podman, name, user, hooks, mutation)
        if not record.services_at_create:
            _arm_idle(env, podman, record, hooks, mutation)
    if record.services_at_create:
        # The supervised start has recorded running; its attach ran in the leaf (direct route).
        _sync_relay_noted(env, podman, notices)
    notices.extend(mutation.warnings)
    if rebuilt:
        notices.extend(_sync_rebuilt(env, podman, name, hooks))
    if warnings is not None:
        warnings.extend(notices)
    return record


def start_through_sv(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    hooks: LifecycleHooks,
    *,
    container_id: str | None = None,
) -> RealmRecord:
    """Start a services-on realm through its runit service and wait until it runs (FR-081).

    `sv up` starts run, and run execs `ginn _service-run`, which takes the realm lock and journals
    the start (DR-032). This process holds no lock while it waits, because the supervised start
    needs the lock to record `running`. The record is read without the lock. The wait gives up
    when runsv has stopped the service (after five crashes in 60 s, AS-028) or when the time is up.

    `container_id` is None for the realm's own start: the wait ends when the record reads running
    and its container runs. A rebuild (ops/rebuild.py) passes the container it is building. Its
    record still reads rebuilding, and `_service-run` starts that container without writing, so the
    wait ends when that container runs, and nothing is recorded here.
    """
    clock = hooks.supervisor.clock
    supervisor.up(env.paths, name, hooks=hooks.supervisor)
    started = clock()
    deadline = started + hooks.start_wait_s
    store = RealmStore(env.paths, env.clock)
    while True:
        record = _load(store, name)
        if container_id is None:
            ready = record.state is RealmState.RUNNING and _running(
                podman, name, record.container_id
            )
        else:
            ready = _running(podman, name, container_id)
        if ready:
            return record
        reason = _runit_stopped(env, name, hooks)
        if reason is not None:
            raise _start_timeout(name, clock() - started, reason)
        if clock() >= deadline:
            raise _start_timeout(name, clock() - started, "the realm does not run yet")
        hooks.supervisor.sleep(hooks.start_poll_s)


def stop_through_sv(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    hooks: LifecycleHooks,
    *,
    container_id: str,
    timeout_s: int,
) -> None:
    """Stop the container of a services-on rebuild through the realm's runit service (ADR-003).

    `sv down` makes runsv send ginn-wait its TERM, ginn-wait stops the container, and the finish
    script returns without writing, because the rebuild holds the record (services/service.py). A
    runsv that is proven gone is passed over, as a stop does. Podman then stops the container if it
    still runs, so no container of the new generation outlives this call.
    """
    supervisor.stop_if_supervised(env.paths, name, hooks=hooks.supervisor)
    if _running(podman, name, container_id):
        podman.stop(container_id, timeout_s)


def _runit_stopped(env: OpEnv, name: RealmName, hooks: LifecycleHooks) -> str | None:
    """Why runsv has stopped the realm's service during a start, or None while it runs (AS-028).

    A `down` status without `want up` means runsv does not restart the service. The down file tells
    the two causes apart. With `normally up` the down file is absent, so this was a stop request,
    such as a `ginn stop` during the wait. Without it, the finish script wrote the down file, so
    runsv gave the realm up after repeated crashes. A crash restart reads `down` with `want up`,
    which is neither. The record is not read: a `failed` record can be left from an earlier give-up
    until the supervised start writes it. A status that cannot be read is unknown, and the wait goes
    on. A runsv proven gone (GINN-E-SUPERVISOR-DOWN) propagates.
    """
    try:
        status = supervisor.status(env.paths, name, hooks=hooks.supervisor)
    except GinnError as err:
        if err.code == "GINN-E-SUPERVISOR-DOWN":
            raise
        return None
    if status.state != supervisor.DOWN or status.want_up:
        return None
    if status.normally_up:
        return "a stop was requested while the realm started"
    return "runsv stopped the service after repeated crashes (AS-028)"


def _start_timeout(name: RealmName, elapsed: float, detail: str) -> GinnError:
    return make(
        "GINN-E-SERVICE-TIMEOUT",
        realm=name.value,
        action="start",
        seconds=round(elapsed),
        detail=detail,
    )


def _sync_relay_noted(env: OpEnv, podman: Podman, notices: list[GinnWarning]) -> None:
    """Sync the relay after a start or stop that has been recorded (ADR-014).

    The command has already succeeded, so a failed sync does not fail it. The failure is added to
    `notices` as GINN-W-RELAY-SYNC, and the next sync retries it.
    """
    try:
        sync_exec_service(env.paths, podman)
    except Exception as exc:  # noqa: BLE001 - the command has succeeded, so this is a warning
        notices.append(relay_failure(exc))


def _settle_idle_stop(env: OpEnv, podman: Podman, name: RealmName) -> None:
    """Record a services-off realm that its idle watcher stopped, as stopped and not as a crash.

    The check reads only, and it writes only when the idle stop is confirmed: the container does not
    run, and the watcher's log says it stopped that container (runtime/idle.py). The write takes the
    realm lock again and checks once more under it. It is journaled and audited as an "idle-stop"
    operation of its own, so the start or stop that follows is a plain one. The stop releases the
    agent filter and the proxy, as a stop does (FR-067). A podman error here is not a confirmation:
    nothing is written, and the operation that follows reports the error as it always did.
    """
    store = RealmStore(env.paths, env.clock)
    record = _load(store, name)
    cid = record.container_id
    if record.state is not RealmState.RUNNING or cid is None:
        return
    try:
        if _running(podman, name, cid):
            return
    except GinnError:
        return
    if not idle_stopped(env.paths, name, container_id=cid, container_running=False):
        return
    with mutating(env, "idle-stop", name, {"generation": record.generation}) as mutation:
        try:
            current = _load(store, name)
            confirmed = (
                current.state is RealmState.RUNNING
                and current.container_id == cid
                and not _running(podman, name, cid)
                and idle_stopped(env.paths, name, container_id=cid, container_running=False)
            )
        except GinnError:
            confirmed = False
        if confirmed:
            store.set_state(name, RealmState.STOPPED)
            mutation.step("stopped")
            _release_after_stop(env, name, mutation)


def _settle_crash(env: OpEnv, podman: Podman, name: RealmName) -> None:
    """Record a services-off realm whose container is gone as failed (DR-020, running to failed).

    `_start` settles a crash the same way, under the realm lock. A stale start needs it first: a
    rebuild refuses a record that reads running (GINN-E-REALM-STATE), and the rebuild then starts
    from a stopped realm. The check reads only. The write takes the realm lock and checks again
    under it, as `_settle_idle_stop` does, and it is journaled and audited as a "crash" operation.
    A podman error propagates, and nothing is written.
    """
    store = RealmStore(env.paths, env.clock)
    record = _load(store, name)
    cid = record.container_id
    if record.state is not RealmState.RUNNING or cid is None or _running(podman, name, cid):
        return
    with mutating(env, "crash", name, {"generation": record.generation}) as mutation:
        current = _load(store, name)
        if (
            current.state is RealmState.RUNNING
            and current.container_id == cid
            and not _running(podman, name, cid)
        ):
            store.set_state(name, RealmState.FAILED)
            mutation.step("failed")


def _sync_rebuilt(
    env: OpEnv, podman: Podman, name: RealmName, hooks: LifecycleHooks
) -> list[GinnWarning]:
    """Sync the menu after a lazy rebuild has started the realm on its new generation (FR-044).

    A rebuild replays the Install Manifest, and a package that the upstream no longer offers leaves
    it, so its exported entries would stay until the next sync. The start has finished and no
    lock is held, so the sync takes the realm lock itself. A failed sync is a GINN-W-EXPORT-SYNC
    warning, and the start stands.
    """
    program = None if hooks.rebuild is None else hooks.rebuild.program
    sync_hooks = SyncHooks() if program is None else SyncHooks(program=program)
    try:
        result = sync_realm(env, podman, name, hooks=sync_hooks)
    except GinnError as err:
        return [warn("GINN-W-EXPORT-SYNC", realm=name.value, reason=err.message)]
    return list(result.warnings)


def _idle_stopped_status(env: OpEnv, record: RealmRecord, container_state: str | None) -> bool:
    """True when a running services-off record's container was stopped by its idle watcher (FR-018).

    Read-only: podman's answer and the watcher's files. The record is not written.
    """
    if record.state is not RealmState.RUNNING or record.services_at_create:
        return False
    if record.container_id is None or _mismatch(RealmState.RUNNING, container_state) is not True:
        return False
    return idle_stopped(
        env.paths, record.name, container_id=record.container_id, container_running=False
    )


def _arm_idle(
    env: OpEnv,
    podman: Podman,
    record: RealmRecord,
    hooks: LifecycleHooks,
    mutation: Mutation,
) -> None:
    """Arm the idle watcher of a services-off realm that has just started (FR-018, NFR-002).

    The init pid comes from one `podman inspect`, and the podman binary and its global flags from
    the adapter's `global_argv`. An object without that method (a test fake) arms nothing. Every
    failure is a GINN-W-IDLE-STOP warning: the start has happened, and it stays a success (FR-018).
    The arm runs under the realm lock, which the start holds, so the watcher record has one writer.
    """
    global_argv = getattr(podman, "global_argv", None)
    cid = record.container_id
    if not callable(global_argv) or cid is None:
        return
    name = record.settings.name
    try:
        init_pid = podman.inspect(cid).pid
    except GinnError as err:
        mutation.warnings.append(warn("GINN-W-IDLE-STOP", realm=name.value, detail=_detail(err)))
        return
    notice = arm_idle_watcher(
        env.paths,
        name,
        container_id=cid,
        init_pid=init_pid,
        podman_argv=list(global_argv()),
        hooks=hooks.idle,
    )
    if notice is not None:
        mutation.warnings.append(notice)


def _follow_autostart(env: OpEnv, name: RealmName) -> None:
    """Make the realm's `down` file match its autostart flag, under the realm lock (DR-012).

    A supervised start does this, so a give-up (which writes `down`) is undone by the next start
    that the user asks for, and a flag set while the realm was down takes effect at its next start.
    """
    record = _load(RealmStore(env.paths, env.clock), name)
    autostart.apply(env.paths, name, record.settings.autostart)


_LEFT_STATES: Final = _HALTED | frozenset({ContainerState.STOPPING})
"""The states in which a start's container cannot take the runtime dir's owner (FR-081): the
halted ones, and stopping. Paused and unknown count as running here, so the wait keeps polling."""


class RunUserState(Enum):
    """What one read of `/run/user/<uid>` in a starting container found (T7.28, FR-081)."""

    OWNED = "owned"
    """The directory belongs to the realm user."""
    NOT_YET = "not yet"
    """The directory is not owned yet, or the read failed while the container still runs."""
    GONE = "gone"
    """The container is not running any more, so the directory will not be given to the user."""


def wait_for_run_user(
    read: Callable[[], RunUserState],
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    wait_s: float,
    poll_s: float,
) -> RunUserState:
    """Poll `read` until it answers OWNED or GONE, or `wait_s` seconds pass. Return the last answer.

    The first read comes at once, so a directory that is owned already costs no sleep. Each later
    read follows a sleep of `poll_s`. GONE ends the wait at once: a stopped container does not
    become owned later, so the rest of the bound is not spent (FR-081). A timeout returns NOT_YET,
    and the caller goes on (T7.28): the attach then fails as it did before the wait, and nothing
    retries it.
    """
    deadline = clock() + wait_s
    while True:
        state = read()
        if state is not RunUserState.NOT_YET or clock() >= deadline:
            return state
        sleep(poll_s)


def run_user_state(
    podman: Podman, cid: str, uid: int, *, target: ExecTarget, pid: int
) -> RunUserState:
    """Read whether `/run/user/<uid>` in the container belongs to the realm user (T7.28, T716-1).

    The check is a stat run as container root in the realm, and only the uid is compared: the owner
    the drop-in gives. When stat prints an owner, the answer is OWNED for the realm uid and NOT_YET
    for any other, and podman is asked nothing more. When it prints no owner, the read is unsettled:
    the exec raised, exited non-zero (a missing directory, or podman's 125 or 255 for an exec on a
    stopped container, which the adapter may return rather than raise), or printed what does not
    parse. Only then is `_gone` asked. A killed init fails every later read, so a start that waits
    on one would otherwise spend its whole bound (FR-081). `pid` is the init pid the start read
    after `podman start`.
    """
    try:
        result = podman.exec(
            cid, ["stat", "-c", "%u", "--", runtime_dir(uid)], target=target, user="0:0"
        )
    except GinnError:
        return RunUserState.GONE if _gone(podman, cid, pid) else RunUserState.NOT_YET
    owner = _owner(result)
    if owner is None:
        return RunUserState.GONE if _gone(podman, cid, pid) else RunUserState.NOT_YET
    return RunUserState.OWNED if owner == uid else RunUserState.NOT_YET


def _owner(result: ExecResult) -> int | None:
    """The uid stat printed, or None when it printed none: a non-zero exit, or output that is not a
    number (FR-081)."""
    if result.exit_code != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _gone(podman: Podman, cid: str, pid: int) -> bool:
    """True when the container the start began with has left (FR-081): its state is in
    `_LEFT_STATES`, or podman runs another init pid, so it restarted since the start read it. Read
    only, and asked only after a failed exec. An inspect that fails is GONE only when podman no
    longer lists the container."""
    try:
        info = podman.inspect(cid)
    except GinnError:
        return _absent(podman, cid)
    return info.state in _LEFT_STATES or info.pid != pid


def _absent(podman: Podman, cid: str) -> bool:
    """True when podman lists no container with this id. A failed listing is no answer (FR-081)."""
    try:
        listed = podman.ps()
    except GinnError:
        return False
    return all(info.id != cid for info in listed)


def run_user_pending_note(name: RealmName) -> str:
    """The note on a failed attach whose owner wait timed out (GINN-W-RUN-USER-PENDING, FR-083).

    A failed start or rebuild raises its own error, and its warnings are not printed. The timeout
    is carried on that error instead, as a note, so the cause is not lost (T7.28).
    """
    return (
        f"GINN-W-RUN-USER-PENDING: the runtime directory of realm '{name.value}' was not yet owned "
        "by the realm user when its wait ended"
    )


def run_user_gone_error(podman: Podman, cid: str, name: RealmName, *, detail: str) -> GinnError:
    """The error of a start or rebuild whose container stopped during the runtime-dir wait (FR-081).

    It has the details of the not-running check that runs right after `started`: `detail` names the
    caller, and `state` is podman's state, or "absent" or "unavailable" when the inspect gives none.
    It is raised before the attach, so no attach runs against a dead container, and no pending note
    is due: the wait ended because the container stopped, not because of a timeout.
    """
    return _internal(
        detail,
        realm=name.value,
        state=_observed_state(podman, cid),
        reason="the container stopped while the start waited for its runtime directory",
    )


def _observed_state(podman: Podman, cid: str) -> str:
    """The state podman reports for the container. When the inspect fails, "absent" if podman no
    longer lists it, and "unavailable" otherwise (the status vocabulary)."""
    try:
        return podman.inspect(cid).state.value
    except GinnError:
        return _ABSENT if _absent(podman, cid) else _UNAVAILABLE


def _settle_run_user(
    podman: Podman, cid: str, uid: int, record: RealmRecord, hooks: LifecycleHooks, pid: int
) -> RunUserState:
    """Wait for the realm's runtime dir to belong to the realm user, before the grants attach.

    A services-on container's `/run/user/<uid>` is root-owned until systemd-tmpfiles-setup applies
    the drop-in (T716-1). The wait is bounded by `hooks.run_user_wait_s`, and it ends at once when
    the container stops (GONE). The answer is the last read: OWNED, or NOT_YET at a timeout. The
    start goes on after a timeout, and the attach fails if the directory is still root's (T7.28).
    The caller says which: a warning when the attach succeeds, or a note on its error when it fails.
    A GONE caller raises `run_user_gone_error` instead. `pid` is the init pid the start read.
    """
    target = ExecTarget(
        realm=record.name.value, services=bool(record.services_at_create), mutating=False
    )
    return wait_for_run_user(
        lambda: run_user_state(podman, cid, uid, target=target, pid=pid),
        clock=hooks.supervisor.clock,
        sleep=hooks.supervisor.sleep,
        wait_s=hooks.run_user_wait_s,
        poll_s=hooks.run_user_poll_s,
    )


def _start(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    user: HostUser,
    hooks: LifecycleHooks,
    mutation: Mutation,
) -> RealmRecord:
    """The body of a start. It runs under the realm lock, after the journal entry is written.

    A start that does not rebuild (a runit start, or a start after a failed lazy rebuild) runs the
    old container, so the record keeps `restart_required` while its settings still differ from it.
    """
    store = RealmStore(env.paths, env.clock)
    record = _load(store, name)
    stale = stale_container(record)
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
    # Before any write: a container that is not the recorded derived image is refused (Decision 9).
    check_derived_image(podman, name, cid, record.init_image_id)
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
    run_user_pending = False
    try:
        podman.start(cid)
        info = podman.inspect(cid)
        if info.state is not ContainerState.RUNNING or info.pid < 2:
            raise _internal("the realm is not running after start", state=info.state.value)
        mutation.step("started")
        if record.services_at_create:
            # Before the attach: it mkdirs under /run/user/<uid> as the realm user (T7.28). A
            # container that stopped in the wait fails here, as a crash after start does (FR-081).
            run_user = _settle_run_user(podman, cid, user.uid, record, hooks, info.pid)
            if run_user is RunUserState.GONE:
                raise run_user_gone_error(
                    podman, cid, name, detail="the realm is not running after start"
                )
            run_user_pending = run_user is RunUserState.NOT_YET
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
        if run_user_pending:
            err.add_note(run_user_pending_note(name))
        _back_to_stopped(env, podman, name, cid, err)
        _leave_agent_quietly(env, name, err)
        raise
    mutation.step("attached")
    if run_user_pending:
        mutation.warnings.append(warn("GINN-W-RUN-USER-PENDING", realm=name.value))
    running = _save(
        env,
        store,
        record,
        state=RealmState.RUNNING,
        previous_state=None,
        grants=list(result.records),
        share_skipped=skips,
        restart_required=stale,
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
        target=ExecTarget(
            realm=record.name.value, services=bool(record.services_at_create), mutating=False
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
    A services-on realm is stopped through its runit service (`_stop_supervised`).
    """
    hooks = LifecycleHooks() if hooks is None else hooks
    current = _load(RealmStore(env.paths, env.clock), name)
    if current.services_at_create:
        return _stop_supervised(env, podman, name, hooks, warnings)
    if current.state is RealmState.RUNNING:
        _settle_idle_stop(env, podman, name)
        current = _load(RealmStore(env.paths, env.clock), name)
    if current.state is RealmState.STOPPED and not _running(podman, name, current.container_id):
        return current
    params = {"generation": current.generation}
    with mutating(env, "stop", name, params) as mutation:
        record = _stop(env, podman, name, hooks, mutation)
    if warnings is not None:
        warnings.extend(mutation.warnings)
    return record


def _stop_supervised(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    hooks: LifecycleHooks,
    warnings: list[GinnWarning] | None,
) -> RealmRecord:
    """Stop a services-on realm through its runit service (FR-016, FR-081).

    The stop holds the realm lock across `sv down`. `sv down` makes runsv send ginn-wait its TERM,
    ginn-wait stops the container, and the finish script comes back. That script sees the lock held
    by this stop, and it writes nothing (services/service.py `finish_service`). So this stop records
    `stopped` itself, after sv has returned. A realm that no runsv supervises has no runit entry to
    restart it, so there is nothing to signal. A runsv proven gone (GINN-E-SUPERVISOR-DOWN) is
    passed over, because nothing restarts the realm then. A record that is not `stopped` after the
    stop (a realm given up after crashes is `failed`, and DR-020 has no failed-to-stopped path for
    runit to take) is stopped here, with podman, as a services-off realm is.
    """
    refuse_before_supervisor(
        env, name, _SUPERVISED_STOP_FROM, "stop", shown_text="running or failed"
    )
    params = {"generation": _load(RealmStore(env.paths, env.clock), name).generation}
    with mutating(env, "stop", name, params) as mutation:
        supervisor.stop_if_supervised(env.paths, name, hooks=hooks.supervisor)
        record = _load(RealmStore(env.paths, env.clock), name)
        if record.state is RealmState.STOPPED and not _running(podman, name, record.container_id):
            stopped = record
        else:
            stopped = _stop(env, podman, name, hooks, mutation)
    notices = list(mutation.warnings)
    _sync_relay_noted(env, podman, notices)  # NFR-001: the relay goes down when nothing needs it
    if warnings is not None:
        warnings.extend(notices)
    return stopped


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


def _exported_apps(env: OpEnv, name: RealmName) -> int | None:
    """The number of the realm's exported menu entries on the host (FR-044, DR-008). Read only.

    The host's `ginn-<realm>-<app>.desktop` entries that name the realm are its exports, and the
    count is taken from them. None when the host cannot be read.
    """
    try:
        return len(_host_entries(env.paths, name.value))
    except (OSError, ValueError):
        return None


def _supervision(env: OpEnv, record: RealmRecord, hooks: LifecycleHooks) -> str:
    """The runit state of a services-on realm, read with `sv status` (FR-081 AC-1).

    Read-only, and it never raises: a supervisor that cannot answer is shown as a word with its
    error code, so `ginn status` still reports the record.
    """
    try:
        return supervisor.status(env.paths, record.name, hooks=hooks.supervisor).describe()
    except GinnError as err:
        return f"unavailable ({err.code})"


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
    idle = _idle_stopped_status(env, record, container_state)
    return RealmStatus(
        name=record.name.value,
        state="stopped" if idle else record.state.value,
        container_state=container_state,
        state_mismatch=False if idle else _mismatch(record.state, container_state),
        state_reason="idle" if idle else None,
        supervision=_supervision(env, record, hooks) if record.services_at_create else None,
        distro=record.settings.distro.value,
        tier=record.settings.tier.value,
        services=record.settings.services,
        autostart=record.settings.autostart,
        generation=record.generation,
        image_digest=record.image_digest,
        created_at=utc_iso(record.created_at),
        updated_at=utc_iso(record.updated_at),
        restart_required=record.restart_required or stale_container(record),
        share_skipped=list(record.share_skipped),
        disk=_disk(env, record, info, hooks) if disk else None,
        exported_apps=_exported_apps(env, record.name),
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
        state_reason=None,
        supervision=None,
        distro=None,
        tier=None,
        services=None,
        autostart=None,
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
