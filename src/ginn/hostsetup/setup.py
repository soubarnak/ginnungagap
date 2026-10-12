# SPDX-License-Identifier: Apache-2.0
"""One-time Host Setup for one user (FR-001, FR-002; groundwork for FR-001 AC-1 and FR-003).

`run_setup` runs these steps in order. Nothing under the target paths is read or written before
steps 1 and 2.

1. The effective uid must be 0 (GINN-E-SETUP-ROOT).
2. The user is resolved by name (GINN-E-NO-SUCH-USER). The root account is refused.
3. Idempotence (FR-002). A complete record whose setup-written paths still match their
   post-images, and whose subordinate ID entries are present, means "already configured". Then
   nothing is locked, created or written, not even the record.
4. Pre-images are captured for every path setup will write, and for the two service-written
   paths. Nothing is written yet.
5. The record is written with status pending. This is the first write.
6. Subordinate IDs for /etc/subuid and /etc/subgid. The range is recorded again, still pending.
7. The service directory /etc/sv/ginn-<user> (run, finish, log/run).
8. The link /var/service/ginn-<user>, last, so runsvdir sees a complete service.
9. The record is written with status complete and the post-images.

A crash after any step leaves a pending record. The next run keeps the stored pre-images, reuses
the stored subordinate ID range, and repeats the steps. A repair that finds a changed path
rewrites it. The service-written paths are the root cgroup subtree_control and the ginn cgroup
directory. The supervisor run script writes them when the service starts (ADR-003). Setup does not
write them, so it only captures their pre-images (ADR-013).

Every host file is placed under `root_dir`. The rendered script contents keep their host paths,
such as /run/ginn/<uid> and /etc/sv/ginn-<user>. The record's own directory is created here, so
the user's runtime directory (ADR-004) is not required. Its absence does not stop setup.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from ginn.errors import GinnError, make
from ginn.hostsetup import record as rec
from ginn.hostsetup import runit_system, subids
from ginn.hostsetup.runit_system import LINK_PARENT, InstallPlan
from ginn.store._time import now_iso

LOCK_DIR = "run/lock"
SUBUID = "etc/subuid"
SUBGID = "etc/subgid"
SHARED_FILES: frozenset[str] = frozenset({SUBUID, SUBGID})
"""Shared files. Other users' setups append lines to them, so the check uses their entries."""
SERVICE_WRITTEN: tuple[tuple[str, str], ...] = (
    ("sys/fs/cgroup/cgroup.subtree_control", "file"),
    ("sys/fs/cgroup/ginn", "dir"),
)
"""Paths the supervisor run script writes at each service start, with their kinds (ADR-003)."""
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


def run_setup(
    user: str,
    *,
    root_dir: Path,
    euid: int | None = None,
    lookup_user: UserLookup | None = None,
    clock: Clock = time.time,
    fault: FaultHook | None = None,
) -> SetupResult:
    """Run Host Setup for `user` with every host path under `root_dir`. See the module docstring.

    `euid` defaults to the process's effective uid, and `lookup_user` to pwd.getpwnam. `clock`
    gives epoch seconds for the record's timestamp. `fault` is called after each step.
    """
    if (os.geteuid() if euid is None else euid) != 0:
        raise make("GINN-E-SETUP-ROOT")
    root = _check_root(root_dir)
    info = _resolve(user, lookup_user or _pwd_lookup)
    rendered = _render(user, info)
    own = rec.relpath(user)
    record_path = root / own
    current = rec.load(record_path)
    if current is not None and current.status == "complete" and _intact(root, current, info):
        return _result(current, record_path, already=True)
    now = now_iso(clock)
    if current is None:
        pending = _capture(root, user, info, rendered, now)
    else:
        # A repair keeps every pre-image. Post-images are unknown again until the record completes.
        kept = tuple(replace(entry, post=None) for entry in current.paths)
        pending = replace(
            current, uid=info.uid, gid=info.gid, status="pending", timestamp=now, paths=kept
        )
    _guard(root, pending)
    _mkdir_chain(root, rec.RECORD_DIR)
    rec.save(record_path, pending)
    _hit(fault, "pending")
    _make_planned_dirs(root, pending)
    pending = replace(pending, subids=_ensure_subids(root, user, info, pending))
    rec.save(record_path, pending)
    _hit(fault, "subids")
    runit_system.install_scripts(root, rendered)
    _hit(fault, "sv")
    runit_system.install_link(root, rendered, InstallPlan())
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


def _check_root(root_dir: Path) -> Path:
    if not root_dir.is_absolute():
        raise make("GINN-E-USAGE", detail=f"--root-dir must be an absolute path: {root_dir}")
    if not root_dir.is_dir():
        raise make("GINN-E-USAGE", detail=f"--root-dir is not a directory: {root_dir}")
    return root_dir


def _pwd_lookup(name: str) -> UserInfo:
    entry = pwd.getpwnam(name)
    return UserInfo(uid=entry.pw_uid, gid=entry.pw_gid, home=entry.pw_dir)


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


def _capture(
    root: Path,
    user: str,
    info: UserInfo,
    rendered: Mapping[str, str],
    now: str,
) -> rec.HostSetupRecord:
    """Read the pre-image of every path this run will write, and of each service path. No writes."""
    leaves: dict[str, str] = {rec.relpath(user): "file", SUBUID: "file", SUBGID: "file"}
    leaves.update({key: "file" for key in rendered})
    leaves[f"{LINK_PARENT}/ginn-{user}"] = "symlink"
    dirs: set[str] = {LOCK_DIR, *_ancestors(LOCK_DIR)}
    for rel in leaves:
        dirs.update(_ancestors(rel))
    entries: list[rec.PathEntry] = []
    for rel in sorted(dirs):
        state = _image(root / rel, pre=True).state
        if state == "absent":
            entries.append(rec.PathEntry(path=rel, kind="dir", pre=rec.Image("absent")))
        elif state != "dir":
            raise _unsafe(root / rel, f"is a {state}, but Host Setup needs a directory there")
    for rel, kind in sorted(leaves.items()):
        pre = _image(root / rel, pre=True)
        _expect(root / rel, pre, kind)
        entries.append(rec.PathEntry(path=rel, kind=kind, pre=pre))
    service: list[rec.PathEntry] = []
    for rel, kind in SERVICE_WRITTEN:
        pre = _image(root / rel, pre=True)
        _expect(root / rel, pre, kind)
        service.append(rec.PathEntry(path=rel, kind=kind, pre=pre))
    return rec.HostSetupRecord(
        user=user,
        uid=info.uid,
        gid=info.gid,
        timestamp=now,
        status="pending",
        subids=None,
        paths=tuple(sorted(entries, key=lambda entry: entry.path)),
        service_paths=tuple(service),
    )


def _guard(root: Path, record: rec.HostSetupRecord) -> None:
    """Refuse before any write when a path the record lists is now of the wrong kind."""
    for entry in record.paths:
        _expect(root / entry.path, _image(root / entry.path, pre=True), entry.kind)


def _intact(root: Path, current: rec.HostSetupRecord, info: UserInfo) -> bool:
    """True when a complete record still describes the host (FR-002). Reads only."""
    if (current.uid, current.gid) != (info.uid, info.gid) or current.subids is None:
        return False
    own = rec.relpath(current.user)
    for entry in current.paths:
        if entry.path == own or entry.path in SHARED_FILES:
            continue
        if entry.post is None or _image(root / entry.path, pre=False) != entry.post:
            return False
    return _has_entry(root / SUBUID, current.subids.uid) and _has_entry(
        root / SUBGID, current.subids.gid
    )


def _has_entry(path: Path, entry: subids.SubIdEntry) -> bool:
    try:
        text = path.read_bytes().decode("utf-8", errors="surrogateescape")
    except OSError:
        # Missing, or not a regular file. The repair then refuses the path (SETUP-PATH-UNSAFE).
        return False
    return entry in subids.parse_entries(text)


def _ensure_subids(
    root: Path,
    user: str,
    info: UserInfo,
    current: rec.HostSetupRecord,
) -> rec.SubIds:
    """Set up the ranges for the first time, or reuse the recorded ones (FR-001, AS-005).

    `uid=` matches a line by its number as well as by name. For /etc/subgid it carries the gid.
    """
    if current.subids is None:
        _, uid_entry = subids.ensure_single(root / SUBUID, user, root_dir=root, uid=info.uid)
        _, gid_entry = subids.ensure_single(root / SUBGID, user, root_dir=root, uid=info.gid)
        return rec.SubIds(uid=uid_entry, gid=gid_entry)
    subids.ensure_exact(root / SUBUID, current.subids.uid, root_dir=root)
    subids.ensure_exact(root / SUBGID, current.subids.gid, root_dir=root)
    return current.subids


def _post(root: Path, entry: rec.PathEntry, own: str) -> rec.PathEntry:
    if entry.path == own:
        return replace(entry, post=rec.Image("self"))
    return replace(entry, post=_image(root / entry.path, pre=False))


def _result(record: rec.HostSetupRecord, record_path: Path, *, already: bool) -> SetupResult:
    return SetupResult(
        user=record.user,
        uid=record.uid,
        gid=record.gid,
        record=str(record_path),
        already_configured=already,
        paths=len(record.paths),
    )


def _make_planned_dirs(root: Path, record: rec.HostSetupRecord) -> None:
    """Create the directories the record planned, parents first. Existing ones are kept."""
    for entry in record.paths:
        if entry.kind == "dir" and entry.pre.state == "absent":
            _mkdir_chain(root, entry.path)


def _mkdir_chain(root: Path, rel: str) -> None:
    """Create `rel` and any missing parents under `root`, each with mode 0755."""
    current = root
    for part in PurePosixPath(rel).parts:
        current = current / part
        if os.path.lexists(current):
            continue
        os.mkdir(current, DIR_MODE)
        os.chmod(current, DIR_MODE)


def _image(path: Path, *, pre: bool) -> rec.Image:
    """Return the state of `path` without following a symbolic link. Refuses other file types."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return rec.Image("absent")
    except NotADirectoryError:
        raise _unsafe(path, "has a parent that is not a directory") from None
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        return rec.Image("symlink", target=os.readlink(path))
    if stat.S_ISDIR(st.st_mode):
        return rec.Image("dir", mode=mode if pre else None)
    if stat.S_ISREG(st.st_mode):
        data = path.read_bytes()
        if pre:
            return rec.Image("file", mode=mode, data=data)
        return rec.Image("file", sha256=hashlib.sha256(data).hexdigest())
    raise _unsafe(path, "is neither a regular file, a directory nor a symbolic link")


def _expect(path: Path, image: rec.Image, kind: str) -> None:
    if image.state not in ("absent", kind):
        raise _unsafe(path, f"is a {image.state}, but Host Setup needs a {kind} there")


def _ancestors(rel: str) -> list[str]:
    parts = PurePosixPath(rel).parts
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _unsafe(path: Path, reason: str) -> GinnError:
    return make("GINN-E-SETUP-PATH-UNSAFE", path=str(path), reason=reason)


def _hit(fault: FaultHook | None, step: str) -> None:
    if fault is not None:
        fault(step)
