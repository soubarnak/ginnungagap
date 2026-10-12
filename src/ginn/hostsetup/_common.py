# SPDX-License-Identifier: Apache-2.0
"""Helpers that Host Setup and its undo share: root, account, probe, parents and lock.

These functions were duplicated between `setup.py` and `undo.py` (FR-001, FR-003, ADR-013 Decision
11). They hold no state. Every host path is given relative to a root directory, so tests run under
a temporary root. Nothing here writes a host file, except `_ensure_dir` and `_open_lock`, which
create a missing directory or the empty lock file, and only when the caller asks for it.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pwd
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from ginn.errors import make
from ginn.hostsetup import record as rec
from ginn.hostsetup.cgroups import DEFAULT_CGROUP_ROOT, GINN_DIR

LOCK_DIR = "run/lock"
SETUP_LOCK = f"{LOCK_DIR}/ginn-host-setup.lock"
"""The global setup lock (ADR-013, Decision 11). It is not recorded: it is an empty tmpfs file."""
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.05
SUBUID = "etc/subuid"
SUBGID = "etc/subgid"
DIR_MODE = 0o755


class UserInfo(NamedTuple):
    """What the account database says about a user: numeric ids and the home directory."""

    uid: int
    gid: int
    home: str


UserLookup = Callable[[str], UserInfo]
"""Resolves a user name. Raises KeyError for an unknown name, as pwd.getpwnam does."""
OwnerOf = Callable[[Path], int]
"""Returns the owning uid of a real directory. Tests inject a root owner: they run as a user."""


RUNIT_DIR = "run/runit"
"""Where runit keeps the supervise state of each service: `run/runit/supervise.<service>`."""


def user_component(part: str, user: str, uid: int) -> bool:
    """True when one path component marks the path as per-user (ADR-013 Decision 12, F11).

    A component is per-user when it equals the decimal uid, `ginn-<user>` or `setup-<user>.json`,
    or when it ends with `.ginn-<user>` or `.ginn-<user>-log`, which is the name runit gives a
    service's supervise state (`supervise.ginn-alice`). Components are never matched as substrings.
    """
    if part in {f"ginn-{user}", f"setup-{user}.json", str(uid)}:
        return True
    return part.endswith((f".ginn-{user}", f".ginn-{user}-log"))


def supervise_links(user: str) -> dict[str, str]:
    """The runsv supervise links of the service directory and its log, with their targets.

    Void's convention (80 of 89 services on the reference host). runsv creates its state under
    /run/runit, not in the service directory, so undo can remove the service directory with rmdir.
    The targets are absolute, as the host sees them, and undo never follows them.
    """
    base = f"etc/sv/ginn-{user}"
    return {
        f"{base}/supervise": f"/{RUNIT_DIR}/supervise.ginn-{user}",
        f"{base}/log/supervise": f"/{RUNIT_DIR}/supervise.ginn-{user}-log",
    }


def delegated_dirs(uid: int) -> dict[str, int]:
    """The per-user directories that the run script chowns to the user, mapped to that uid.

    ADR-003 (S-01 step 6, `cgroups.chown_groups`) chowns `ginn/<uid>` under the cgroup root. ADR-004
    (step 1 of the run script) gives `/run/ginn/<uid>` to the user with mode 0700. The supervisor
    leaf is not listed, because it stays root-owned. Their parent check accepts the record's uid as
    owner (ADR-013, Decision 27).
    """
    cgroup = str(DEFAULT_CGROUP_ROOT / GINN_DIR / str(uid)).lstrip("/")
    return {cgroup: uid, f"run/ginn/{uid}": uid}


def is_runsv_state(rel: str, user: str) -> bool:
    """True for `run/runit/supervise.ginn-<user>` or its `-log` twin, the runsv state of `user`."""
    path = PurePosixPath(rel)
    return path.parent == PurePosixPath(RUNIT_DIR) and path.name in (
        f"supervise.ginn-{user}",
        f"supervise.ginn-{user}-log",
    )


@dataclass(frozen=True)
class Host:
    """The root that every host path lies under, and the owner check that parents must pass.

    `delegated` maps the root-relative delegated directories of one user (`delegated_dirs`) to the
    uid that must own each of them. Every other parent must be owned by uid 0.
    """

    root: Path
    owner_of: OwnerOf
    delegated: Mapping[str, int] = field(default_factory=dict)


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
    """Resolve an account for setup. An unknown name, or a root account, is refused."""
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


def _probe(path: Path) -> tuple[str, int, str]:
    """Return (state, mode bits, link target) of `path`. A symbolic link is never followed.

    The state is absent, file, dir, symlink or other. The target is "" unless the state is
    symlink. A FIFO or device is `other`, so no caller opens it by mistake.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ("absent", 0, "")
    except NotADirectoryError:
        raise rec.unsafe(path, "has a parent that is not a directory") from None
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        return ("symlink", mode, os.readlink(path))
    if stat.S_ISDIR(st.st_mode):
        return ("dir", mode, "")
    if stat.S_ISREG(st.st_mode):
        return ("file", mode, "")
    return ("other", mode, "")


def _ensure_dir(root: Path, rel: str) -> list[str]:
    """Create `rel` and its missing parents under `root`, mode 0755. Return what it made.

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
            raise rec.unsafe(current, f"is a {state}, but a directory is needed")
        try:
            os.mkdir(current, DIR_MODE)
        except FileExistsError:
            if _probe(current)[0] != "dir":
                raise rec.unsafe(
                    current, "appeared as a non-directory while Host Setup ran"
                ) from None
            continue
        os.chmod(current, DIR_MODE)
        created.append("/".join(parts[: index + 1]))
    return created


def _check_parents(host: Host, rel: str) -> None:
    """Refuse a parent of `rel` that is a link or a file, or that a non-root user may change.

    Each existing parent must be a real directory, and not writable by group or others (the mode
    bits from lstat). Its owner must be uid 0, except a delegated directory of `host.delegated`,
    which must be owned by the uid that the record names (ADR-003, ADR-013 Decision 27). The owner
    comes through `host.owner_of`. The walk stops at the first missing parent, because setup
    creates every parent below it as root.
    """
    current = host.root
    prefix = ""
    for part in PurePosixPath(rel).parts[:-1]:
        current = current / part
        prefix = f"{prefix}/{part}" if prefix else part
        state, mode, _ = _probe(current)
        if state == "absent":
            return
        if state != "dir":
            raise rec.unsafe(current, f"is a {state}, but a directory is needed")
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise rec.unsafe(
                current,
                f"is writable by group or others (mode {mode:04o}), so Host Setup refuses to "
                "write under it; check who changed it, then correct the mode by hand",
            )
        owner = host.owner_of(current)
        if prefix in host.delegated:
            expected = host.delegated[prefix]
            if owner != expected:
                raise rec.unsafe(
                    current,
                    f"is owned by uid {owner}, but uid {expected}, the uid of the Host Setup "
                    "Record, must own it (ADR-003); check who changed it, then correct the owner "
                    "by hand",
                )
        elif owner != 0:
            raise rec.unsafe(
                current,
                f"is owned by uid {owner}, but uid 0 must own it; check who changed it, then "
                "correct the owner by hand",
            )


@contextmanager
def _global_lock(root: Path, timeout: float, *, ensure: bool) -> Iterator[set[str]]:
    """Hold /run/lock/ginn-host-setup.lock (ADR-013, Decision 11). Yields the directories made.

    With `ensure`, the lock directory is created when it is missing (setup). Without it, a missing
    directory is refused (undo), because undo creates nothing that it did not find. The lock file
    is not recorded.
    """
    created = set(_ensure_dir(root, LOCK_DIR)) if ensure else set()
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
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = os.open(path, flags)
    except FileNotFoundError:
        raise rec.unsafe(path, "its directory is missing, so the lock cannot be taken") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise rec.unsafe(path, "is a symbolic link, but a regular file is needed") from exc
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise rec.unsafe(path, "is not a regular file, but the lock needs one")
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
