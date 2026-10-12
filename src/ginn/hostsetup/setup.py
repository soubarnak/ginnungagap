# SPDX-License-Identifier: Apache-2.0
"""One-time Host Setup for one user under a root prefix (FR-001, FR-002, FR-003, DR-009, NFR-030).

`run_setup` runs these steps. Nothing under the target paths is read or written before step 2.

1. The effective uid must be 0 (GINN-E-SETUP-ROOT). Then --user must be given (GINN-E-USAGE).
2. The user is resolved by name (GINN-E-NO-SUCH-USER). The root account is refused.
3. Idempotence, with no lock (FR-002). A complete record whose setup-written paths still match
   their post-images (digest and mode), whose subordinate ID entries cover the recorded ranges and
   whose planned paths are all recorded, means "already configured". Then nothing is locked,
   created or written, not even a lock file.
4. The global lock /run/lock/ginn-host-setup.lock is taken (GINN-E-SETUP-BUSY after 30 s). Under
   it the record is loaded and the check is repeated, because another run may have finished.
   Directories that this acquisition creates are remembered, so their pre-image is "absent".
5. Pre-images are captured for every path setup will write and for each service-written path.
   Nothing is written yet. A repair keeps the stored pre-images, and captures a fresh pre-image of
   each newly planned path before writing it. Stale temporary names are removed.
6. The record is written with status pending. This is the first write. When etc/ginn does not
   exist, the record goes into etc/.ginn.ginn-tmp first, and that directory is renamed to etc/ginn.
7. The planned directories are created, parents first. A directory that a repair finds with a
   changed mode gets its mode back.
8. Subordinate IDs for /etc/subuid, then /etc/subgid. The record is written again, still pending.
9. The service scripts under etc/sv/ginn-<user> (run, finish, log/run).
10. The service link, last, so runsvdir sees a complete service. On Void the link goes into the
    directory that var/service resolves to, not into var/service itself.
11. The record is written with status complete and the post-images.

A crash after any step leaves a pending record, or a temporary directory with no record. The next
run keeps the stored pre-images, reuses the stored subordinate ID range, and repeats the steps.

Void keeps /var/service as a symbolic link chain (ADR-013, Decision 10). Setup follows that chain
inside the root, hop by hop with readlink: a relative target resolves against the directory of its
link, and an absolute target is re-rooted under the root. The chain may be at most 40 hops long.
Every other path refuses symbolic links. The chain must end at a real directory owned by uid 0,
or the run refuses with GINN-E-SETUP-PATH-UNSAFE before any write.

The service-written paths are the root cgroup subtree_control, the ginn cgroup directories, and
/run/ginn with its <uid> directory. The supervisor run script writes them when the service starts
(ADR-003). Setup does not write them, so it only captures their pre-images (ADR-013, Decision 12).

Every host file is placed under `root_dir`. The rendered script contents keep their host paths,
such as /run/ginn/<uid> and /etc/sv/ginn-<user>. Setup writes its own files with fixed temporary
names, so a SIGKILL leaves a predictable sibling for the next run to remove.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import pwd
import shutil
import stat
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from ginn.errors import GinnError, make
from ginn.hostsetup import record as rec
from ginn.hostsetup import runit_system, subids
from ginn.hostsetup._atomic import (
    atomic_symlink,
    atomic_write,
    fixed_tmp_path,
    remove_stale_tmp,
    sync_dir,
)
from ginn.hostsetup.cgroups import DEFAULT_CGROUP_ROOT, GINN_DIR, SUPERVISOR_LEAF
from ginn.store._time import now_iso

LOCK_DIR = "run/lock"
SETUP_LOCK = f"{LOCK_DIR}/ginn-host-setup.lock"
"""The global setup lock (ADR-013, Decision 11). It is not recorded: it is an empty tmpfs file."""
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.05
SUBUID = "etc/subuid"
SUBGID = "etc/subgid"
TMP_DIR = "etc/.ginn.ginn-tmp"
"""The directory the first record is written into, before it is renamed to etc/ginn."""
BOOT_ID = "proc/sys/kernel/random/boot_id"
LINK_PARENT = runit_system.LINK_PARENT
MAX_HOPS = 40
DIR_MODE = 0o755
ALREADY_CONFIGURED = "already configured"
CONFIGURED = "configured"


class UserInfo(NamedTuple):
    """What the account database says about a user: numeric ids and the home directory."""

    uid: int
    gid: int
    home: str


UserLookup = Callable[[str], UserInfo]
"""Resolves a user name. Raises KeyError for an unknown name, as pwd.getpwnam does."""
FaultHook = Callable[[str], None]
"""Called with the name of each step after it finishes. Tests raise here to simulate a crash."""
Clock = Callable[[], float]
OwnerOf = Callable[[Path], int]
"""Returns the owning uid of a real directory. Tests inject a root owner: they run as a user."""


@dataclass(frozen=True)
class SetupResult:
    """The outcome of one `run_setup` call."""

    user: str
    uid: int
    gid: int
    record: str
    already_configured: bool
    paths: int

    @property
    def status(self) -> str:
        return ALREADY_CONFIGURED if self.already_configured else CONFIGURED

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "user": self.user,
            "uid": self.uid,
            "gid": self.gid,
            "record": self.record,
            "paths": self.paths,
        }

    def render(self) -> str:
        if self.already_configured:
            return f"{self.user}: already configured; nothing changed (record {self.record})"
        return f"{self.user}: host setup complete; record {self.record} lists {self.paths} paths"


@dataclass(frozen=True)
class _Target:
    """What one run writes for `user`: the rendered scripts and the resolved path of the link."""

    user: str
    uid: int
    gid: int
    rendered: Mapping[str, str]
    link_rel: str

    @property
    def leaves(self) -> dict[str, str]:
        """Every planned file, link and append-only file, with its kind."""
        leaves = {
            rec.relpath(self.user): "file",
            SUBUID: "append-lines",
            SUBGID: "append-lines",
            self.link_rel: "symlink",
        }
        leaves.update({rel: "file" for rel in self.rendered})
        return leaves

    @property
    def write_paths(self) -> tuple[str, ...]:
        """Every path this run writes, so each can be swept of its stale temporary name."""
        return (rec.relpath(self.user), SUBUID, SUBGID, self.link_rel, *self.rendered)


def run_setup(
    user: str | None,
    *,
    root_dir: Path,
    euid: int | None = None,
    lookup_user: UserLookup | None = None,
    clock: Clock = time.time,
    fault: FaultHook | None = None,
    owner_of: OwnerOf | None = None,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> SetupResult:
    """Run Host Setup for `user` with every host path under `root_dir`. See the module docstring.

    `euid` defaults to the process's effective uid, and `lookup_user` to pwd.getpwnam. `clock`
    gives epoch seconds for the record's timestamp. `fault` is called after each step. `owner_of`
    gives the owner of the directory that the service link goes into, and defaults to the
    real owner. `lock_timeout` is how long to wait for the global lock.
    """
    if (os.geteuid() if euid is None else euid) != 0:
        raise make("GINN-E-SETUP-ROOT")
    if user is None:
        raise make("GINN-E-USAGE", detail="--user NAME is required")
    root = _check_root(root_dir)
    info = _resolve(user, lookup_user or _pwd_lookup)
    owner = owner_of or _owner_uid
    own = rec.relpath(user)
    record_path = root / own
    _check_parents(root, own)
    current = rec.load(record_path)
    if current is not None and current.status == "complete":
        if _intact(root, current, _target(root, user, info, owner)):
            return _result(current, record_path, already=True)
    with _global_lock(root, lock_timeout) as created:
        current = rec.load(record_path)
        target = _target(root, user, info, owner)
        if current is not None and current.status == "complete":
            if _intact(root, current, target):
                return _result(current, record_path, already=True)
        now = now_iso(clock)
        if current is None:
            pending = _capture(root, target, created, now)
        else:
            pending = _repair(root, current, target, created, now)
        _guard(root, pending)
        _sweep(root, target)
        _write_pending(root, record_path, pending)
        _hit(fault, "pending")
        _make_planned_dirs(root, pending)
        _hit(fault, "dirs")
        pending = replace(pending, subids=_subids_step(root, target, pending, fault))
        rec.save(record_path, pending)
        _hit(fault, "subids")
        _write_scripts(root, target)
        _hit(fault, "sv")
        _link_service(root, target)
        _hit(fault, "symlink")
        done = replace(
            pending,
            status="complete",
            timestamp=now,
            paths=tuple(_post(root, entry, own) for entry in pending.paths),
        )
        rec.save(record_path, done)
        _hit(fault, "complete")
        return _result(done, record_path, already=False)


def read_boot_id(root: Path) -> str | None:
    """Return the boot id under `root` (/proc/sys/kernel/random/boot_id on a host), or None."""
    try:
        raw = (root / BOOT_ID).read_bytes()
    except OSError:
        return None
    text = raw.decode("ascii", errors="replace").strip()
    return text if text and "\0" not in text and "\n" not in text else None


def _check_root(root_dir: Path) -> Path:
    if not root_dir.is_absolute():
        raise make("GINN-E-USAGE", detail=f"--root-dir must be an absolute path: {root_dir}")
    if not root_dir.is_dir():
        raise make("GINN-E-USAGE", detail=f"--root-dir is not a directory: {root_dir}")
    return root_dir


def _pwd_lookup(name: str) -> UserInfo:
    entry = pwd.getpwnam(name)
    return UserInfo(uid=entry.pw_uid, gid=entry.pw_gid, home=entry.pw_dir)


def _owner_uid(path: Path) -> int:
    return os.lstat(path).st_uid


def _resolve(user: str, lookup: UserLookup) -> UserInfo:
    if rec.USER_NAME.fullmatch(user) is None:
        raise make("GINN-E-NO-SUCH-USER", user=user)
    try:
        info = lookup(user)
    except KeyError:
        raise make("GINN-E-NO-SUCH-USER", user=user) from None
    if info.uid <= 0 or info.gid <= 0:
        raise make(
            "GINN-E-USAGE",
            detail=f"account {user} has uid {info.uid} and gid {info.gid}; Host Setup does not "
            "set up root",
        )
    if not info.home.startswith("/"):
        raise make("GINN-E-USAGE", detail=f"account {user} has no absolute home directory")
    return info


def _render(user: str, info: UserInfo) -> dict[str, str]:
    home = PurePosixPath(info.home)
    xdg = {
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    return runit_system.render(user, info.uid, info.gid, info.home, xdg)


def _target(root: Path, user: str, info: UserInfo, owner_of: OwnerOf) -> _Target:
    """Plan the writes for `user`. Resolves the service chain, which reads and never writes."""
    service = _resolve_service_dir(root, owner_of)
    return _Target(
        user=user,
        uid=info.uid,
        gid=info.gid,
        rendered=_render(user, info),
        link_rel=f"{service}/ginn-{user}",
    )


def _resolve_service_dir(root: Path, owner_of: OwnerOf) -> str:
    """Follow the var/service chain inside `root` and return the directory the link goes into.

    Only the chain may hold symbolic links. Each hop is read with readlink, so the kernel never
    follows one. A relative target resolves against the directory of its link, and an absolute
    target is re-rooted under `root`. A `..` that would leave `root` is refused, and so is a chain
    of more than MAX_HOPS hops. The chain must end at a real directory owned by uid 0, or it must
    be absent altogether, in which case setup creates it as a plain directory.
    """
    _check_parents(root, f"{PurePosixPath(LINK_PARENT).parts[0]}/x")
    pending = list(PurePosixPath(LINK_PARENT).parts)
    done: list[str] = []
    hops = 0
    missing = False
    while pending:
        part = pending.pop(0)
        if part == "..":
            if not done:
                raise _unsafe(root / LINK_PARENT, "its symbolic link chain leaves the root")
            done.pop()
            continue
        if missing:
            done.append(part)
            continue
        here = root.joinpath(*done, part)
        state, _, link = _probe(here)
        if state == "symlink":
            hops += 1
            if hops > MAX_HOPS:
                raise _unsafe(here, f"is a symbolic link chain of more than {MAX_HOPS} hops")
            links = list(PurePosixPath(link).parts)
            if link.startswith("/"):
                done = []
                links = links[1:]
            pending = links + pending
            continue
        if state == "absent":
            if hops:
                raise _unsafe(here, "ends a symbolic link chain, but nothing is there")
            missing = True
            done.append(part)
            continue
        if state != "dir":
            raise _unsafe(here, f"is a {state} in the var/service chain, but a directory is needed")
        done.append(part)
    if not done:
        raise _unsafe(root / LINK_PARENT, "resolves to the root directory")
    rel = "/".join(done)
    if missing:
        return rel
    owner = owner_of(root / rel)
    if owner != 0:
        raise _unsafe(root / rel, f"is owned by uid {owner}, but uid 0 must own it")
    return rel


def _capture(
    root: Path,
    target: _Target,
    created: set[str],
    now: str,
) -> rec.HostSetupRecord:
    """Read the pre-image of every path this run will write, and of each service path. No writes."""
    entries = _entries(root, target, set(), created)
    service: list[rec.PathEntry] = []
    for rel, kind in _service_written(target.uid):
        pre = _read(root, rel, pre=True)
        _expect(root / rel, pre, kind)
        service.append(rec.PathEntry(path=rel, kind=kind, pre=pre, shared=_is_shared(rel, target)))
    return rec.HostSetupRecord(
        user=target.user,
        uid=target.uid,
        gid=target.gid,
        timestamp=now,
        status="pending",
        subids=None,
        paths=_sorted(entries),
        service_paths=_sorted(service),
        boot_id=read_boot_id(root),
    )


def _repair(
    root: Path,
    current: rec.HostSetupRecord,
    target: _Target,
    created: set[str],
    now: str,
) -> rec.HostSetupRecord:
    """Keep every stored pre-image, and add a fresh one for each newly planned path (m3)."""
    kept = tuple(replace(entry, post=None) for entry in current.paths)
    fresh = _entries(root, target, {entry.path for entry in kept}, created)
    return replace(
        current,
        uid=target.uid,
        gid=target.gid,
        status="pending",
        timestamp=now,
        paths=_sorted((*kept, *fresh)),
    )


def _entries(
    root: Path,
    target: _Target,
    have: set[str],
    created: set[str],
) -> list[rec.PathEntry]:
    """Return entries for the planned paths that are not in `have`, with fresh pre-images.

    A directory that existed before setup is not recorded, because setup does not write it. A
    directory or file that `created` names was made by this run's lock, so its pre-image is absent.
    """
    leaves = target.leaves
    dirs: set[str] = {LOCK_DIR, *_ancestors(LOCK_DIR)}
    for rel in leaves:
        dirs.update(_ancestors(rel))
    entries: list[rec.PathEntry] = []
    for rel in sorted(dirs):
        if rel in have:
            continue
        if rel in created:
            pre = rec.Image("absent")
        else:
            pre = _read(root, rel, pre=True)
            if pre.state == "dir":
                continue
            _expect(root / rel, pre, "dir")
        entries.append(rec.PathEntry(path=rel, kind="dir", pre=pre, shared=_is_shared(rel, target)))
    for rel, kind in sorted(leaves.items()):
        if rel in have:
            continue
        if rel in created:
            pre = rec.Image("absent")
        else:
            pre = _read(root, rel, pre=True)
            _expect(root / rel, pre, kind)
        entries.append(rec.PathEntry(path=rel, kind=kind, pre=pre, shared=_is_shared(rel, target)))
    return entries


def _service_written(uid: int) -> tuple[tuple[str, str], ...]:
    """The service-written paths and their kinds (ADR-013, Decision 12)."""
    cgroup = PurePosixPath(str(DEFAULT_CGROUP_ROOT)).relative_to("/")
    ginn = cgroup / GINN_DIR
    user = ginn / str(uid)
    return (
        (f"{cgroup}/cgroup.subtree_control", "file"),
        (str(ginn), "dir"),
        (str(user), "dir"),
        (str(user / SUPERVISOR_LEAF), "dir"),
        ("run/ginn", "dir"),
        (f"run/ginn/{uid}", "dir"),
    )


def _is_shared(rel: str, target: _Target) -> bool:
    """True unless the path names this user or this user's uid (ADR-013, Decision 12)."""
    own = {f"ginn-{target.user}", f"setup-{target.user}.json", str(target.uid)}
    return not any(part in own for part in PurePosixPath(rel).parts)


def _intact(root: Path, current: rec.HostSetupRecord, target: _Target) -> bool:
    """True when a complete record still describes the host (FR-002). Reads only."""
    if (current.uid, current.gid) != (target.uid, target.gid) or current.subids is None:
        return False
    recorded = {entry.path for entry in current.paths}
    if not set(target.leaves) <= recorded:
        return False
    own = rec.relpath(target.user)
    for entry in current.paths:
        if entry.path == own or entry.kind == "append-lines":
            continue
        if entry.post is None:
            return False
        if _read(root, entry.path, pre=False) != entry.post:
            return False
    return _covers(root, SUBUID, current.subids.uid) and _covers(root, SUBGID, current.subids.gid)


def _covers(root: Path, rel: str, entry: subids.SubIdEntry) -> bool:
    """True when the file has a line for the name that covers the recorded range (Decision 16)."""
    _check_parents(root, rel)
    state, _, _ = _probe(root / rel)
    if state != "file":
        return False
    text = (root / rel).read_bytes().decode("utf-8", errors="surrogateescape")
    return any(
        line.name == entry.name and line.start <= entry.start and line.end >= entry.end
        for line in subids.parse_entries(text)
    )


def _guard(root: Path, record: rec.HostSetupRecord) -> None:
    """Refuse before any write when a path the record lists is now of the wrong kind."""
    for entry in record.paths:
        _expect(root / entry.path, _read(root, entry.path, pre=True), entry.kind)


def _sweep(root: Path, target: _Target) -> None:
    """Remove the temporary names that an earlier run left behind (ADR-013, Decision 16).

    Runs under the lock, before the first write. Only the fixed names are removed. Nothing else in
    /etc is touched.
    """
    _check_parents(root, TMP_DIR)
    state, _, _ = _probe(root / TMP_DIR)
    if state == "dir":
        shutil.rmtree(root / TMP_DIR)
    elif state != "absent":
        raise _unsafe(root / TMP_DIR, f"is a {state}, but a directory is needed")
    for rel in target.write_paths:
        _check_parents(root, rel)
        tmp = fixed_tmp_path(root / rel)
        try:
            remove_stale_tmp(tmp)
        except IsADirectoryError as exc:
            raise _unsafe(tmp, "is a directory where Host Setup keeps a temporary file") from exc


def _write_pending(root: Path, record_path: Path, record: rec.HostSetupRecord) -> None:
    """Write the pending record. The first creation of etc/ginn goes through a temporary dir."""
    directory = record_path.parent
    state, _, _ = _probe(directory)
    if state != "absent":
        rec.save(record_path, record)
        return
    _ensure_dir(root, "etc")
    tmp = root / TMP_DIR
    os.mkdir(tmp, DIR_MODE)
    os.chmod(tmp, DIR_MODE)
    rec.save(tmp / record_path.name, record)
    os.rename(tmp, directory)
    sync_dir(root / "etc")


def _make_planned_dirs(root: Path, record: rec.HostSetupRecord) -> None:
    """Create the directories the record planned, parents first, and restore their mode."""
    for entry in record.paths:
        if entry.kind == "dir" and entry.pre.state == "absent":
            _ensure_dir(root, entry.path)
            _, mode, _ = _probe(root / entry.path)
            if mode != DIR_MODE:
                os.chmod(root / entry.path, DIR_MODE)


def _subids_step(
    root: Path,
    target: _Target,
    pending: rec.HostSetupRecord,
    fault: FaultHook | None,
) -> rec.SubIds:
    """Set the ranges up for the first time, or reuse the recorded ones (FR-001, AS-005)."""
    if pending.subids is None:
        _, uid_entry = subids.ensure_single(
            root / SUBUID, target.user, root_dir=root, uid=target.uid
        )
        _hit(fault, "subuid")
        _, gid_entry = subids.ensure_single(
            root / SUBGID, target.user, root_dir=root, uid=target.gid
        )
        _hit(fault, "subgid")
        return rec.SubIds(uid=uid_entry, gid=gid_entry)
    subids.ensure_exact(root / SUBUID, pending.subids.uid, root_dir=root)
    _hit(fault, "subuid")
    subids.ensure_exact(root / SUBGID, pending.subids.gid, root_dir=root)
    _hit(fault, "subgid")
    return pending.subids


def _write_scripts(root: Path, target: _Target) -> None:
    """Write the rendered scripts with fixed temporary names. A matching script is left alone."""
    for rel in sorted(target.rendered):
        path = root / rel
        data = target.rendered[rel].encode("utf-8")
        image = _read(root, rel, pre=True)
        if image.state == "file" and image.data == data and image.mode == runit_system.SCRIPT_MODE:
            continue
        _expect(path, image, "file")
        atomic_write(path, data, runit_system.SCRIPT_MODE, fixed_tmp=True)


def _link_service(root: Path, target: _Target) -> None:
    """Point the service link at /etc/sv/ginn-<user>, in the resolved directory."""
    _check_parents(root, target.link_rel)
    link = root / target.link_rel
    want = f"/{runit_system.SV_PARENT}/ginn-{target.user}"
    state, _, found = _probe(link)
    if state == "symlink" and found == want:
        return
    if state == "absent":
        try:
            os.symlink(want, link)
        except FileExistsError:
            # A racing process may have made the same link. Anything else is refused.
            now_state, _, now_found = _probe(link)
            if (now_state, now_found) != ("symlink", want):
                raise _unsafe(link, "appeared while Host Setup ran") from None
        sync_dir(link.parent)
        return
    if state == "symlink":
        atomic_symlink(link, want)
        return
    raise _unsafe(link, f"is a {state}, but a symbolic link is needed")


def _ensure_dir(root: Path, rel: str) -> list[str]:
    """Create `rel` and its missing parents under `root`, each with mode 0755. Return what it made.

    A directory that a racing process made first is accepted. A file or link in the way is not.
    """
    created: list[str] = []
    parts = PurePosixPath(rel).parts
    current = root
    for index, part in enumerate(parts):
        current = current / part
        state, _, _ = _probe(current)
        if state == "dir":
            continue
        if state != "absent":
            raise _unsafe(current, f"is a {state}, but a directory is needed")
        try:
            os.mkdir(current, DIR_MODE)
        except FileExistsError:
            if _probe(current)[0] != "dir":
                raise _unsafe(current, "appeared as a non-directory while Host Setup ran") from None
            continue
        os.chmod(current, DIR_MODE)
        created.append("/".join(parts[: index + 1]))
    return created


@contextmanager
def _global_lock(root: Path, timeout: float) -> Iterator[set[str]]:
    """Hold /run/lock/ginn-host-setup.lock (ADR-013, Decision 11). Yields the directories made.

    The lock directories are created when they are missing. The lock file is not recorded.
    """
    created = set(_ensure_dir(root, LOCK_DIR))
    fd = _open_lock(root / SETUP_LOCK)
    try:
        _take_lock(fd, root / SETUP_LOCK, timeout)
        yield created
    finally:
        os.close(fd)


def _open_lock(path: Path) -> int:
    """Open the lock file, creating it when it is missing. A symbolic link is refused."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _unsafe(path, "is a symbolic link, but a regular file is needed") from exc
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise _unsafe(path, "is not a regular file, but the lock needs one")
    return fd


def _take_lock(fd: int, path: Path, timeout: float) -> None:
    """Take the lock without blocking, polling until `timeout`. Then GINN-E-SETUP-BUSY."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        if time.monotonic() >= deadline:
            raise make("GINN-E-SETUP-BUSY", lock=str(path), seconds=f"{timeout:g}")
        time.sleep(LOCK_POLL_SECONDS)


def _probe(path: Path) -> tuple[str, int, str]:
    """Return (state, mode bits, link target) of `path`. A symbolic link is never followed.

    The target is "" unless the state is symlink.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "absent", 0, ""
    except NotADirectoryError:
        raise _unsafe(path, "has a parent that is not a directory") from None
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        return "symlink", mode, os.readlink(path)
    if stat.S_ISDIR(st.st_mode):
        return "dir", mode, ""
    if stat.S_ISREG(st.st_mode):
        return "file", mode, ""
    raise _unsafe(path, "is neither a regular file, a directory nor a symbolic link")


def _image(path: Path, *, pre: bool) -> rec.Image:
    """Return the state of `path` without following a symbolic link. Refuses other file types."""
    state, mode, target = _probe(path)
    if state == "absent":
        return rec.Image("absent")
    if state == "symlink":
        return rec.Image("symlink", target=target)
    if state == "dir":
        return rec.Image("dir", mode=mode)
    data = path.read_bytes()
    if pre:
        return rec.Image("file", mode=mode, data=data)
    return rec.Image("file", mode=mode, sha256=hashlib.sha256(data).hexdigest())


def _read(root: Path, rel: str, *, pre: bool) -> rec.Image:
    """`_image` for a root-relative path, after refusing a symbolic link among its parents."""
    _check_parents(root, rel)
    return _image(root / rel, pre=pre)


def _expect(path: Path, image: rec.Image, kind: str) -> None:
    if image.state not in ("absent", rec.STATE_OF_KIND[kind]):
        raise _unsafe(path, f"is a {image.state}, but Host Setup needs a {kind} there")


def _check_parents(root: Path, rel: str) -> None:
    """Refuse a symbolic link or file among the parents of `rel`, so no write follows one."""
    current = root
    for part in PurePosixPath(rel).parts[:-1]:
        current = current / part
        state, _, _ = _probe(current)
        if state == "absent":
            return
        if state != "dir":
            raise _unsafe(current, f"is a {state}, but a directory is needed")


def _post(root: Path, entry: rec.PathEntry, own: str) -> rec.PathEntry:
    if entry.path == own:
        return replace(entry, post=rec.Image("self"))
    return replace(entry, post=_read(root, entry.path, pre=False))


def _sorted(entries: Iterable[rec.PathEntry]) -> tuple[rec.PathEntry, ...]:
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _result(record: rec.HostSetupRecord, record_path: Path, *, already: bool) -> SetupResult:
    return SetupResult(
        user=record.user,
        uid=record.uid,
        gid=record.gid,
        record=str(record_path),
        already_configured=already,
        paths=len(record.paths),
    )


def _ancestors(rel: str) -> list[str]:
    parts = PurePosixPath(rel).parts
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _unsafe(path: Path, reason: str) -> GinnError:
    return rec.unsafe(path, reason)


def _hit(fault: FaultHook | None, step: str) -> None:
    if fault is not None:
        fault(step)
