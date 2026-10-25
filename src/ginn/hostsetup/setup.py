# SPDX-License-Identifier: Apache-2.0
"""One-time Host Setup for one user under a root prefix (FR-001, FR-002, FR-003, DR-009, NFR-030).

`run_setup` runs these steps. Nothing under the target paths is read or written before step 2.

1. The effective uid must be 0 (GINN-E-SETUP-ROOT). Then --user must be given (GINN-E-USAGE).
2. The user is resolved by name (GINN-E-NO-SUCH-USER). The root account is refused. The parents of
   the user's record are checked (`_check_parents`): each must be a real directory owned by uid 0
   and not writable by group or others.
3. The shared record etc/ginn/shared.json is loaded. When it is missing but etc/ginn holds a
   per-user record, the run refuses with GINN-E-SETUP-PATH-UNSAFE. It never recreates shared.json.
4. Idempotence, with no lock (FR-002). A complete per-user record whose setup-written paths still
   match their post-images (digest and mode), whose subordinate ID entries cover the recorded
   ranges and whose planned paths are all recorded, means "already configured". Then nothing is
   locked, created or written, shared.json included, not even a lock file. This reads only.
5. The global lock /run/lock/ginn-host-setup.lock is taken (GINN-E-SETUP-BUSY after 30 s). Under
   it the records are loaded and the check is repeated, because another run may have finished.
   A repair whose account uid, gid or home changed is refused (`_check_identity`). A new user is
   refused when another user's record holds its uid (`_refuse_uid_holder`). Both refusals come
   before any write, and undo of the other record or of this one comes first (ADR-013, Decisions 2
   and 24). The delegated directories of the user are owned by the user's uid (Decision 27).
   Directories that this acquisition creates are remembered, so their pre-image is "absent".
6. The plan is made. Pre-images are captured for every path setup will write and for each
   service-written path. Nothing is written yet. A repair keeps the stored pre-images, and captures
   a fresh pre-image of each newly planned path before writing it. Stale temporary names are
   removed.
7. The records are written. The first setup writes etc/ginn/shared.json and the per-user record,
   both into etc/.ginn.ginn-tmp, and then renames that directory to etc/ginn. A crash before the
   rename leaves only the temporary directory. A later setup rewrites shared.json only when it must
   add a shared directory (see `_plan`). The per-user record is written with status pending.
8. The planned directories are created, parents first. A directory that a repair finds with a
   changed mode gets its mode back.
9. Subordinate IDs for /etc/subuid, then /etc/subgid. The per-user record is written again, still
   pending.
10. The service scripts under etc/sv/ginn-<user> (run, finish, log/run).
11. The service link, last, so runsvdir sees a complete service. On Void the link goes into the
    directory that var/service resolves to, not into var/service itself.
12. The per-user record is written with status complete and the post-images.

The shared record and the per-user record split the planned paths. A path is per-user when one of
its components is the user's decimal uid, `ginn-<user>` or `setup-<user>.json`. Every other path is
shared (`_is_shared`, ADR-013 Decision 12). Components are compared whole, so a user named `ginn`
leaves etc/ginn shared. The shared record takes the shared paths other than the two subordinate ID
files, which every user's record keeps. A shared directory that the first setup creates goes into
shared.json with pre-image absent. An existing etc/ginn with no records and no shared.json is a
first setup: shared.json is written into it, and the directory is not recorded (Decision 20). When
a later user's setup must create a shared directory that is missing (for example etc/sv, after an
admin removed it), the directory is added to shared.json under the global lock, with its true
pre-image (absent), only if shared.json lacks that path. A later user's setup never changes an
existing shared entry, and never adds a shared entry to its own record. Shared entries carry
pre-images only, so a rerun does not check the mode of a shared directory (Decision 18).

A crash after any step leaves a pending record, or a temporary directory with no record. The next
run keeps the stored pre-images, reuses the stored subordinate ID range, and repeats the steps.

Void keeps /var/service as a symbolic link chain (ADR-013, Decision 10). Setup follows the chain
inside the root, hop by hop with readlink: a relative target resolves against the directory of its
link, and an absolute target is re-rooted under the root. The chain may be at most 40 hops long.
The chain resolution follows symbolic links at any component that it reaches while resolving the
var/service chain. The var component itself, and every other path, refuse symbolic links. The chain
must end at a real directory owned by uid 0, or the run refuses with GINN-E-SETUP-PATH-UNSAFE
before any write.

The service-written paths are the root cgroup subtree_control, the ginn cgroup directories, and
/run/ginn with its <uid> directory. The supervisor run script writes them when the service starts
(ADR-003). Setup does not write them, so it only captures their pre-images (ADR-013, Decision 12).

Every host file is placed under `root_dir`. The rendered script contents keep their host paths,
such as /run/ginn/<uid> and /etc/sv/ginn-<user>. Setup writes its own files with fixed temporary
names, so a SIGKILL leaves a predictable sibling for the next run to remove.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

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
from ginn.hostsetup._common import (
    DIR_MODE,
    LOCK_DIR,
    LOCK_TIMEOUT_SECONDS,
    RUNIT_DIR,
    SUBGID,
    SUBUID,
    Host,
    OwnerOf,
    UserInfo,
    UserLookup,
    _check_parents,
    _check_root,
    _ensure_dir,
    _global_lock,
    _owner_uid,
    _probe,
    _pwd_lookup,
    _resolve,
    delegated_dirs,
    supervise_links,
    user_component,
)
from ginn.hostsetup.cgroups import DEFAULT_CGROUP_ROOT, GINN_DIR, SUPERVISOR_LEAF
from ginn.store._time import now_iso

TMP_DIR = "etc/.ginn.ginn-tmp"
"""The directory the first records are written into, before it is renamed to etc/ginn."""
BOOT_ID = "proc/sys/kernel/random/boot_id"
LINK_PARENT = runit_system.LINK_PARENT
MAX_HOPS = 40
ALREADY_CONFIGURED = "already configured"
CONFIGURED = "configured"


FaultHook = Callable[[str], None]
"""Called with the name of each step after it finishes. Tests raise here to simulate a crash.

The steps are "tmpdir" (first setup only, after both records are in the temporary directory and
before its rename), "pending", "dirs", "subuid", "subgid", "subids", "sv", "symlink", "complete",
and "shared" (a later setup that added a shared directory, after shared.json is written).
"""
Clock = Callable[[], float]


@dataclass(frozen=True)
class SetupResult:
    """The outcome of one `run_setup` call. `paths` counts the per-user record's paths."""

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
    """What one run writes for `user`: the rendered scripts, the links and the home directory.

    `supervise_links` maps each runsv supervise link (relative to the root) to its absolute target
    under /run/runit. The links sit in the service directory and its log, so runsv creates its
    state in /run/runit, which is tmpfs and is not part of the service directory (ADR-003).
    """

    user: str
    uid: int
    gid: int
    home: str
    rendered: Mapping[str, str]
    link_rel: str
    supervise_links: Mapping[str, str]

    @property
    def leaves(self) -> dict[str, str]:
        """Every planned per-user file, link and append-only file, with its kind."""
        leaves = {
            rec.relpath(self.user): "file",
            SUBUID: "append-lines",
            SUBGID: "append-lines",
            self.link_rel: "symlink",
        }
        leaves.update({rel: "symlink" for rel in self.supervise_links})
        leaves.update({rel: "file" for rel in self.rendered})
        return leaves

    @property
    def write_paths(self) -> tuple[str, ...]:
        """Every path this run writes, so each can be swept of its stale temporary name."""
        return (
            rec.relpath(self.user),
            rec.SHARED_RELPATH,
            SUBUID,
            SUBGID,
            self.link_rel,
            *self.supervise_links,
            *self.rendered,
        )


@dataclass(frozen=True)
class _Plan:
    """What one run will write: the per-user record, the shared record, and the shared dirs."""

    user: rec.HostSetupRecord
    shared: rec.SharedRecord
    shared_changed: bool
    dirs: tuple[rec.PathEntry, ...]
    """The shared directories that this run must create, whether or not shared.json lists them."""


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
    gives epoch seconds for the records' timestamps. `fault` is called after each step. `owner_of`
    gives the owner of a real directory, and defaults to the real owner. `lock_timeout` is how long
    to wait for the global lock.
    """
    if (os.geteuid() if euid is None else euid) != 0:
        raise make("GINN-E-SETUP-ROOT")
    if user is None:
        raise make("GINN-E-USAGE", detail="--user NAME is required")
    root = _check_root(root_dir)
    info = _resolve(user, lookup_user or _pwd_lookup)
    host = Host(root, owner_of or _owner_uid, delegated_dirs(info.uid))
    own = rec.relpath(user)
    record_path = root / own
    shared_path = root / rec.SHARED_RELPATH
    _check_parents(host, own)
    current = rec.load(record_path)
    shared = rec.load_shared(shared_path)
    if current is not None and current.status == "complete" and shared is not None:
        if _intact(host, current, _target(host, user, info)):
            return _result(current, record_path, already=True)
    if shared is None and current is not None:
        _require_shared(host, shared)
    if current is not None:
        _check_identity(current, info, record_path)
    with _global_lock(host.root, lock_timeout, ensure=True) as created:
        current = rec.load(record_path)
        shared = rec.load_shared(shared_path)
        _require_shared(host, shared)
        target = _target(host, user, info)
        if current is not None and current.status == "complete":
            if _intact(host, current, target):
                return _result(current, record_path, already=True)
        if current is not None:
            _check_identity(current, info, record_path)
        if current is None:
            _refuse_uid_holder(host, user, info.uid)
        now = now_iso(clock)
        plan = _plan(host, target, current, shared, created, now)
        _guard(host, (*plan.user.paths, *plan.shared.paths))
        _sweep(host, target)
        _write_records(host, record_path, shared_path, plan, fault)
        _hit(fault, "pending")
        _make_planned_dirs(host, (*plan.dirs, *plan.user.paths))
        _hit(fault, "dirs")
        pending = replace(plan.user, subids=_subids_step(host, target, plan.user, fault))
        rec.save(record_path, pending)
        _hit(fault, "subids")
        _write_scripts(host, target)
        _link_supervise(host, target)
        _hit(fault, "sv")
        _link_service(host, target)
        _hit(fault, "symlink")
        done = replace(
            pending,
            status="complete",
            timestamp=now,
            paths=tuple(_post(host, entry, own) for entry in pending.paths),
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


def _render(user: str, info: UserInfo) -> dict[str, str]:
    home = PurePosixPath(info.home)
    xdg = {
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    return runit_system.render(user, info.uid, info.gid, info.home, xdg)


def _target(host: Host, user: str, info: UserInfo) -> _Target:
    """Plan the writes for `user`. Resolves the service chain, which reads and never writes."""
    service = _resolve_service_dir(host)
    return _Target(
        user=user,
        uid=info.uid,
        gid=info.gid,
        home=info.home,
        rendered=_render(user, info),
        link_rel=f"{service}/ginn-{user}",
        supervise_links=supervise_links(user),
    )


def _resolve_service_dir(host: Host) -> str:
    """Follow the var/service chain inside the root and return the directory the link goes into.

    `var` itself must be a real directory. From `service` on, each symbolic link that the chain
    reaches is read with readlink, so the kernel never follows one. A relative target resolves
    against the directory of its link, and an absolute target is re-rooted under the root. A `..`
    that would leave the root is refused, and so is a chain of more than MAX_HOPS hops. The chain
    must end at a real directory owned by uid 0, or it must be absent altogether, in which case
    setup creates it as a plain directory.
    """
    _check_parents(host, f"{PurePosixPath(LINK_PARENT).parts[0]}/x")
    pending = list(PurePosixPath(LINK_PARENT).parts)
    done: list[str] = []
    hops = 0
    missing = False
    while pending:
        part = pending.pop(0)
        if part == "..":
            if not done:
                raise _unsafe(host.root / LINK_PARENT, "its symbolic link chain leaves the root")
            done.pop()
            continue
        if missing:
            done.append(part)
            continue
        here = host.root.joinpath(*done, part)
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
        raise _unsafe(host.root / LINK_PARENT, "resolves to the root directory")
    rel = "/".join(done)
    if missing:
        return rel
    owner = host.owner_of(host.root / rel)
    if owner != 0:
        raise _unsafe(host.root / rel, f"is owned by uid {owner}, but uid 0 must own it")
    return rel


def _is_shared(rel: str, target: _Target) -> bool:
    """True unless a component of `rel` marks it per-user (ADR-013, Decision 12; `user_component`).

    The components are compared whole or as a `.ginn-<user>` suffix, never as a substring: uid 1001
    does not match `run/ginn/10010`, a user named `sv` does not make `etc/sv` per-user, and a user
    named `ginn` leaves `etc/ginn` shared. The two subordinate ID files are shared by path, and
    `_is_user_entry` keeps them in the per-user record.
    """
    return not any(
        user_component(part, target.user, target.uid) for part in PurePosixPath(rel).parts
    )


def _is_user_entry(entry: rec.PathEntry) -> bool:
    """True for an entry that the per-user record keeps: a user path, or a subordinate ID file."""
    return not entry.shared or entry.path in rec.SUBID_FILES


def _service_written(target: _Target) -> tuple[tuple[str, str], ...]:
    """The service-written paths and their kinds (ADR-013, Decision 12, and the runsv state).

    The two runsv state directories are created by runsv when the service starts, behind the
    supervise links, so their pre-images are captured here, before the links exist.
    """
    cgroup = PurePosixPath(str(DEFAULT_CGROUP_ROOT)).relative_to("/")
    ginn = cgroup / GINN_DIR
    user = ginn / str(target.uid)
    return (
        (f"{cgroup}/cgroup.subtree_control", "file"),
        (str(ginn), "dir"),
        (str(user), "dir"),
        (str(user / SUPERVISOR_LEAF), "dir"),
        ("run/ginn", "dir"),
        (f"run/ginn/{target.uid}", "dir"),
        (f"{RUNIT_DIR}/supervise.ginn-{target.user}", "dir"),
        (f"{RUNIT_DIR}/supervise.ginn-{target.user}-log", "dir"),
    )


def _service_entries(host: Host, target: _Target, *, shared: bool) -> list[rec.PathEntry]:
    """Capture the pre-images of the service-written paths of one classification (ADR-013, D12)."""
    entries: list[rec.PathEntry] = []
    for rel, kind in _service_written(target):
        if _is_shared(rel, target) != shared:
            continue
        pre = _read(host, rel, pre=True)
        _expect(host.root / rel, pre, kind)
        entries.append(rec.PathEntry(path=rel, kind=kind, pre=pre, shared=shared))
    return entries


def _own_entry() -> rec.PathEntry:
    """The shared record's own file: pre-image absent, and post-image self (ADR-013, Decision 9)."""
    return rec.PathEntry(
        path=rec.SHARED_RELPATH,
        kind="file",
        pre=rec.Image("absent"),
        post=rec.Image("self"),
        shared=True,
    )


def _require_shared(host: Host, shared: rec.SharedRecord | None) -> None:
    """Refuse when shared.json is missing but etc/ginn holds a per-user record (FR-001, DR-009).

    The pre-images of the shared paths exist only in shared.json, so it is never recreated here.
    With no per-user record in etc/ginn, a missing shared.json is simply written by the next setup.
    Reads only.
    """
    if shared is not None:
        return
    directory = host.root / rec.RECORD_DIR
    if _probe(directory)[0] != "dir":
        return
    names = sorted(name for name in os.listdir(directory) if rec.is_record_name(name))
    if names:
        raise _unsafe(
            host.root / rec.SHARED_RELPATH,
            f"is missing, but {rec.RECORD_DIR} holds the record {names[0]}; restore shared.json "
            "from a copy, and do not let setup recreate it",
        )


def _check_identity(current: rec.HostSetupRecord, info: UserInfo, path: Path) -> None:
    """Refuse a repair whose account uid, gid or home changed (ADR-013, Decisions 2 and 24).

    The service paths, the runtime directory and the cgroup leaves are named by the uid, so a repair
    under a new uid would store the new uid beside paths of the old one. The admin undoes first.
    Undo takes the identity from the record, so it works even when the account is gone. Reads only.
    """
    if (current.uid, current.gid, current.home) == (info.uid, info.gid, info.home):
        return
    raise _unsafe(
        path,
        f"was written for uid {current.uid}, gid {current.gid} and home {current.home}, but "
        f"account {current.user} now has uid {info.uid}, gid {info.gid} and home {info.home}; "
        f"the service paths still name uid {current.uid}, so run 'sudo ginn host-setup --undo "
        f"--user {current.user}' first, then set up again",
    )


def _refuse_uid_holder(host: Host, user: str, uid: int) -> None:
    """Refuse when the per-user record of another user already holds `uid` (ADR-013, Decision 24).

    A recycled uid, or a stale record of a deleted account, would make two records name the same
    /run/ginn/<uid> and cgroup leaves. The other record is undone first. Undo works for a deleted
    account too, because it reads the identity from the record. The caller runs this only for a
    user with no record yet, so a repair is not blocked by another record. Reads only.
    """
    directory = host.root / rec.RECORD_DIR
    if _probe(directory)[0] != "dir":
        return
    own = PurePosixPath(rec.relpath(user)).name
    for name in sorted(os.listdir(directory)):
        if name == own or not rec.is_record_name(name):
            continue
        other = rec.load(directory / name)
        if other is not None and other.uid == uid:
            raise _unsafe(
                directory / name,
                f"holds uid {uid}, which account {user} now has, so the record of {other.user} "
                f"must be undone first with 'sudo ginn host-setup --undo --user {other.user}'; "
                "undo works for a deleted account too",
            )


def _plan(
    host: Host,
    target: _Target,
    current: rec.HostSetupRecord | None,
    shared: rec.SharedRecord | None,
    created: set[str],
    now: str,
) -> _Plan:
    """Capture every pre-image this run needs, and build the records it will write. No writes.

    A path that is absent now, or that `created` names, is planned. An existing path is not
    recorded, because setup does not write it. A repair keeps the stored per-user pre-images.
    The shared record is built the first time (`shared` is None), and afterwards is only grown:
    a shared directory that this run must create is added when shared.json lacks it. A shared entry
    that shared.json already holds is never changed, and no shared entry goes into the per-user
    record.
    """
    kept = current.paths if current is not None else ()
    fresh = _entries(host, target, {entry.path for entry in kept}, created)
    user_fresh = [entry for entry in fresh if _is_user_entry(entry)]
    shared_fresh = [entry for entry in fresh if not _is_user_entry(entry)]
    if current is None:
        user = rec.HostSetupRecord(
            user=target.user,
            uid=target.uid,
            gid=target.gid,
            home=target.home,
            timestamp=now,
            status="pending",
            subids=None,
            paths=_sorted(user_fresh),
            service_paths=_sorted(_service_entries(host, target, shared=False)),
            boot_id=read_boot_id(host.root),
        )
    else:
        user = replace(
            current,
            uid=target.uid,
            gid=target.gid,
            home=target.home,
            status="pending",
            timestamp=now,
            paths=_sorted((*(replace(entry, post=None) for entry in kept), *user_fresh)),
        )
    if shared is None:
        # The subordinate ID files are kept here as they were at the first setup, which is the only
        # true original of each file (ADR-013, Decision 17). They are read before any write.
        originals = [
            replace(entry, shared=True, post=None)
            for entry in user_fresh
            if entry.path in rec.SUBID_FILES
        ]
        first = rec.SharedRecord(
            timestamp=now,
            paths=_sorted((_own_entry(), *shared_fresh, *originals)),
            service_paths=_sorted(_service_entries(host, target, shared=True)),
            boot_id=read_boot_id(host.root),
        )
        return _Plan(user, first, True, _sorted(shared_fresh))
    known = {entry.path for entry in shared.paths}
    additions = [entry for entry in shared_fresh if entry.path not in known]
    if not additions:
        return _Plan(user, shared, False, _sorted(shared_fresh))
    grown = replace(shared, paths=_sorted((*shared.paths, *additions)))
    return _Plan(user, grown, True, _sorted(shared_fresh))


def _intact(host: Host, current: rec.HostSetupRecord, target: _Target) -> bool:
    """True when a complete per-user record still describes the host (FR-002). Reads only."""
    if (current.uid, current.gid, current.home) != (
        target.uid,
        target.gid,
        target.home,
    ) or current.subids is None:
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
        if _read(host, entry.path, pre=False) != entry.post:
            return False
    return _covers(host, SUBUID, current.subids.uid) and _covers(host, SUBGID, current.subids.gid)


def _covers(host: Host, rel: str, entry: subids.SubIdEntry) -> bool:
    """True when the file has a line for the name that covers the recorded range (Decision 16)."""
    _check_parents(host, rel)
    state, _, _ = _probe(host.root / rel)
    if state != "file":
        return False
    text = (host.root / rel).read_bytes().decode("utf-8", errors="surrogateescape")
    return any(
        line.name == entry.name and line.start <= entry.start and line.end >= entry.end
        for line in subids.parse_entries(text)
    )


def _guard(host: Host, entries: Iterable[rec.PathEntry]) -> None:
    """Refuse before any write when a path a record lists is now of the wrong kind."""
    for entry in entries:
        _expect(host.root / entry.path, _read(host, entry.path, pre=True), entry.kind)


def _sweep(host: Host, target: _Target) -> None:
    """Remove the temporary names that an earlier run left behind (ADR-013, Decision 16).

    Runs under the lock, before the first write. Only the fixed names are removed. Nothing else in
    /etc is touched.
    """
    _check_parents(host, TMP_DIR)
    state, _, _ = _probe(host.root / TMP_DIR)
    if state == "dir":
        shutil.rmtree(host.root / TMP_DIR)
    elif state != "absent":
        raise _unsafe(host.root / TMP_DIR, f"is a {state}, but a directory is needed")
    for rel in target.write_paths:
        _check_parents(host, rel)
        tmp = fixed_tmp_path(host.root / rel)
        try:
            remove_stale_tmp(tmp)
        except IsADirectoryError as exc:
            raise _unsafe(tmp, "is a directory where Host Setup keeps a temporary file") from exc


def _write_records(
    host: Host,
    record_path: Path,
    shared_path: Path,
    plan: _Plan,
    fault: FaultHook | None,
) -> None:
    """Write the records. The first creation of etc/ginn goes through a temporary directory."""
    if _probe(record_path.parent)[0] == "absent":
        _first_records(host, record_path, shared_path, plan, fault)
        return
    if plan.shared_changed:
        rec.save_shared(shared_path, plan.shared)
        _hit(fault, "shared")
    rec.save(record_path, plan.user)


def _first_records(
    host: Host,
    record_path: Path,
    shared_path: Path,
    plan: _Plan,
    fault: FaultHook | None,
) -> None:
    """Write both records into etc/.ginn.ginn-tmp, then rename it to etc/ginn (ADR-013, D16).

    shared.json and the per-user record are in the directory before the rename, so etc/ginn never
    exists without shared.json. A crash before the rename leaves the temporary directory alone.
    """
    _ensure_dir(host.root, "etc")
    tmp = host.root / TMP_DIR
    os.mkdir(tmp, DIR_MODE)
    os.chmod(tmp, DIR_MODE)
    rec.save(tmp / record_path.name, plan.user)
    rec.save_shared(tmp / shared_path.name, plan.shared)
    _hit(fault, "tmpdir")
    os.rename(tmp, record_path.parent)
    sync_dir(host.root / "etc")


def _make_planned_dirs(host: Host, entries: Iterable[rec.PathEntry]) -> None:
    """Create the directories that the records planned, parents first, and restore their mode."""
    for entry in entries:
        if entry.kind == "dir" and entry.pre.state == "absent":
            _ensure_dir(host.root, entry.path)
            _, mode, _ = _probe(host.root / entry.path)
            if mode != DIR_MODE:
                os.chmod(host.root / entry.path, DIR_MODE)


def _subids_step(
    host: Host,
    target: _Target,
    pending: rec.HostSetupRecord,
    fault: FaultHook | None,
) -> rec.SubIds:
    """Set the ranges up for the first time, or reuse the recorded ones (FR-001, AS-005)."""
    if pending.subids is None:
        _, uid_entry = subids.ensure_single(
            host.root / SUBUID, target.user, root_dir=host.root, uid=target.uid
        )
        _hit(fault, "subuid")
        _, gid_entry = subids.ensure_single(
            host.root / SUBGID, target.user, root_dir=host.root, uid=target.gid
        )
        _hit(fault, "subgid")
        return rec.SubIds(uid=uid_entry, gid=gid_entry)
    subids.ensure_exact(host.root / SUBUID, pending.subids.uid, root_dir=host.root)
    _hit(fault, "subuid")
    subids.ensure_exact(host.root / SUBGID, pending.subids.gid, root_dir=host.root)
    _hit(fault, "subgid")
    return pending.subids


def _write_scripts(host: Host, target: _Target) -> None:
    """Write the rendered scripts with fixed temporary names. A matching script is left alone."""
    for rel in sorted(target.rendered):
        path = host.root / rel
        data = target.rendered[rel].encode("utf-8")
        image = _read(host, rel, pre=True)
        if image.state == "file" and image.data == data and image.mode == runit_system.SCRIPT_MODE:
            continue
        _expect(path, image, "file")
        atomic_write(path, data, runit_system.SCRIPT_MODE, fixed_tmp=True)


def _link_supervise(host: Host, target: _Target) -> None:
    """Point the two runsv supervise links at their state directories under /run/runit."""
    for rel, want in target.supervise_links.items():
        _point_link(host, rel, want)


def _link_service(host: Host, target: _Target) -> None:
    """Point the service link at /etc/sv/ginn-<user>, in the resolved directory."""
    _point_link(host, target.link_rel, f"/{runit_system.SV_PARENT}/ginn-{target.user}")


def _point_link(host: Host, rel: str, want: str) -> None:
    """Make the symbolic link `rel` point at `want`. A link that is already right is left alone."""
    _check_parents(host, rel)
    link = host.root / rel
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


def _entries(
    host: Host,
    target: _Target,
    have: set[str],
    created: set[str],
) -> list[rec.PathEntry]:
    """Return entries for the planned paths that are not in `have`, with fresh pre-images.

    A shared directory that existed before setup is not recorded, because setup does not write it.
    A per-user directory that setup writes into is recorded with its pre-image, present or absent,
    so that undo reports a pre-existing one rather than skipping it (ADR-013, Decision 15). A
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
        shared = _is_shared(rel, target)
        if rel in created:
            pre = rec.Image("absent")
        else:
            pre = _read(host, rel, pre=True)
            if pre.state == "dir" and shared:
                continue
            _expect(host.root / rel, pre, "dir")
        entries.append(rec.PathEntry(path=rel, kind="dir", pre=pre, shared=shared))
    for rel, kind in sorted(leaves.items()):
        if rel in have:
            continue
        if rel in created:
            pre = rec.Image("absent")
        else:
            pre = _read(host, rel, pre=True)
            _expect(host.root / rel, pre, kind)
        entries.append(rec.PathEntry(path=rel, kind=kind, pre=pre, shared=_is_shared(rel, target)))
    return entries


def _image(path: Path, *, pre: bool) -> rec.Image:
    """Return the state of `path` without following a symbolic link. Refuses other file types."""
    state, mode, target = _probe(path)
    if state == "absent":
        return rec.Image("absent")
    if state == "symlink":
        return rec.Image("symlink", target=target)
    if state == "dir":
        return rec.Image("dir", mode=mode)
    if state == "other":
        raise _unsafe(path, "is neither a regular file, a directory nor a symbolic link")
    data = path.read_bytes()
    if pre:
        return rec.Image("file", mode=mode, data=data)
    return rec.Image("file", mode=mode, sha256=hashlib.sha256(data).hexdigest())


def _read(host: Host, rel: str, *, pre: bool) -> rec.Image:
    """`_image` for a root-relative path, after `_check_parents` has checked its parents."""
    _check_parents(host, rel)
    return _image(host.root / rel, pre=pre)


def _expect(path: Path, image: rec.Image, kind: str) -> None:
    if image.state not in ("absent", rec.STATE_OF_KIND[kind]):
        raise _unsafe(path, f"is a {image.state}, but Host Setup needs a {kind} there")


def _post(host: Host, entry: rec.PathEntry, own: str) -> rec.PathEntry:
    if entry.path == own:
        return replace(entry, post=rec.Image("self"))
    return replace(entry, post=_read(host, entry.path, pre=False))


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
