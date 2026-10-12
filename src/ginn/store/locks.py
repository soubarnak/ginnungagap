# SPDX-License-Identifier: Apache-2.0
"""Per-realm and host-wide operation locks (FR-123, DR-033).

One mutating operation runs per realm at a time. A second one is refused at once and is never
queued: `flock(LOCK_EX | LOCK_NB)` on `paths.locks/<name>.lock`. After it acquires the lock the
holder writes `{"op", "pid", "started"}` into the file. A refused caller reads that record and
raises GINN-E-REALM-BUSY with the running operation, e.g. "rebuild in progress".

Lock files are never unlinked, because unlinking lets two holders lock different inodes. On
release the holder truncates the file before it unlocks, so a stale record is never read.

Lock names cannot collide with realm files. Realm names start with a letter (DR-002), so the
host-wide lock uses `_global` and the audit writer uses `_audit`.

The lock is not reentrant, and the store classes never take it themselves. The caller holds it
around every RealmStore, Journal and AuditLog call that belongs to one operation.
"""

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ginn.domain import RealmName
from ginn.errors import make
from ginn.store._time import now_iso
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths

GLOBAL_LOCK_NAME = "_global"
AUDIT_LOCK_NAME = "_audit"
_UNKNOWN_OP = "another operation"
_READ_ATTEMPTS = 5
_READ_PAUSE_SECONDS = 0.002


def _lock_path(paths: OwnedPaths, key: str) -> Path:
    paths.locks.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
    return paths.locks / f"{key}.lock"


def _holder_op(path: Path) -> str:
    """Return the op recorded by the current holder of `path`.

    The holder writes its record just after it acquires the lock, so an empty or partial file
    is possible for a moment. The read is retried briefly. If it still does not parse, the
    generic name is used. The lock refusal itself does not depend on this read.
    """
    for attempt in range(_READ_ATTEMPTS):
        try:
            record: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if isinstance(record, dict) and isinstance(record.get("op"), str):
            return str(record["op"])
        if attempt + 1 < _READ_ATTEMPTS:
            time.sleep(_READ_PAUSE_SECONDS)
    return _UNKNOWN_OP


@contextmanager
def _exclusive(paths: OwnedPaths, key: str, op: str) -> Iterator[None]:
    path = _lock_path(paths, key)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            running = _holder_op(path)
            raise make("GINN-E-REALM-BUSY", op=running) from None
        _write_holder(fd, op)
        try:
            yield
        finally:
            os.ftruncate(fd, 0)
            os.fsync(fd)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_holder(fd: int, op: str) -> None:
    record = {"op": op, "pid": os.getpid(), "started": now_iso(time.time)}
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    os.ftruncate(fd, 0)
    os.pwrite(fd, payload, 0)
    os.fsync(fd)


@contextmanager
def realm_lock(paths: OwnedPaths, name: RealmName, op: str) -> Iterator[None]:
    """Hold the lock for one mutating operation on realm `name`.

    Raises GINN-E-REALM-BUSY at once when another operation holds it. `op` is the name shown to
    the next caller, such as "rebuild" or "install".
    """
    with _exclusive(paths, name.value, op):
        yield


IMAGE_LOCK_PREFIX = "_image-"
"""Lock names for one distribution's image start with `_`, which no realm name can (DR-002)."""


@contextmanager
def image_lock(paths: OwnedPaths, distro: str, op: str) -> Iterator[None]:
    """Hold the lock for one distribution's image, around every create that pulls it.

    The rmi guard in ops/create.py (remove the image a create pulled, unless a container uses it)
    is only correct while no other create of the same distribution runs. This lock makes that so.
    A second holder is refused at once with GINN-E-REALM-BUSY, as a realm lock is (FR-123), so two
    creates of one distribution at the same moment cannot both pull and then undo each other. Take
    the image lock before the realm lock, always in that order.
    """
    with _exclusive(paths, f"{IMAGE_LOCK_PREFIX}{distro}", op):
        yield


@contextmanager
def global_lock(paths: OwnedPaths, op: str) -> Iterator[None]:
    """Hold the host-wide lock for operations such as integrity ack or host setup."""
    with _exclusive(paths, GLOBAL_LOCK_NAME, op):
        yield


def audit_lock_path(paths: OwnedPaths) -> Path:
    """Lock file that serialises audit appends and rotation (see store/audit.py)."""
    return _lock_path(paths, AUDIT_LOCK_NAME)
