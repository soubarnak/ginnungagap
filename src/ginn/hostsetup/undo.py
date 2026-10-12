# SPDX-License-Identifier: Apache-2.0
"""Host Setup undo for one user (FR-003, DR-009, DR-010; ADR-013 Decisions 15, 17, 18, 20 to 26).

`run_undo` takes the global setup lock and works from the Host Setup Record of one user. It reverses
what `setup.run_setup` changed. It never writes shared bytes back (CL-11): the subordinate ID files
lose only the exact lines that setup appended, and cgroup controllers are disabled, not restored.
Each step ends with a fault hook, and every hook fires on every undo, whether or not the step has
work, so a crash can be simulated at any step. The steps run in this order (Decision 15 (v), with
F11 changes):

1. The service link, `etc/runit/runsvdir/.../ginn-<user>`. Removed when its pre-image is absent,
   restored when its pre-image was a symbolic link. Only this link is undone here.
2. Wait for runsv to exit. The waiter follows the supervise link of the service directory, with
   its absolute target re-rooted under the root directory (`_rooted_waiter`). The links are still in
   place, so the waiter can open `supervise/ok`. A regular file at `supervise/ok` is refused with
   GINN-E-UNDO-SUPERVISE-STATE (`_state_running` checks for a FIFO first). A timeout raises
   GINN-E-UNDO-SERVICE-RUNNING and leaves the record in place, so a rerun continues from here.
3. Per-user cleanup, after the wait confirmed the exit. First the other symbolic links (the runsv
   supervise links), each matched against its own target. Then the per-user service directories,
   leaf first; the runsv state directories under /run/runit, which hold only runit's names; the
   subordinate ID lines that setup appended (`subids.remove_exact`, which also reports every
   differing line of the user, on every undo); and the per-user files and directories, restored
   from their pre-images or removed.
4. Shared state. Applied only when this is the last record. Each shared directory whose pre-image
   is absent is removed with rmdir, leaf first. Then the root cgroup.subtree_control gets `-<ctrl>`
   for each controller that is present now and absent from its pre-image, and only in the boot that
   shared.json recorded; a refused write is reported. Then each subordinate ID file is settled
   against its original in shared.json (Decision 23). Then, when the rename path of Decision 25
   applies, /etc/ginn moves aside to /etc/.ginn.ginn-tmp, after `_holds_only_ours` is checked again.
   Otherwise shared.json is removed. When another record remains, every shared path that still
   exists is reported and left. If shared.json is already gone, the steps are skipped, and undo
   says so.
5. The user's own record is removed, and /etc/ginn is removed when shared.json listed it with
   pre-image absent (Decision 15 (v) step 6, Decision 20). On the rename path the temporary
   directory is emptied, and only the four names of Decision 16 are removed. Any other name is
   reported and kept. The rename path makes that removal crash-safe: a crash leaves either the whole
   directory, or a temporary directory that the next run sweeps under the lock.

Undo dispatches on each entry's `kind`, `shared` flag and pre-image state, never on path lists.
The record is the identity of the user: its uid, gid and home are read from the record only. The
account database is read only to warn when it no longer agrees with the record, and a missing
account still undoes. The record must name the user it is loaded for (`_load_own`). The two
delegated directories of the user must be owned by the record's uid (Decision 27). A path that is a
symbolic link is never followed.

The realm check reads the realm records under the recorded home, with the home re-rooted under the
root directory, and the walk opens each component with O_NOFOLLOW. A custom XDG_STATE_HOME is not
seen, because root cannot read the user's environment. That is a residual.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from ginn.errors import make
from ginn.hostsetup import record as rec
from ginn.hostsetup import runit_system, subids
from ginn.hostsetup._atomic import atomic_symlink, atomic_write, sync_dir
from ginn.hostsetup._common import (
    LOCK_TIMEOUT_SECONDS,
    SUBGID,
    SUBUID,
    Host,
    OwnerOf,
    UserInfo,
    UserLookup,
    _check_parents,
    _check_root,
    _global_lock,
    _owner_uid,
    _probe,
    _pwd_lookup,
    delegated_dirs,
    is_runsv_state,
    supervise_links,
)
from ginn.hostsetup.cgroups import DEFAULT_CGROUP_ROOT
from ginn.hostsetup.setup import TMP_DIR, read_boot_id
from ginn.store.paths import realms_dir_for_home

RECORD_DIR = rec.RECORD_DIR
SHARED_RELPATH = rec.SHARED_RELPATH
CGROUP_RELPATH = str(DEFAULT_CGROUP_ROOT).lstrip("/")
SERVICE_WAIT_SECONDS = 30.0
SERVICE_POLL_SECONDS = 0.1
UNDO_STEPS: tuple[str, ...] = (
    "link",
    "wait",
    "service",
    "subids",
    "files",
    "shared-dirs",
    "control",
    "settle",
    "shared",
    "record",
    "records-dir",
)
"""The fault points, in the order undo reaches them. Tests crash after each one.

Every point fires on every undo, including a non-last one, where the shared points do no work. On
the rename path, "shared" fires after /etc/ginn is moved aside, "record" after the own record is
removed from the temporary directory, and "records-dir" after that directory is gone.
"""
UNDONE = "undone"
NOT_CONFIGURED = "not configured"
REMOVE_RECORDS_DIR = "remove"
"""The plan for /etc/ginn when the rename path applies (Decision 25)."""
RUNSV_NAMES: frozenset[str] = frozenset({"control", "lock", "ok", "pid", "stat", "status"})
"""The entries that runsv keeps in its state directory (runit's own names)."""

REASON_SHARED = "shared, used by another record"
REASON_PRE_PRESENT = "pre-image present"
REASON_ADMIN_LINE = "admin line, present before setup, kept"
REASON_DIFFERING = "differing line kept, not the recorded range"
REASON_NOT_EMPTY = "ENOTEMPTY, the directory still holds entries that undo did not remove"
REASON_BOOT = "boot changed since setup, so the cgroup state is gone; controllers left"
REASON_BOOT_UNKNOWN = "boot id unknown, so cgroup controllers are not touched"
REASON_LINK = "symbolic link target is not ours, so it is left"
REASON_SYMLINK = "symbolic link, not followed"
REASON_OTHER_LINES = "subordinate ID line of another record or the admin, not in the original"
REASON_PRE_EXISTING = "pre-existing, setup did not create it, so undo leaves it"
REASON_SHARED_GONE = "shared.json is already gone, so undo cannot confirm setup created it"
REASON_RECORDS_DIR = "holds no record and no shared.json, so it is left"
REASON_RECORDS_EXTRA = "holds entries that setup did not write, so it is left"
REASON_RUNSV_EXTRA = "runsv state holds entries that runit does not keep, so it is left"
REASON_ORIGINAL_GONE = "the original of this file is not in shared.json, so the file is kept"
REASON_TERMINATOR = "differs from the original only in a line terminator, so it is kept"
REASON_ORIGINAL_BYTES = (
    "differs from its original bytes in more than its lines, and undo never writes bytes back, "
    "so it is kept"
)

FaultHook = Callable[[str], None]
ServiceWait = Callable[[Path, float], None]
RealmNames = Callable[[Path, str], Sequence[str]]
"""Lists the realm names under a recorded home, re-rooted under the root (tests inject it)."""
RemoveDir = Callable[[Path], None]
WriteControl = Callable[[Path, str], None]


@dataclass(frozen=True)
class LeftPath:
    """A path that undo left in place, and why (CL-11)."""

    path: str
    reason: str


@dataclass(frozen=True)
class UndoResult:
    """The outcome of one `run_undo` call. `left` is information, not a failure."""

    user: str
    record: str
    status: str
    left: tuple[LeftPath, ...]
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "user": self.user,
            "record": self.record,
            "left": [{"path": item.path, "reason": item.reason} for item in self.left],
            "notes": list(self.notes),
        }

    def render(self) -> str:
        if self.status == NOT_CONFIGURED:
            head = f"{self.user}: not configured; nothing changed"
        else:
            head = f"{self.user}: host setup undone; {len(self.left)} path(s) left in place"
        lines = [head]
        lines.extend(f"left {item.path}: {item.reason}" for item in self.left)
        lines.extend(f"note: {text}" for text in self.notes)
        return "\n".join(lines)


@dataclass
class _Ctx:
    """What one undo run needs: the host, the injected effects, and what it has reported."""

    host: Host
    user: str
    fault: FaultHook | None
    wait: ServiceWait
    rmdir_cgroup: RemoveDir
    write_control: WriteControl
    left: list[LeftPath]
    notes: list[str]

    @property
    def root(self) -> Path:
        return self.host.root

    def path(self, rel: str) -> Path:
        """Return root/rel after checking every parent (`_check_parents`). Never follows rel."""
        _check_parents(self.host, rel)
        return self.root / rel

    def leave(self, rel: str, reason: str) -> None:
        self.left.append(LeftPath(rel, reason))

    def hit(self, step: str) -> None:
        if self.fault is not None:
            self.fault(step)

    def unlink(self, rel: str) -> None:
        path = self.path(rel)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        sync_dir(path.parent)


def run_undo(
    user: str | None,
    *,
    root_dir: Path,
    euid: int | None = None,
    lookup_user: UserLookup | None = None,
    realm_names: RealmNames | None = None,
    fault: FaultHook | None = None,
    wait_service: ServiceWait | None = None,
    rmdir_cgroup: RemoveDir | None = None,
    write_control: WriteControl | None = None,
    owner_of: OwnerOf | None = None,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> UndoResult:
    """Undo Host Setup for `user` with every host path under `root_dir`. See the module docstring.

    The root check runs first and reads no target path. The record is loaded before the lock, so
    its recorded home can be checked for realms. A user with realms is refused with nothing changed,
    and the check runs again under the lock. `realm_names` lists the realms under a recorded home
    (tests inject it), `wait_service` waits for runsv, `rmdir_cgroup` removes a cgroup directory and
    `write_control` writes one controller change. Tests inject the last two, because a plain rmdir
    of a tmp_path directory with files does not behave like cgroupfs. `owner_of` gives the owner of
    a real directory, which every parent must have (uid 0).
    """
    if (os.geteuid() if euid is None else euid) != 0:
        raise make("GINN-E-SETUP-ROOT")
    if user is None:
        raise make("GINN-E-USAGE", detail="--user NAME is required")
    if rec.USER_NAME.fullmatch(user) is None:
        # The same name rule as setup (`_resolve`), checked before any path is built from it.
        raise make("GINN-E-NO-SUCH-USER", user=user)
    root = _check_root(root_dir)
    host = Host(root, owner_of or _owner_uid)
    names: RealmNames = realm_names or _realm_names
    _check_parents(host, rec.relpath(user))
    first = _load_own(root, user)
    if first is None and _probe(root / TMP_DIR)[0] == "absent":
        return _not_configured(root, user)
    if first is not None:
        _refuse_realms(root, user, first.home, names)
    with _global_lock(root, lock_timeout, ensure=False):
        current = _load_own(root, user)
        if current is None:
            _sweep_tmp(host)
            return _not_configured(root, user)
        _refuse_realms(root, user, current.home, names)
        # The delegated directories are owned by the record's uid, never by the current account
        # (ADR-003, ADR-013 Decision 27).
        host = replace(host, delegated=delegated_dirs(current.uid))
        _sweep_tmp(host)
        ctx = _Ctx(
            host=host,
            user=user,
            fault=fault,
            wait=wait_service or _rooted_waiter(root),
            rmdir_cgroup=rmdir_cgroup or os.rmdir,
            write_control=write_control or _write_control,
            left=[],
            notes=_account_notes(user, current, lookup_user or _pwd_lookup),
        )
        return _undo_record(ctx, current)


def wait_until_stopped(
    svdir: Path, seconds: float, *, running: Callable[[Path], bool] | None = None
) -> None:
    """Poll until `running` reports that runsv has exited, or raise after `seconds`."""
    check = runsv_running if running is None else running
    deadline = time.monotonic() + seconds
    while check(svdir):
        if time.monotonic() >= deadline:
            raise make(
                "GINN-E-UNDO-SERVICE-RUNNING",
                user=svdir.name.removeprefix("ginn-"),
                seconds=f"{seconds:g}",
            )
        time.sleep(SERVICE_POLL_SECONDS)


def runsv_running(svdir: Path) -> bool:
    """True while runsv serves `svdir`, through its `supervise` entry as the host resolves it.

    Undo's own waiter does not use this function: `_rooted_waiter` re-roots the link first. See
    `_state_running` for the check itself.
    """
    return _state_running(svdir / "supervise", svdir.name.removeprefix("ginn-"))


def _state_running(state: Path, user: str) -> bool:
    """True while runsv serves the state directory `state`. runit keeps `ok` open for reading then.

    `ok` must be a FIFO. The check is the lstat of `ok`, so a regular file is refused with
    GINN-E-UNDO-SUPERVISE-STATE before any open. Without the check, a write-only open would succeed
    on a regular file and the waiter would run to its timeout. A write-only open with O_NONBLOCK
    fails with ENXIO when nothing reads the FIFO, which is how `sv` checks a service. A missing
    state directory means runsv is not running.
    """
    ok = state / "ok"
    try:
        st = os.lstat(ok)
    except (FileNotFoundError, NotADirectoryError):
        return False
    if not stat.S_ISFIFO(st.st_mode):
        raise make(
            "GINN-E-UNDO-SUPERVISE-STATE",
            path=str(ok),
            state=_kind_word(st.st_mode),
            user=user,
        )
    try:
        fd = os.open(ok, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ENOENT, errno.ENOTDIR):
            return False
        raise
    os.close(fd)
    return True


def _kind_word(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symbolic link"
    return "special file"


def _rooted_waiter(root: Path) -> ServiceWait:
    """The default waiter: it follows the supervise link of the service, re-rooted under `root`.

    The link's absolute target (/run/runit/supervise.ginn-<user>) is read with readlink and taken
    under `root`, so a test or an alternate root never reaches the real /run/runit. A missing link
    or target means runsv is not running, and the wait returns at once.
    """

    def wait(svdir: Path, seconds: float) -> None:
        state = _state_under_root(root, svdir)
        if state is None:
            return
        user = svdir.name.removeprefix("ginn-")
        wait_until_stopped(svdir, seconds, running=lambda _svdir: _state_running(state, user))

    return wait


def _state_under_root(root: Path, svdir: Path) -> Path | None:
    """Return the runsv state directory that the supervise link of `svdir` names, under `root`.

    The link is read with readlink and never followed. Its target must be absolute and free of
    `..`, and it is taken under `root`. Each component on the way must be a real directory, so no
    symbolic link leads out of the root: a link or any other file there is refused, as
    `_check_parents` refuses it. None when the link or a component of its target is missing.
    """
    link = svdir / "supervise"
    state, _, text = _probe(link)
    if state == "absent":
        return None
    if state != "symlink":
        return link
    parts = PurePosixPath(text).parts
    if not text.startswith("/") or ".." in parts:
        raise rec.unsafe(link, f"points at {text}, which is not a path under the root")
    current = root
    for part in parts[1:]:
        current = current / part
        step, _, _ = _probe(current)
        if step == "absent":
            return None
        if step != "dir":
            raise rec.unsafe(current, f"is a {step}, but a directory is needed")
    return current


def _undo_record(ctx: _Ctx, current: rec.HostSetupRecord) -> UndoResult:
    shared = rec.load_shared(ctx.root / SHARED_RELPATH)
    last = _is_last(ctx.root, ctx.user)
    records = _records_dir_plan(ctx.root, ctx.user, shared, last)
    own = rec.relpath(ctx.user)
    # The shared steps of Decision 15 (v) step 4 run only for the last record, and only while
    # shared.json exists. An absent shared.json means an earlier undo already ran them.
    final = shared if last else None

    for entry in current.paths:
        if entry.kind == "symlink" and _is_service_link(entry, ctx.user):
            _undo_link(ctx, entry)
    ctx.hit("link")

    ctx.wait(ctx.root / runit_system.SV_PARENT / f"ginn-{ctx.user}", SERVICE_WAIT_SECONDS)
    ctx.hit("wait")

    for entry in current.paths:
        if entry.kind == "symlink" and not _is_service_link(entry, ctx.user):
            _undo_link(ctx, entry)
    for entry in _deepest_first(e for e in current.service_paths if not e.shared):
        _undo_service_dir(ctx, entry)
    ctx.hit("service")

    reported: dict[str, set[subids.SubIdEntry]] = {}
    for entry in current.paths:
        if entry.kind == "append-lines":
            reported[entry.path] = _undo_subids(ctx, current, entry, last)
    ctx.hit("subids")

    for entry in current.paths:
        if entry.kind == "file" and entry.path != own:
            _undo_file(ctx, entry)
    for entry in _deepest_first(e for e in current.paths if e.kind == "dir"):
        _undo_dir(ctx, entry)
    ctx.hit("files")

    if shared is None and last:
        # The shared steps were applied by an earlier undo, so they are skipped (Decision 15 (v)).
        # Any subordinate ID file that is still here is reported, because its original is gone.
        ctx.notes.append("shared.json is already gone; an earlier undo applied the shared steps")
        for entry in current.paths:
            # A file that step 3 already reported keeps that reason, so it is not reported twice.
            if entry.kind != "append-lines" or any(item.path == entry.path for item in ctx.left):
                continue
            if _probe(ctx.path(entry.path))[0] != "absent":
                ctx.leave(entry.path, REASON_ORIGINAL_GONE)
    elif shared is not None and not last:
        _report_shared(ctx, shared)
    if final is not None:
        _undo_shared_dirs(ctx, final)
    ctx.hit("shared-dirs")

    if final is not None:
        _undo_controllers(ctx, final)
    ctx.hit("control")

    if final is not None:
        for entry in current.paths:
            if entry.kind == "append-lines":
                _settle_subid_file(ctx, entry.path, final, reported[entry.path])
    ctx.hit("settle")

    renamed = False
    if final is not None:
        if records == REMOVE_RECORDS_DIR:
            renamed = _move_records_dir_aside(ctx, ctx.user)
            if not renamed:
                records = REASON_RECORDS_EXTRA
        if not renamed:
            ctx.unlink(SHARED_RELPATH)
    ctx.hit("shared")

    if renamed:
        _remove_records_dir(ctx, own)
    else:
        ctx.unlink(own)
        ctx.hit("record")
        if records is not None and _probe(ctx.path(RECORD_DIR))[0] != "absent":
            ctx.leave(RECORD_DIR, records)
        ctx.hit("records-dir")

    return UndoResult(
        user=ctx.user,
        record=str(ctx.root / own),
        status=UNDONE,
        left=tuple(ctx.left),
        notes=tuple(ctx.notes),
    )


def _records_dir_plan(
    root: Path, user: str, shared: rec.SharedRecord | None, last: bool
) -> str | None:
    """Decide the fate of /etc/ginn before step 4 changes anything (Decisions 20 and 25).

    Returns None when this is not the last record. Returns REMOVE_RECORDS_DIR for the rename path,
    which needs both conditions: shared.json lists /etc/ginn with pre-image absent, and the
    directory holds only shared.json, the own record and the fixed temporary names. Otherwise it
    returns the reason to leave the directory, and the fallback path removes shared.json and the
    own record.
    """
    if not last:
        return None
    if shared is None:
        return REASON_SHARED_GONE
    for entry in shared.paths:
        if entry.path == RECORD_DIR:
            if entry.kind != "dir" or entry.pre.state != "absent":
                return REASON_PRE_EXISTING
            return REMOVE_RECORDS_DIR if _holds_only_ours(root, user) else REASON_RECORDS_EXTRA
    return REASON_PRE_EXISTING


def _holds_only_ours(root: Path, user: str) -> bool:
    """True when etc/ginn holds only shared.json, the own record and the temporary names."""
    directory = root / RECORD_DIR
    if _probe(directory)[0] != "dir":
        return False
    own = PurePosixPath(rec.relpath(user)).name
    allowed = {
        rec.SHARED_NAME,
        own,
        f".{rec.SHARED_NAME}.ginn-tmp",
        f".{own}.ginn-tmp",
    }
    names = set(os.listdir(directory))
    if not {rec.SHARED_NAME, own} <= names or not names <= allowed:
        return False
    return all(_probe(directory / name)[0] == "file" for name in names)


def _load_own(root: Path, user: str) -> rec.HostSetupRecord | None:
    """Load the per-user record of `user`, which must name that user (Decision 24).

    A record whose `user` field names someone else is refused with GINN-E-SETUP-PATH-UNSAFE. Undo
    would otherwise act on another user's uid, home and paths.
    """
    path = root / rec.relpath(user)
    loaded = rec.load(path)
    if loaded is not None and loaded.user != user:
        raise rec.unsafe(
            path, f"holds the record of user {loaded.user}, not {user}, so undo does not act on it"
        )
    return loaded


def _is_service_link(entry: rec.PathEntry, user: str) -> bool:
    """True for the service link: the entry named ginn-<user>, in the runsvdir directory."""
    return PurePosixPath(entry.path).name == f"ginn-{user}"


def _link_target(entry: rec.PathEntry, user: str) -> str | None:
    """The target that this symbolic link should have now (Decision 22, TRAP 1).

    The recorded post-image target when the record is complete. A pending record has no post-image,
    so the target is derived from the user name, as setup derives it (`supervise_links`). None means
    no target is known, and the link is left.
    """
    if entry.post is not None and entry.post.state == "symlink" and entry.post.target is not None:
        return entry.post.target
    if _is_service_link(entry, user):
        return f"/{runit_system.SV_PARENT}/ginn-{user}"
    return supervise_links(user).get(entry.path)


def _undo_link(ctx: _Ctx, entry: rec.PathEntry) -> None:
    """Remove a symbolic link whose pre-image is absent, or restore the pre-image target.

    A link is matched against its own target (`_link_target`), so a link that somebody else
    repointed is left and reported. Used for the service link in step 1 and for the runsv supervise
    links in step 3.
    """
    rel = entry.path
    want = _link_target(entry, ctx.user)
    path = ctx.path(rel)
    state, _, target = _probe(path)
    if entry.pre.state == "absent":
        if state == "absent":
            return
        if state == "symlink" and want is not None and target == want:
            ctx.unlink(rel)
            return
        ctx.leave(rel, REASON_LINK if state == "symlink" else _state_reason(state))
        return
    if state == "symlink" and target == entry.pre.target:
        return
    matched = state == "symlink" and want is not None and target == want
    if (state == "absent" or matched) and entry.pre.target is not None:
        atomic_symlink(path, entry.pre.target)
        return
    ctx.leave(rel, REASON_LINK if state == "symlink" else _state_reason(state))


def _undo_service_dir(ctx: _Ctx, entry: rec.PathEntry) -> None:
    """Step 3, per-user service paths. Removed only when their pre-image is absent."""
    rel = entry.path
    if entry.kind != "dir":
        ctx.leave(rel, f"service path of kind {entry.kind} is not undone")
        return
    state = _probe(ctx.path(rel))[0]
    if entry.pre.state != "absent":
        if state != "absent":
            ctx.leave(rel, REASON_PRE_PRESENT)
        return
    if state == "absent":
        return
    if is_runsv_state(rel, ctx.user):
        _remove_runsv_dir(ctx, rel)
    elif _is_cgroup(rel):
        _remove_dir(ctx, rel)
    else:
        _remove_tree(ctx, rel)


def _remove_runsv_dir(ctx: _Ctx, rel: str) -> None:
    """Remove runsv's state directory only when it holds runit's names and nothing else.

    runsv creates `supervise.ginn-<user>` behind the supervise link, with its own names (control,
    lock, ok, pid, stat, status). A symbolic link at the top, a subdirectory, or any other name
    means something else is there, so the directory is left and the names are reported.
    """
    path = ctx.path(rel)
    state = _probe(path)[0]
    if state != "dir":
        ctx.leave(rel, REASON_SYMLINK if state == "symlink" else _state_reason(state))
        return
    names = sorted(os.listdir(path))
    odd = [
        name
        for name in names
        if name not in RUNSV_NAMES or _probe(path / name)[0] not in ("file", "other")
    ]
    if odd:
        ctx.leave(rel, f"{REASON_RUNSV_EXTRA}: {', '.join(odd)}")
        return
    for name in names:
        ctx.unlink(f"{rel}/{name}")
    _remove_dir(ctx, rel)


def _undo_subids(
    ctx: _Ctx,
    current: rec.HostSetupRecord,
    entry: rec.PathEntry,
    last: bool,
) -> set[subids.SubIdEntry]:
    """Step 3, subordinate ID lines (Decision 15 (i)). Never restores the file whole.

    Setup's own exact line is removed when the line is absent from this record's pre-image. A line
    of the user that the pre-image holds is an admin line, and it is kept. The last record reports
    it. A same-name line with another range is kept and reported on every undo (Decision 15 (i)).
    Returns the lines that were kept and reported, so step 4 does not report them again.
    """
    rel = entry.path
    pre_lines = _pre_lines(entry.pre)
    recorded = _recorded_range(current, rel)
    if current.subids is None:
        # A pending record without ranges: the only lines setup can have appended are this user's
        # lines that the pre-image lacks (Decision 15, pre-image and current state).
        now = _read_lines(ctx.path(rel))
        candidates = [line for line in now if line.name == ctx.user and line not in pre_lines]
    else:
        candidates = [] if recorded is None else [recorded]
    reported: set[subids.SubIdEntry] = set()
    for line in candidates:
        if line in pre_lines:
            reported.add(line)
            if last:
                ctx.leave(rel, f"{REASON_ADMIN_LINE}: {line.render()}")
            continue
        removal = subids.remove_exact(
            ctx.path(rel), line.name, line.start, line.count, root_dir=ctx.root
        )
        for kept in removal.kept_differing:
            reported.add(kept)
            ctx.leave(rel, f"{REASON_DIFFERING}: {kept.render()}")
    return reported


def _settle_subid_file(
    ctx: _Ctx,
    rel: str,
    shared: rec.SharedRecord,
    reported: set[subids.SubIdEntry],
) -> None:
    """Settle one subordinate ID file once every record's own lines are gone (Decision 23).

    Step 4 of the last record calls this, after the shared directories and the controllers. The
    file is compared with its original, which shared.json holds: the state at the first setup. The
    file is deleted only when the original was absent and the file is now empty. A line present now
    and absent from the original belongs to another record or to the admin, and it is reported
    line by line, unless step 3 already reported it. When the lines match but the bytes do not,
    the file is kept and reported, because undo never writes bytes back. A file that step 3 or this
    function already reported is not reported twice.
    """
    path = ctx.path(rel)
    state = _probe(path)[0]
    if state == "absent":
        return
    if state != "file":
        ctx.leave(rel, _state_reason(state))
        return
    data = path.read_bytes()
    original = _original(shared, rel)
    if original is None:
        ctx.leave(rel, REASON_ORIGINAL_GONE)
        return
    if original.state == "absent" and not data.strip():
        ctx.unlink(rel)
        return
    original_lines = _pre_lines(original)
    for line in _parse_bytes(data):
        if line in original_lines or line in reported:
            continue
        ctx.leave(rel, f"{REASON_OTHER_LINES}: {line.render()}")
    original_bytes = b""
    if original.state == "file" and original.data is not None:
        original_bytes = original.data
    if data != original_bytes and not any(item.path == rel for item in ctx.left):
        if data.splitlines() == original_bytes.splitlines():
            ctx.leave(rel, REASON_TERMINATOR)
        else:
            ctx.leave(rel, REASON_ORIGINAL_BYTES)


def _original(shared: rec.SharedRecord, rel: str) -> rec.Image | None:
    """The original state of a subordinate ID file, from shared.json, or None when it is unknown.

    The originals are the `paths` entries of shared.json for etc/subuid and etc/subgid, with kind
    append-lines and the pre-image that the first setup captured (ADR-013, Decision 23).
    """
    for entry in shared.paths:
        if entry.path == rel and entry.kind == "append-lines":
            return entry.pre
    return None


def _undo_file(ctx: _Ctx, entry: rec.PathEntry) -> None:
    """Step 3, a per-user file. Restored from its pre-image, or removed when it had none."""
    rel = entry.path
    path = ctx.path(rel)
    state, mode, _ = _probe(path)
    pre = entry.pre
    if pre.state == "absent":
        if state == "file":
            ctx.unlink(rel)
        elif state != "absent":
            ctx.leave(rel, _state_reason(state))
        return
    if pre.state != "file" or pre.data is None or pre.mode is None:
        ctx.leave(rel, f"pre-image is {pre.state}, so it is not undone")
        return
    if state == "file" and mode == pre.mode and path.read_bytes() == pre.data:
        return
    if state in ("absent", "file"):
        atomic_write(path, pre.data, pre.mode, fixed_tmp=True)
        return
    ctx.leave(rel, _state_reason(state))


def _undo_dir(ctx: _Ctx, entry: rec.PathEntry) -> None:
    """Step 3, a per-user directory. Removed leaf first when its pre-image is absent.

    A directory that existed before setup is never removed. Its mode is restored and it is reported
    as pre-image present, so a pre-existing directory is never skipped silently (Decision 21).
    """
    rel = entry.path
    if entry.pre.state == "absent":
        _remove_dir(ctx, rel)
        return
    path = ctx.path(rel)
    state, mode, _ = _probe(path)
    if state == "absent":
        return
    if state != "dir":
        ctx.leave(rel, _state_reason(state))
        return
    if entry.pre.mode is not None and mode != entry.pre.mode:
        os.chmod(path, entry.pre.mode)
    ctx.leave(rel, REASON_PRE_PRESENT)


def _report_shared(ctx: _Ctx, shared: rec.SharedRecord) -> None:
    """Another record remains, so every shared path that still exists is left and reported."""
    for entry in (*shared.paths, *shared.service_paths):
        if _probe(ctx.path(entry.path))[0] != "absent":
            ctx.leave(entry.path, REASON_SHARED)


def _undo_shared_dirs(ctx: _Ctx, shared: rec.SharedRecord) -> None:
    """Step 4 (iii): the shared directories, leaf first, before the root controllers are written.

    cgroup v2 refuses to disable a controller at a parent while a child cgroup still enables it, so
    the root controllers are written only after the cgroup directories are gone (Decision 15 (ii)).
    The records directory is not in this list: `_undo_record` removes it, on either path.
    """
    dirs = [
        entry
        for entry in (*shared.paths, *shared.service_paths)
        if entry.kind == "dir" and entry.path != RECORD_DIR
    ]
    for entry in _deepest_first(dirs):
        _undo_shared_dir(ctx, entry)


def _undo_controllers(ctx: _Ctx, shared: rec.SharedRecord) -> None:
    """Step 4 (ii): disable the controllers that the run script enabled, in the same boot only."""
    boot_now = read_boot_id(ctx.root)
    for entry in shared.service_paths:
        if entry.kind == "file":
            _disable_controllers(ctx, entry, shared.boot_id, boot_now)


def _move_records_dir_aside(ctx: _Ctx, user: str) -> bool:
    """Rename /etc/ginn to /etc/.ginn.ginn-tmp, then flush /etc (Decision 25). False if not renamed.

    The plan checked `_holds_only_ours` before any change. It is checked again here, right before
    the rename, because an entry may have appeared since. On False nothing is renamed, and the
    caller takes the fallback path. The temporary name is swept before the run starts, so the rename
    cannot land on a leftover.
    """
    if not _holds_only_ours(ctx.root, user):
        return False
    source = ctx.path(RECORD_DIR)
    target = ctx.path(TMP_DIR)
    if _probe(target)[0] != "absent":
        raise rec.unsafe(target, "is still here, so etc/ginn cannot be moved aside")
    os.rename(source, target)
    sync_dir(source.parent)
    return True


def _remove_records_dir(ctx: _Ctx, own: str) -> None:
    """Empty the moved-aside directory and remove it, after the four names of Decision 16 only.

    shared.json goes first and the own record last. Any other name is reported with
    REASON_RECORDS_EXTRA and left, and then the directory is not removed. A crash after the rename
    leaves the directory, which the next run's sweep removes under the lock.
    """
    own_name = PurePosixPath(own).name
    allowed = {
        rec.SHARED_NAME,
        own_name,
        f".{rec.SHARED_NAME}.ginn-tmp",
        f".{own_name}.ginn-tmp",
    }
    names = set(os.listdir(ctx.path(TMP_DIR)))
    extra = sorted(names - allowed)
    for name in extra:
        ctx.leave(f"{TMP_DIR}/{name}", REASON_RECORDS_EXTRA)
    middle = sorted((names & allowed) - {rec.SHARED_NAME, own_name})
    order = [rec.SHARED_NAME, *middle, own_name]
    for name in order:
        if name in names:
            ctx.unlink(f"{TMP_DIR}/{name}")
    ctx.hit("record")
    if not extra:
        try:
            os.rmdir(ctx.path(TMP_DIR))
        except OSError as exc:
            ctx.leave(TMP_DIR, f"could not be removed: {exc.strerror}")
    sync_dir(ctx.root / "etc")
    ctx.hit("records-dir")


def _disable_controllers(
    ctx: _Ctx, entry: rec.PathEntry, boot_recorded: str | None, boot_now: str | None
) -> None:
    """Write `-<ctrl>` for each controller now enabled and absent from the pre-image (ii).

    Only in the boot that shared.json recorded, because cgroup state does not survive a reboot.
    The pre-image bytes are never written back.
    """
    rel = entry.path
    if boot_recorded is None or boot_now is None:
        ctx.leave(rel, REASON_BOOT_UNKNOWN)
        return
    if boot_recorded != boot_now:
        ctx.leave(rel, REASON_BOOT)
        return
    pre = entry.pre
    if pre.state != "file" or pre.data is None:
        ctx.leave(rel, f"pre-image is {pre.state}, so it is not undone")
        return
    path = ctx.path(rel)
    state = _probe(path)[0]
    if state != "file":
        ctx.leave(rel, _state_reason(state))
        return
    wanted = frozenset(pre.data.decode("ascii", errors="replace").split())
    now = frozenset(path.read_text(encoding="ascii", errors="replace").split())
    for ctrl in sorted(now - wanted):
        try:
            ctx.write_control(path, f"-{ctrl}")
        except OSError as exc:
            # EBUSY: a cgroup that is not ours still enables the controller. Report, do not crash.
            ctx.leave(rel, f"-{ctrl} refused: {exc.strerror}")


def _undo_shared_dir(ctx: _Ctx, entry: rec.PathEntry) -> None:
    """Step 4 (iii). A shared directory is removed only when its pre-image is absent."""
    rel = entry.path
    if entry.pre.state != "absent":
        if _probe(ctx.path(rel))[0] != "absent":
            ctx.leave(rel, REASON_PRE_PRESENT)
        return
    _remove_dir(ctx, rel)


def _remove_dir(ctx: _Ctx, rel: str) -> None:
    """rmdir one directory. Absent is done. A non-empty one is left and reported."""
    path = ctx.path(rel)
    state = _probe(path)[0]
    if state == "absent":
        return
    if state != "dir":
        ctx.leave(rel, _state_reason(state))
        return
    remove: RemoveDir = ctx.rmdir_cgroup if _is_cgroup(rel) else os.rmdir
    try:
        remove(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno == errno.ENOTEMPTY:
            ctx.leave(rel, REASON_NOT_EMPTY)
        else:
            ctx.leave(rel, f"could not be removed: {exc.strerror}")


def _remove_tree(ctx: _Ctx, rel: str) -> None:
    """Remove a per-user runtime tree (run/ginn/<uid>). The top is never followed."""
    path = ctx.path(rel)
    state = _probe(path)[0]
    if state == "absent":
        return
    if state == "symlink":
        ctx.leave(rel, REASON_SYMLINK)
        return
    if state != "dir":
        ctx.leave(rel, _state_reason(state))
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        ctx.leave(rel, f"could not be removed: {exc.strerror}")


def _is_last(root: Path, user: str) -> bool:
    """True when no other per-user record exists, pending or complete."""
    directory = root / RECORD_DIR
    if _probe(directory)[0] != "dir":
        return True
    own = PurePosixPath(rec.relpath(user)).name
    return not any(rec.is_record_name(name) and name != own for name in os.listdir(directory))


def _not_configured(root: Path, user: str) -> UndoResult:
    """No record for `user`. Nothing is changed. An empty /etc/ginn left by an undo is reported."""
    left: list[LeftPath] = []
    records_dir = root / RECORD_DIR
    if _probe(records_dir)[0] == "dir" and not _has_records(root):
        if _probe(root / SHARED_RELPATH)[0] == "absent":
            left.append(LeftPath(RECORD_DIR, REASON_RECORDS_DIR))
    return UndoResult(
        user=user,
        record=str(root / rec.relpath(user)),
        status=NOT_CONFIGURED,
        left=tuple(left),
        notes=(f"no Host Setup Record for {user}; nothing changed",),
    )


def _sweep_tmp(host: Host) -> None:
    """Remove the first-setup temporary directory when a crash left it (Decision 16). Under lock."""
    tmp = host.root / TMP_DIR
    state = _probe(tmp)[0]
    if state == "absent":
        return
    if state != "dir":
        raise rec.unsafe(tmp, f"is a {state}, but a directory is needed")
    shutil.rmtree(tmp)
    sync_dir(host.root / "etc")


def _account_notes(user: str, current: rec.HostSetupRecord, lookup: UserLookup) -> list[str]:
    """Warn when the account database no longer agrees with the record. Undo uses the record."""
    try:
        info: UserInfo = lookup(user)
    except KeyError:
        return [f"account {user} does not exist now; undo used the uid, gid and home of the record"]
    if (info.uid, info.gid, info.home) != (current.uid, current.gid, current.home):
        return [
            f"warning: account {user} now has uid {info.uid}, gid {info.gid} and home "
            f"{info.home}, but the record says {current.uid}, {current.gid} and {current.home}; "
            "undo used the record"
        ]
    return []


def _has_records(root: Path) -> bool:
    return any(rec.is_record_name(name) for name in os.listdir(root / RECORD_DIR))


def _is_cgroup(rel: str) -> bool:
    return rel == CGROUP_RELPATH or rel.startswith(f"{CGROUP_RELPATH}/")


def _deepest_first(entries: Iterable[rec.PathEntry]) -> list[rec.PathEntry]:
    return sorted(entries, key=lambda entry: entry.path.count("/"), reverse=True)


def _recorded_range(current: rec.HostSetupRecord, rel: str) -> subids.SubIdEntry | None:
    if current.subids is None:
        return None
    if rel == SUBUID:
        return current.subids.uid
    if rel == SUBGID:
        return current.subids.gid
    return None


def _pre_lines(pre: rec.Image) -> list[subids.SubIdEntry]:
    if pre.state == "file" and pre.data is not None:
        return _parse_bytes(pre.data)
    return []


def _read_lines(path: Path) -> list[subids.SubIdEntry]:
    if _probe(path)[0] != "file":
        return []
    return _parse_bytes(path.read_bytes())


def _parse_bytes(data: bytes) -> list[subids.SubIdEntry]:
    return subids.parse_entries(data.decode("utf-8", errors="surrogateescape"))


def _state_reason(state: str) -> str:
    return f"is {state} now, so it is not touched"


def _write_control(path: Path, text: str) -> None:
    """Write one controller change to a cgroup control file, as the kernel expects."""
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, text.encode("ascii"))
    finally:
        os.close(fd)


def _realm_names(root: Path, home: str) -> list[str]:
    """Names of the realm records under the recorded home, re-rooted under `root` (FR-003 AC-2).

    The walk opens each component from the one before it with O_NOFOLLOW, so no symbolic link in
    the home is followed. A missing component means there are no realms. Any other OSError is
    refused with GINN-E-SETUP-PATH-UNSAFE, which names the path, and no traceback is shown.
    """
    parts = [part for part in realms_dir_for_home(PurePosixPath(home)).parts if part != "/"]
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    walked = root
    try:
        for part in parts:
            walked = walked / part
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                return []
            except OSError as exc:
                raise rec.unsafe(
                    walked,
                    f"cannot be walked to find the user's realms ({os.strerror(exc.errno or 0)})",
                ) from None
            os.close(fd)
            fd = child
        try:
            names = os.listdir(fd)
        except OSError as exc:
            raise rec.unsafe(
                walked,
                f"cannot be listed to find the user's realms ({os.strerror(exc.errno or 0)})",
            ) from None
    finally:
        os.close(fd)
    return sorted(name[: -len(".json")] for name in names if name.endswith(".json"))


def _refuse_realms(root: Path, user: str, home: str, names: RealmNames) -> None:
    """GINN-E-UNDO-REALMS-EXIST (FR-003 AC-2) when the recorded home still holds a realm."""
    if list(names(root, home)):
        raise make("GINN-E-UNDO-REALMS-EXIST", user=user)
