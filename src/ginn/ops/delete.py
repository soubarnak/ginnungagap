# SPDX-License-Identifier: Apache-2.0
"""Delete a realm (FR-019, FR-025, AS-014, DR-010, DR-017, DR-020, DR-021).

`delete_realm` is a mutating operation. It takes the image lock of the realm's distribution, as
ops/create.py does, and then runs inside `mutating()`, which takes the realm lock. The body runs
these steps. Each one is a journal step, in this order. The names are stable and listed in STEPS:

- deleting: the record moves to `deleting` (DR-020), unless it already reads `deleting`. A later
  delete finds the record there, and recovery (ops/recovery.py) leaves an interrupted delete there.
- stopped: each container that podman lists for the realm and that is not created or exited is
  stopped. A stop that fails is noted, and the container is removed anyway (see below). Then the
  realm leaves the Agent Proxy filter, and its D-Bus proxy is stopped and its state removed
  (FR-067, FR-019 AC-1). A failure there is a GinnError, so the record stays `deleting` and the next
  delete finishes the release.
- container-removed: each container that podman lists for the realm is removed with force. The
  realm root is the container, so this removes it (FR-019). A container that is already gone is
  not an error.
- service-removed: the realm's runit service directory and its per-realm files go (FR-081). A
  services-off realm has none, so this is a no-op for it.
- exports-removed: the realm's exported entries go: its menu entries and icon copies, its CLI
  launchers, and its sync state (FR-019 AC-1, DR-008). Nothing reads the realm, so this runs
  whether or not the realm still runs, and a rerun removes what is left.
- image-removed: the record's image is removed when no container uses it. A failed rmi keeps the
  image and records the error code, and the delete goes on. A container that shows no image name
  is not compared, and a note says so. A container that uses the image keeps it, and a note names
  it.
- home: the Realm Home is removed only when `purge_home` is asked and `confirm` returns True
  (AS-014, DR-017). Otherwise it is kept, and the result says so.
- record-removed: the record file is removed last (DR-020, `deleting` to removed).

Containers are found by the label `io.ginn.realm` (DR-010, T4.1), not by the stored container id,
and a listed container counts only when its own label names this realm. So a delete also finds a
container whose id was never saved, and a record with no container id can be deleted. That is why
ops/lifecycle.py names `ginn delete` as the remedy for such a record.

Realm Home removal (DR-010, DR-021). The check runs before the record moves, and it changes
nothing. It refuses:
- a homes directory that is a symbolic link, and a realm home that is a link or not a directory;
- a mount point at the home or anywhere below it. The mount table (proc(5), mountinfo) is read, and
  the mount id of every directory that the walk opens is compared with the home's. The device
  alone is not enough: a bind mount of the same file system has the same st_dev;
- a tree deeper than `DeleteHooks.max_depth` levels;
- a tree that cannot be read, or a mount table or mount id that cannot be read (fail closed).
An owned directory that cannot be read is left to the removal, which repairs its mode through a
path descriptor (O_PATH, then chmod through /proc/self/fd, so the inode that was checked is the one
changed).

The removal walks the tree by file descriptor, from a descriptor on the homes directory that is
opened with O_DIRECTORY and O_NOFOLLOW. So no link is followed, and a swapped homes link is
refused. It checks the mount table again before it starts. Each directory it enters must have the
home's mount id, so a mount that appears after the check stops the walk before anything under it
is touched. An entry on another device is never entered or removed. A directory the user owns is
made owner-writable before its entries are removed, so a read-only directory goes too.

An entry that the kernel still refuses stops the purge with GINN-E-HOME-UNSAFE. The error names the
path, and the kernel's filename in its details. Its remedy depends on the reason. A mount point:
unmount it, and never remove it with rm -rf, which would delete the mounted files. An entry that a
subordinate uid owns: `podman unshare rm -rf --one-file-system <realm home>`. Other reasons get the
generic inspect-and-retry remedy. The record stays `deleting`, and the next purge continues where
this one stopped. This module never runs `podman unshare` itself.

Failure and resume. A GinnError from a step leaves the record `deleting`. The next delete starts
again at the first step, and every step is idempotent, so the same delete finishes it. A crash
(any other exception, or SIGKILL) keeps the journal file. Recovery then leaves the record
`deleting`, and a new delete runs after recovery (FR-122). A purge that stopped part way leaves the
rest in place. A re-run without a purge keeps what is left, and the result reports it.

Refusals. A record that DR-020 does not allow to delete (`creating`, `updating`, `rebuilding`) is
refused with GINN-E-REALM-STATE, inside the journal, so the refusal is audited. A running
services-on realm is refused with GINN-E-NOT-SUPPORTED-YET before any write, until T7.4. Its
container is kept running by its runit entry (FR-081), so this module must not stop it with podman
before that entry is down (ADR-003). A stopped services-on realm has no container running, and it is
deleted like any other. An unknown name raises GINN-E-NO-SUCH-REALM before any write (FR-025).

Running realms. The body stops the container with podman itself. It does not call
ops/lifecycle.stop_realm, because that takes the realm lock, which this body already holds, and it
would refuse with GINN-E-REALM-BUSY.

Not built yet. Exported entries (M5, export/), the Install Manifest (M5) and the supervision entry
(M7, services/) have no code. Their removal belongs between `container-removed` and `image-removed`
when those modules exist. The Realm Home is the only other artifact, and it is removed only by the
`home` step.

Confirmation. AS-014 says only "given and confirmed", so the form is the caller's: `confirm` gets
the prompt text and returns True to confirm. Without a `confirm` hook, a purge is declined.
`confirm` runs before the operation takes its locks and before any check. So a purge can be
confirmed and then refused: a link, a mount point, a busy realm, or a state the record does not
allow. The CLI (T4.11) must therefore check the record and the Realm Home read-only before it asks
the user, and it must report a refused purge as refused.

This module makes no direct subprocess call. It reaches podman only through the Podman adapter.
"""

import errno
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from ginn.domain import RealmName, RealmState
from ginn.errors import GinnError, make
from ginn.errors import Warning as GinnWarning
from ginn.export.sync import unexport_all
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops.lifecycle import STOP_TIMEOUT_S
from ginn.ops.security_ops import forget_proxy, leave_agent
from ginn.podman.adapter import Podman
from ginn.podman.models import ContainerInfo, ContainerState
from ginn.runtime.spec import LABEL_REALM
from ginn.services.runit_user import remove_service
from ginn.store.locks import image_lock
from ginn.store.records import RealmRecord, RealmStore

STEPS: Final[tuple[str, ...]] = (
    "deleting",
    "stopped",
    "container-removed",
    "service-removed",
    "exports-removed",
    "image-removed",
    "home",
    "record-removed",
)
"""The journal steps of a delete, in order (FR-122). The tests read these names. Recovery does not
need them: it leaves a `deleting` record in that state whichever step was reached."""
_DELETE_FROM: Final = frozenset(
    {
        RealmState.STOPPED,
        RealmState.RUNNING,
        RealmState.FAILED,
        RealmState.BROKEN,
        RealmState.DELETING,
    }
)
"""The states that DR-020 allows to delete. `creating`, `updating` and `rebuilding` are not."""
_NOT_RUNNING: Final = frozenset({ContainerState.CREATED, ContainerState.EXITED})
"""Container states that need no stop. Any other state, such as paused, is stopped first."""
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
"""Opens a directory for a walk. A link at the last component is never followed (DR-021)."""
_OWNER_RWX: Final = stat.S_IRWXU
"""The owner bits a directory needs so that its entries can be listed and removed."""
MAX_TREE_DEPTH: Final = 256
"""Levels below the realm home that a walk enters. A deeper tree is refused (GINN-E-HOME-UNSAFE)."""
KIND_MOUNT: Final = "mount"
"""A refusal for a mount point. Its remedy is to unmount, never to remove with rm -rf."""
KIND_OWNER: Final = "owner"
"""A refusal for an entry that a subordinate uid owns. Its remedy is `podman unshare rm -rf`."""
KIND_OTHER: Final = "other"
"""Any other refusal. Its remedy is the generic inspect-and-retry text of the catalogue."""


_MOUNTINFO: Final = Path("/proc/self/mountinfo")
"""The mount table of this process (proc(5))."""
_OCTAL_ESCAPE: Final = re.compile(rb"\\([0-7]{3})")
"""A proc(5) escape in a mount table field: a backslash and three octal digits."""


def _mountinfo_points(path: Path) -> list[str]:
    """Every mount point in a mount table (proc(5) mountinfo), decoded.

    The mount point is the fifth field of a line. proc(5) escapes a space, tab, newline and
    backslash there as a backslash and three octal digits, so those escapes are undone before the
    bytes are decoded. A line with fewer than five fields is skipped. An unreadable table raises
    OSError, and the caller fails closed.
    """
    points: list[str] = []
    for line in path.read_bytes().split(b"\n"):
        fields = line.split(b" ")
        if len(fields) > 4:
            unescaped = _OCTAL_ESCAPE.sub(lambda m: bytes([int(m.group(1), 8)]), fields[4])
            points.append(os.fsdecode(unescaped))
    return points


def mount_id_of_fd(fd: int) -> int:
    """The mount id of the file behind `fd`, from /proc/self/fdinfo (proc(5)).

    Two directories with one st_dev can sit on different mounts, such as a bind mount from the same
    file system. Their mount ids differ. Raises OSError or ValueError when the line cannot be read,
    so the caller fails closed.
    """
    text = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii", errors="replace")
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key == "mnt_id":
            return int(value.strip())
    raise ValueError("fdinfo has no mnt_id line")


@dataclass(frozen=True)
class DeleteHooks:
    """The seams of a delete. The defaults are the real ones.

    `on_step` runs after each journal step is recorded, so a test can stop the operation at any
    step. `stop_timeout_s` is the wait before podman kills a running container. `mountinfo` is the
    mount table a purge reads. `mount_id` reads the mount id of an open directory. `max_depth`
    bounds how deep a purge walks.
    """

    on_step: Callable[[str], None] | None = None
    stop_timeout_s: int = STOP_TIMEOUT_S
    mountinfo: Path = _MOUNTINFO
    mount_id: Callable[[int], int] = mount_id_of_fd
    max_depth: int = MAX_TREE_DEPTH


@dataclass(frozen=True)
class DeleteResult:
    """What a delete did.

    `home` is "removed", "kept" or "absent" (AS-014, FR-019). `purge_declined` is True when a purge
    was asked but not confirmed, so the home was kept because of the answer. `image` is "removed",
    "kept" (in use, or its rmi failed) or "absent". `image_code` is the code of a failed rmi, else
    None. `notes` lists what was recovered from or skipped: stop failures, containers that show no
    image name, and the container that keeps the image. `warnings` holds the FR-006 warnings of the
    delete (a host change found after the operation), once it has finished.
    """

    realm: str
    home: str
    purge_declined: bool
    image: str
    image_code: str | None
    notes: tuple[str, ...]
    warnings: tuple[GinnWarning, ...] = ()


def delete_realm(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    purge_home: bool = False,
    confirm: Callable[[str], bool] | None = None,
    hooks: DeleteHooks | None = None,
) -> DeleteResult:
    """Delete realm `name`. Return what happened to its Realm Home, its image and its stops.

    The Realm Home is removed only when `purge_home` is True and `confirm` returns True for the
    prompt. See the module docstring for the steps, the refusals and the resume rule.
    """
    hooks = DeleteHooks() if hooks is None else hooks
    current = RealmStore(env.paths, env.clock).load(name)  # GINN-E-NO-SUCH-REALM, before any write
    _refuse_running_services(current)
    home = env.paths.homes / name.value
    purge = False
    if purge_home and confirm is not None:
        purge = confirm(
            f"Also delete the Realm Home {home} of realm '{name.value}'? It cannot be undone."
        )
    declined = purge_home and not purge
    params = {
        "generation": current.generation,
        "purge_home": purge,
        "purge_declined": declined,
    }
    distro = current.settings.distro.value
    with image_lock(env.paths, distro, "delete"), mutating(env, "delete", name, params) as mutation:
        result = _delete(env, podman, name, purge, declined, hooks, mutation)
    return replace(result, warnings=tuple(mutation.warnings))


def check_purge(env: OpEnv, name: RealmName, *, hooks: DeleteHooks | None = None) -> None:
    """Refuse a purge that cannot run, before the caller asks for confirmation (AS-014, DR-021).

    The CLI calls this before its prompt, because `delete_realm` runs `confirm` before any check.
    It is read-only: no lock, no journal, no audit entry, no write. It raises GINN-E-NO-SUCH-REALM
    for an unknown realm, GINN-E-NOT-SUPPORTED-YET for a running services-on realm (until T7.4), and
    the Realm Home refusals of a purge. `delete_realm` checks again under the locks, so an answer
    given here can still be refused there.
    """
    current = RealmStore(env.paths, env.clock).load(name)
    _refuse_running_services(current)
    _check_home(env, name, DeleteHooks() if hooks is None else hooks)


def _refuse_running_services(record: RealmRecord) -> None:
    """Refuse to delete a running services-on realm, before any write (ADR-003, FR-081, T7.4).

    Stopping it with podman would leave its runit entry running, and runit would restart the
    container (FR-081 AC-2). Its sv service must stop it first, which T7.4 adds.
    """
    if record.settings.services and record.state is RealmState.RUNNING:
        raise make(
            "GINN-E-NOT-SUPPORTED-YET",
            realm=record.name.value,
            feature="delete of a running services-on realm",
            milestone="M7 (T7.4)",
        )


def _delete(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    purge: bool,
    declined: bool,
    hooks: DeleteHooks,
    mutation: Mutation,
) -> DeleteResult:
    """The body of a delete. It runs under the image and realm locks, after the journal entry."""
    store = RealmStore(env.paths, env.clock)
    record = store.load(name)
    if record.state not in _DELETE_FROM:
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state=record.state.value,
            action="delete",
            allowed="stopped, running, failed, broken or deleting",
        )
    if purge:
        _check_home(
            env, name, hooks
        )  # every refusal of the Realm Home comes before the record moves
    if record.state is not RealmState.DELETING:
        store.set_state(name, RealmState.DELETING)
    _step(mutation, hooks, "deleting")

    notes = _stop_containers(podman, name, hooks.stop_timeout_s)
    _release_filter_and_proxy(env, name)
    _step(mutation, hooks, "stopped")

    _remove_containers(podman, name)
    _step(mutation, hooks, "container-removed")

    remove_service(env.paths, name)
    _step(mutation, hooks, "service-removed")

    unexport_all(env.paths, name)
    _step(mutation, hooks, "exports-removed")

    image, image_code, image_notes = _remove_image(podman, record.image_ref)
    notes.extend(image_notes)
    _step(mutation, hooks, "image-removed")

    home_state = _remove_home(env, name, hooks) if purge else _kept_or_absent(env, name)
    mutation.add_params(home=home_state, image=image, image_error=image_code, notes=notes)
    _step(mutation, hooks, "home")

    store.delete(name)
    _step(mutation, hooks, "record-removed")
    return DeleteResult(
        realm=name.value,
        home=home_state,
        purge_declined=declined,
        image=image,
        image_code=image_code,
        notes=tuple(notes),
    )


def _step(mutation: Mutation, hooks: DeleteHooks, name: str) -> None:
    mutation.step(name)
    if hooks.on_step is not None:
        hooks.on_step(name)


def _listed(podman: Podman, name: RealmName) -> list[ContainerInfo]:
    """The containers that podman lists for this realm. Each must carry the realm's own label, so
    a listing that holds another realm's container never acts on it (DR-010)."""
    return [
        info
        for info in podman.ps({LABEL_REALM: name.value})
        if info.labels.get(LABEL_REALM) == name.value
    ]


def _stop_containers(podman: Podman, name: RealmName, timeout_s: int) -> list[str]:
    """Stop each listed container that runs. A stop that fails is noted, and the container is then
    removed with force by _remove_containers, as lifecycle's rollback does (ops/lifecycle.py)."""
    notes: list[str] = []
    for info in _listed(podman, name):
        if info.state in _NOT_RUNNING:
            continue
        try:
            podman.stop(info.id, timeout_s)
        except GinnError as exc:
            notes.append(f"stop of {info.id[:12]} failed with {exc.code}; removed with rm --force")
    return notes


def _release_filter_and_proxy(env: OpEnv, name: RealmName) -> None:
    """Leave the Agent Proxy filter, then forget the realm's D-Bus proxy (FR-067, FR-019 AC-1).

    The filter leaves first, so it stops when no member is left. A plain OSError is raised as a
    GinnError, so the delete fails with a code and the record stays `deleting` for the next delete.
    """
    try:
        leave_agent(env, name)
        forget_proxy(env, name)
    except OSError as exc:
        err = make("GINN-E-INTERNAL")
        err.details.update({"reason": f"release of realm {name.value}: {exc.strerror or exc}"})
        raise err from exc


def _remove_containers(podman: Podman, name: RealmName) -> None:
    """Force-remove each listed container. One that vanished meanwhile is not an error."""
    for info in _listed(podman, name):
        try:
            podman.rm(info.id, force=True)
        except GinnError:
            if any(left.id == info.id for left in _listed(podman, name)):
                raise


def _image_names(podman: Podman, ref: str) -> tuple[str, ...]:
    """The strings by which podman may show this image: its reference, digest, id and repo digests.
    When the image cannot be inspected, only the reference is known."""
    try:
        info = podman.image_inspect(ref)
    except GinnError:
        return (ref,)
    return (ref, info.digest, info.id, *info.repo_digests)


def _uses_image(listed: str, names: Sequence[str]) -> bool:
    """True when a container's image string and one of the image's names contain one another.

    Podman shows an image by name, digest or id, so a plain equality would miss a container. A
    doubtful match keeps the image, which is the safe reading. An empty string is never a match:
    the caller skips it and notes that it did.
    """
    return any(name != "" and (name in listed or listed in name) for name in names)


def _remove_image(podman: Podman, ref: str) -> tuple[str, str | None, list[str]]:
    """Remove the image unless a container uses it, as ops/create.py does. Return the outcome, the
    code of a failed rmi, and the notes that say why the image was kept or a container skipped.

    A failed rmi is not fatal: the image stays, and the delete goes on.
    """
    if not podman.image_exists(ref):
        return "absent", None, []
    names = _image_names(podman, ref)
    notes: list[str] = []
    for info in podman.ps():
        if info.image == "":
            notes.append(f"container {info.id[:12]} shows no image name; it is not compared")
            continue
        if _uses_image(info.image, names):
            notes.append(f"image kept: container {info.id[:12]} uses it")
            return "kept", None, notes
    try:
        podman.rmi(ref)
    except GinnError as exc:
        notes.append(f"image kept: rmi failed with {exc.code}")
        return "kept", exc.code, notes
    return "removed", None, notes


def _kept_or_absent(env: OpEnv, name: RealmName) -> str:
    return "kept" if os.path.lexists(env.paths.homes / name.value) else "absent"


def _ours(st: os.stat_result) -> bool:
    """True when the user owns the entry. Only such a directory is made owner-writable."""
    return st.st_uid == os.getuid()


def _refuse(
    name: RealmName,
    home: Path,
    path: Path,
    reason: str,
    *,
    kind: str = KIND_OTHER,
    filename: str | None = None,
) -> GinnError:
    """GINN-E-HOME-UNSAFE for `path`. `filename` is the name the kernel reported, if any. The
    remedy follows `kind`: unmount for a mount point, and podman unshare for a subordinate uid."""
    err = make("GINN-E-HOME-UNSAFE", realm=name.value, path=str(path), reason=reason)
    err.details["filename"] = str(path) if filename is None else filename
    if kind == KIND_MOUNT:
        err.remedy = (
            f"unmount '{path}', then retry 'ginn delete {name.value} --purge-home'; never remove "
            "it with rm -rf, which would delete the mounted files"
        )
    elif kind == KIND_OWNER:
        err.remedy = (
            f"'{path}' is owned by a subordinate uid, so ginn cannot remove it; first check that "
            f"'findmnt -R {home}' lists nothing, then run "
            f"'podman unshare rm -rf --one-file-system {home}', then retry "
            f"'ginn delete {name.value} --purge-home'"
        )
    return err


def _kind_of(exc: OSError, *, owned: bool) -> str:
    """The refusal kind of a failed removal: a busy mount, a non-owned entry, or other."""
    if exc.errno == errno.EBUSY:
        return KIND_MOUNT
    return KIND_OTHER if owned else KIND_OWNER


def _open_homes(env: OpEnv, name: RealmName) -> int:
    """Open the homes directory without following a link. The caller closes the descriptor."""
    homes = env.paths.homes
    try:
        return os.open(homes, _DIR_FLAGS)
    except OSError as exc:
        raise _refuse(name, homes, homes, exc.strerror or str(exc), filename=str(homes)) from exc


def _check_mount_table(name: RealmName, home: Path, hooks: DeleteHooks) -> None:
    """Refuse a realm home that is a mount point, or that holds one, by the mount table (DR-021).

    A mount point equal to the home or under it refuses. An unreadable table refuses too, so the
    purge fails closed. The table is read by `_mountinfo_points`, not by runtime/attach.py.
    """
    real_home = os.path.join(os.path.realpath(home.parent), home.name)
    try:
        points = _mountinfo_points(hooks.mountinfo)
    except OSError as exc:
        reason = f"the mount table cannot be read ({exc.strerror or exc})"
        raise _refuse(name, home, home, reason) from exc
    for point in points:
        if point == real_home or point.startswith(real_home + "/"):
            raise _refuse(name, home, Path(point), "it is a mount point", kind=KIND_MOUNT)


def _root_stat(name: RealmName, home: Path, homes_fd: int) -> os.stat_result | None:
    """The realm home's own stat, or None when it is absent. Refuses a link, a non-directory, and a
    home on another device than the homes directory. Nothing is changed (DR-021)."""
    try:
        root = os.stat(name.value, dir_fd=homes_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _refuse(name, home, home, exc.strerror or str(exc)) from exc
    if stat.S_ISLNK(root.st_mode):
        raise _refuse(name, home, home, "it is a symbolic link")
    if not stat.S_ISDIR(root.st_mode):
        raise _refuse(name, home, home, "it is not a directory")
    if root.st_dev != os.fstat(homes_fd).st_dev:
        raise _refuse(name, home, home, "it is a mount point", kind=KIND_MOUNT)
    return root


class _Unreadable(Exception):
    """An owned directory that the scan cannot open yet. The removal repairs its mode."""


@dataclass(frozen=True)
class _Walk:
    """One realm home under a walk: its name and path, its device and mount id, and the hooks."""

    name: RealmName
    home: Path
    dev: int
    mnt_id: int
    hooks: DeleteHooks


def _mount_id_checked(name: RealmName, home: Path, fd: int, path: Path, hooks: DeleteHooks) -> int:
    """The mount id of `fd`. An unreadable id refuses, so the walk fails closed."""
    try:
        return hooks.mount_id(fd)
    except (OSError, ValueError) as exc:
        raise _refuse(name, home, path, f"its mount id cannot be read: {exc}") from exc


def _repair_mode(
    ctx: _Walk, dir_fd: int, child: str, where: Path, expected: os.stat_result
) -> None:
    """Give the owner rwx on an owned directory that cannot be opened.

    An O_PATH descriptor needs no read permission on the directory. Before anything changes, its
    mount id must equal the walk's, so a directory on another mount is refused and never chmod'ed.
    The chmod goes through /proc/self/fd, so it acts on the inode that was checked, not on a name
    that could be swapped.
    """
    name, home = ctx.name, ctx.home
    try:
        pfd = os.open(child, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError as exc:
        raise _refuse(name, home, where, exc.strerror or str(exc), filename=child) from exc
    try:
        opened = os.fstat(pfd)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            reason = "it changed before it could be repaired"
            raise _refuse(name, home, where, reason, filename=child)
        if _mount_id_checked(name, home, pfd, where, ctx.hooks) != ctx.mnt_id:
            raise _refuse(name, home, where, "it is a mount point", kind=KIND_MOUNT, filename=child)
        os.chmod(f"/proc/self/fd/{pfd}", stat.S_IMODE(opened.st_mode) | _OWNER_RWX)
    except OSError as exc:
        raise _refuse(name, home, where, exc.strerror or str(exc), filename=child) from exc
    finally:
        os.close(pfd)


def _open_child(
    ctx: _Walk,
    dir_fd: int,
    child: str,
    where: Path,
    expected: os.stat_result,
    *,
    repair: bool,
) -> int:
    """Open entry `child` as a directory, if it is still the one `expected` describes.

    An owned directory that cannot be opened raises _Unreadable when `repair` is off, and is
    repaired (see _repair_mode) when it is on. A directory the user does not own refuses.
    """
    name, home = ctx.name, ctx.home
    try:
        fd = os.open(child, _DIR_FLAGS, dir_fd=dir_fd)
    except PermissionError as exc:
        if not _ours(expected):
            raise _refuse(
                name, home, where, exc.strerror or str(exc), kind=KIND_OWNER, filename=child
            ) from exc
        if not repair:
            raise _Unreadable(child) from exc
        _repair_mode(ctx, dir_fd, child, where, expected)
        try:
            fd = os.open(child, _DIR_FLAGS, dir_fd=dir_fd)
        except OSError as again:
            raise _refuse(
                name, home, where, again.strerror or str(again), filename=child
            ) from again
    except OSError as exc:
        raise _refuse(name, home, where, exc.strerror or str(exc), filename=child) from exc
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(fd)
        raise _refuse(name, home, where, "it changed while it was being read", filename=child)
    return fd


def _enter(
    ctx: _Walk, dir_fd: int, child: str, where: Path, expected: os.stat_result, *, repair: bool
) -> int:
    """Open entry `child` of this walk, and refuse it when it sits on another mount.

    The mount id is compared, not the device, so a bind mount of the same file system is caught.
    This also catches a mount that appeared after the check.
    """
    fd = _open_child(ctx, dir_fd, child, where, expected, repair=repair)
    try:
        if _mount_id_checked(ctx.name, ctx.home, fd, where, ctx.hooks) != ctx.mnt_id:
            raise _refuse(
                ctx.name, ctx.home, where, "it is a mount point", kind=KIND_MOUNT, filename=child
            )
    except GinnError:
        os.close(fd)
        raise
    return fd


def _listing(
    name: RealmName, home: Path, dir_fd: int, path: Path
) -> list[tuple[str, os.stat_result]]:
    """The entries of `dir_fd`, each with its own stat. A link is described, never followed."""
    try:
        with os.scandir(dir_fd) as entries:
            return [(entry.name, entry.stat(follow_symlinks=False)) for entry in entries]
    except OSError as exc:
        raise _refuse(name, home, path, exc.strerror or str(exc)) from exc


def _check_depth(ctx: _Walk, where: Path, depth: int) -> None:
    """Refuse a walk that would go deeper than the limit. The depth bound keeps the recursion
    well inside Python's own limit."""
    if depth > ctx.hooks.max_depth:
        raise _refuse(
            ctx.name,
            ctx.home,
            where,
            f"the tree is too deep (more than {ctx.hooks.max_depth} levels)",
        )


def _open_root(
    name: RealmName, home: Path, homes_fd: int, hooks: DeleteHooks, *, repair: bool
) -> tuple[_Walk, int] | None:
    """Open the realm home as the root of a walk. Return the walk and the home's descriptor.

    None when the home is absent, or, for a check, when it is an owned home that cannot be read yet
    (the removal repairs it). The walk's mount id is the homes directory's, so the home itself must
    sit on that mount. A home on another mount, such as a bind mount from the same file system, is
    refused before the record moves (DR-021).
    """
    root = _root_stat(name, home, homes_fd)
    if root is None:
        return None
    homes_mnt = _mount_id_checked(name, home, homes_fd, home.parent, hooks)
    ctx = _Walk(name, home, root.st_dev, homes_mnt, hooks)
    try:
        return ctx, _enter(ctx, homes_fd, name.value, home, root, repair=repair)
    except _Unreadable:
        return None


def _check_home(env: OpEnv, name: RealmName, hooks: DeleteHooks) -> None:
    """Refuse a Realm Home that a purge could not remove safely, before the record moves.

    It changes nothing. The mount table is read, and the homes directory and the home are opened
    without following links. Every directory of the tree is compared by mount id with the homes
    directory's, so a bind mount from the same file system is caught.
    """
    home = env.paths.homes / name.value
    if os.path.islink(env.paths.homes):
        raise _refuse(name, home, env.paths.homes, "the homes directory is a symbolic link")
    if not os.path.lexists(home):
        return
    _check_mount_table(name, home, hooks)
    homes_fd = _open_homes(env, name)
    try:
        opened = _open_root(name, home, homes_fd, hooks, repair=False)
        if opened is None:
            return
        ctx, home_fd = opened
        try:
            _scan_tree(ctx, home_fd, home, 0)
        finally:
            os.close(home_fd)
    finally:
        os.close(homes_fd)


def _scan_tree(ctx: _Walk, dir_fd: int, path: Path, depth: int) -> None:
    """Refuse a tree with a mount point, or one that is too deep. It only reads."""
    for child, st in _listing(ctx.name, ctx.home, dir_fd, path):
        where = path / child
        if st.st_dev != ctx.dev:
            raise _refuse(
                ctx.name, ctx.home, where, "it is a mount point", kind=KIND_MOUNT, filename=child
            )
        if not stat.S_ISDIR(st.st_mode):
            continue
        _check_depth(ctx, where, depth + 1)
        try:
            sub = _enter(ctx, dir_fd, child, where, st, repair=False)
        except _Unreadable:
            continue  # the mount table covers its mounts, and the removal repairs its mode
        try:
            _scan_tree(ctx, sub, where, depth + 1)
        finally:
            os.close(sub)


def _remove_home(env: OpEnv, name: RealmName, hooks: DeleteHooks) -> str:
    """Remove the Realm Home through a descriptor on the homes directory. Return "removed" or
    "absent". A refused entry raises GINN-E-HOME-UNSAFE, and the record stays `deleting`."""
    home = env.paths.homes / name.value
    if not os.path.lexists(home):
        return "absent"
    _check_mount_table(name, home, hooks)  # again: a mount may have appeared since the check
    homes_fd = _open_homes(env, name)
    try:
        opened = _open_root(name, home, homes_fd, hooks, repair=True)
        if opened is None:
            return "absent"
        ctx, home_fd = opened
        try:
            _remove_contents(ctx, home_fd, home, 0)
        finally:
            os.close(home_fd)
        try:
            os.rmdir(name.value, dir_fd=homes_fd)
        except OSError as exc:
            raise _refuse(name, home, home, exc.strerror or str(exc), filename=name.value) from exc
    finally:
        os.close(homes_fd)
    return "removed"


def _make_owner_writable(ctx: _Walk, dir_fd: int, path: Path) -> None:
    """Give the owner rwx on a directory the user owns, so that its entries can be removed."""
    st = os.fstat(dir_fd)
    if not _ours(st) or st.st_mode & _OWNER_RWX == _OWNER_RWX:
        return
    try:
        os.fchmod(dir_fd, stat.S_IMODE(st.st_mode) | _OWNER_RWX)
    except OSError as exc:
        raise _refuse(ctx.name, ctx.home, path, exc.strerror or str(exc)) from exc


def _remove_contents(ctx: _Walk, dir_fd: int, path: Path, depth: int) -> None:
    """Empty the directory `dir_fd`. An entry on another device, or in another mount, is never
    entered or removed. The check on each entry comes before its removal, so the walk stops at
    the first such entry, and nothing after it is touched.
    """
    _make_owner_writable(ctx, dir_fd, path)
    dir_is_ours = _ours(os.fstat(dir_fd))
    for child, st in _listing(ctx.name, ctx.home, dir_fd, path):
        where = path / child
        if st.st_dev != ctx.dev:
            raise _refuse(
                ctx.name, ctx.home, where, "it is a mount point", kind=KIND_MOUNT, filename=child
            )
        try:
            if stat.S_ISDIR(st.st_mode):
                _check_depth(ctx, where, depth + 1)
                sub = _enter(ctx, dir_fd, child, where, st, repair=True)
                try:
                    _remove_contents(ctx, sub, where, depth + 1)
                finally:
                    os.close(sub)
                os.rmdir(child, dir_fd=dir_fd)
            else:
                os.unlink(child, dir_fd=dir_fd)
        except OSError as exc:
            kind = _kind_of(exc, owned=dir_is_ours and _ours(st))
            filename = child if exc.filename is None else str(exc.filename)
            raise _refuse(
                ctx.name, ctx.home, where, exc.strerror or str(exc), kind=kind, filename=filename
            ) from exc


__all__ = [
    "MAX_TREE_DEPTH",
    "STEPS",
    "DeleteHooks",
    "DeleteResult",
    "check_purge",
    "delete_realm",
    "mount_id_of_fd",
]
