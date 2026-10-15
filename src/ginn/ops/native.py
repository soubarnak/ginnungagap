# SPDX-License-Identifier: Apache-2.0
"""Record the packages that a `--root` session installs (FR-042 AC-1, DR-007).

`ginn exec --root arch -- pacman -S htop` installs htop with the realm's own manager, and the
Install Manifest must hold it (FR-042 AC-1). The explicit-package diff finds it. `list-explicit`
runs before the session and again after it, and each name that is new becomes a `native` entry
(packages/manifest.py). The diff runs only for a `--root` session, through the `root_session` hook
of runtime/enter.py, so the non-root path makes no extra exec (NFR-004).

The reads take no lock. A shell can run for a long time, and it is not a mutating operation
(FR-123). Only the write takes the realm lock, through `mutating("record-native")`, so it is
audited and journaled (DR-032, FR-124). A diff with no new name writes nothing, and takes no lock.

The exec status is the command's own status (IR-011), so nothing here raises after the session has
run. A failed read, a name that is not a valid package name, or a failed write becomes a
GINN-W-MANIFEST-UNRECORDED warning through `notify`, and the status comes back unchanged.
"""

from collections.abc import Callable
from dataclasses import replace
from typing import Final

from ginn.distros import DistroDef
from ginn.distros import load as load_distro
from ginn.domain import DistroId, PackageName, RealmName
from ginn.errors import GinnError, warn
from ginn.errors import Warning as GinnWarning
from ginn.ops._mutating import OpEnv, mutating
from ginn.packages.manager import explicit_packages, printable, shown
from ginn.packages.manifest import recorded_native
from ginn.podman.adapter import ExecTarget, Podman
from ginn.runtime.enter import RootSession
from ginn.store._time import utc_from_epoch
from ginn.store.records import RealmRecord, RealmStore

_DETAIL_LIMIT: Final = 400
"""Longest detail a warning shows, after escaping."""
OP_RECORD_NATIVE: Final = "record-native"
"""The audit operation name of the write (FR-124)."""
UNRECORDED_CODE: Final = "GINN-W-MANIFEST-UNRECORDED"
"""The warning of a session whose package changes did not reach the Install Manifest."""


def _describe(exc: Exception) -> str:
    """`CODE: message (reason)` for a catalogue error, else the exception type and its text.

    Each part is escaped where it is made: the reason of a catalogue error was escaped by
    packages/manager.py, and any other text is escaped here (NFR-040).
    """
    if isinstance(exc, GinnError):
        reason = exc.details.get("detail")
        suffix = f" ({reason})" if isinstance(reason, str) else ""
        return f"{exc.code}: {exc.message}{suffix}"
    return printable(f"{type(exc).__name__}: {exc}", _DETAIL_LIMIT)


def _unrecorded(realm: str, detail: str) -> GinnWarning:
    """The warning. Its detail is built from escaped parts, so it is not escaped again."""
    return warn(UNRECORDED_CODE, realm=realm, detail=detail)


def _listed(
    podman: Podman, cid: str, definition: DistroDef, realm: str, target: ExecTarget
) -> tuple[frozenset[str] | None, str]:
    """The explicit packages of the container, or None with the reason it could not be read."""
    try:
        return explicit_packages(podman, cid, definition, realm=realm, target=target), ""
    except Exception as exc:  # noqa: BLE001 - reported as a warning, so the exec status still reaches the caller
        return None, _describe(exc)


def _split(names: list[str], distro: DistroId) -> tuple[list[str], list[str]]:
    """The names the manifest can hold (DR-003), and the names it cannot."""
    valid: list[str] = []
    rejected: list[str] = []
    for raw in names:
        try:
            PackageName(raw, distro)
        except GinnError:
            rejected.append(raw)
        else:
            valid.append(raw)
    return valid, rejected


def _record(env: OpEnv, name: RealmName, names: list[str]) -> None:
    """Add a `native` entry for each name without one, under the realm lock (FR-042 AC-1)."""
    store = RealmStore(env.paths, env.clock)
    with mutating(env, OP_RECORD_NATIVE, name, {"packages": names}) as mutation:
        current = store.load(name)
        updated = replace(
            current,
            manifest=recorded_native(current.manifest, names),
            updated_at=utc_from_epoch(env.clock()),
        )
        store.save(updated)
        mutation.step("recorded")


def _recorded_session(
    env: OpEnv,
    podman: Podman,
    record: RealmRecord,
    cid: str,
    run: Callable[[], int],
    notify: Callable[[GinnWarning], None],
) -> int:
    """Diff the explicit packages around `run`, record the new ones, and return the status."""
    realm = record.name.value
    definition = load_distro(record.settings.distro.value)
    target = ExecTarget(realm=realm, services=bool(record.services_at_create), mutating=False)
    before, reason = _listed(podman, cid, definition, realm, target)
    status = run()
    if before is None:
        notify(_unrecorded(realm, f"the packages before the session are unknown: {reason}"))
        return status
    after, reason = _listed(podman, cid, definition, realm, target)
    if after is None:
        notify(_unrecorded(realm, f"the packages after the session are unknown: {reason}"))
        return status
    added = sorted(after - before)
    if not added:
        return status
    names, rejected = _split(added, record.settings.distro)
    if rejected:
        notify(_unrecorded(realm, f"not valid package names: {shown(rejected)}"))
    if names:
        try:
            _record(env, record.name, names)
        except Exception as exc:  # noqa: BLE001 - reported as a warning, so the exec status still reaches the caller
            notify(_unrecorded(realm, _describe(exc)))
    return status


def root_session_recorder(
    env: OpEnv,
    podman: Podman,
    *,
    notify: Callable[[GinnWarning], None],
) -> RootSession:
    """The `root_session` hook of a `--root` enter (EnterHooks, FR-042 AC-1).

    `notify` receives each warning as it happens, so the caller shows it before the next output.
    """

    def session(record: RealmRecord, cid: str, run: Callable[[], int]) -> int:
        return _recorded_session(env, podman, record, cid, run, notify)

    return session


__all__ = ["OP_RECORD_NATIVE", "UNRECORDED_CODE", "root_session_recorder"]
