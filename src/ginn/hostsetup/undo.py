# SPDX-License-Identifier: Apache-2.0
"""Host Setup undo for one user (FR-003, DR-009, DR-010; ADR-013 Decisions 15, 17, 18 and 20).

`run_undo` takes the global setup lock and works from the Host Setup Record of one user. It reverses
what `setup.run_setup` changed. It never writes shared bytes back (CL-11): the subordinate ID files
lose only the exact lines that setup appended, and cgroup controllers are disabled, not restored.
Each step ends with a fault hook, so a crash can be simulated at any step. The steps run in this
order (Decision 15 (v)):

1. The service link. Removed when its pre-image is absent, restored when its pre-image was a
   symbolic link. Only a link that points at /etc/sv/ginn-<user> is changed.
2. Wait for runsv to exit (`wait_until_stopped`). A timeout raises GINN-E-UNDO-SERVICE-RUNNING and
   leaves the record in place, so a rerun continues from here.
3. Per-user cleanup: the per-user service directories, leaf first; the subordinate ID lines that
   setup appended (`subids.remove_exact`); and the per-user files and directories, restored from
   their pre-images or removed.
4. Shared state. Applied only when this is the last record. The root cgroup.subtree_control gets
   `-<ctrl>` for each controller that is present now and absent from its pre-image, and only in the
   boot that shared.json recorded. Each shared directory whose pre-image is absent is removed with
   rmdir, leaf first. Then shared.json is removed. When another record remains, every shared path
   that still exists is reported and left.
5. The user's own record is removed.
6. /etc/ginn is removed with rmdir, but only when shared.json listed it with pre-image absent. That
   entry is read before step 4 removes shared.json (Decision 15 (v) step 6, Decision 20).

Undo dispatches on each entry's `kind`, `shared` flag and pre-image state, never on path lists.
The constants below name only the service link's parent, the subordinate ID files, the cgroup root
and the record directory. A path that is a symbolic link is never followed.

The realm check reads the realm records under the default state directory of the account's home
(`~/.local/state/ginn/realms`, as store/paths.py derives it), with the home re-rooted under the
root directory. A custom XDG_STATE_HOME is not seen, because root cannot read the user's
environment. That is a residual.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pwd
import shutil
import stat
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ginn.errors import make
from ginn.hostsetup import record as rec
from ginn.hostsetup import runit_system, subids
from ginn.hostsetup._atomic import atomic_symlink, atomic_write, sync_dir
from ginn.hostsetup.cgroups import DEFAULT_CGROUP_ROOT
from ginn.hostsetup.setup import (
    LOCK_POLL_SECONDS,
    LOCK_TIMEOUT_SECONDS,
    SETUP_LOCK,
    SUBGID,
    SUBUID,
    UserInfo,
    UserLookup,
    read_boot_id,
)

RECORD_DIR = rec.RECORD_DIR
SHARED_RELPATH = rec.SHARED_RELPATH
REALMS_RELPATH = PurePosixPath(".local/state/ginn/realms")
CGROUP_RELPATH = str(DEFAULT_CGROUP_ROOT).lstrip("/")
SERVICE_WAIT_SECONDS = 30.0
SERVICE_POLL_SECONDS = 0.1
UNDO_STEPS: tuple[str, ...] = (
    "link",
    "wait",
    "service",
    "subids",
    "files",
    "control",
    "shared-dirs",
    "shared",
    "record",
    "records-dir",
)
"""The fault points, in the order undo reaches them. Tests crash after each one."""
UNDONE = "undone"
NOT_CONFIGURED = "not configured"
REMOVE_RECORDS_DIR = "remove"
"""The plan for /etc/ginn when shared.json lists it with pre-image absent."""

REASON_SHARED = "shared, used by another record"
REASON_PRE_PRESENT = "pre-image present"
REASON_ADMIN_LINE = "admin line, present before setup, kept"
REASON_DIFFERING = "differing line kept, not the recorded range"
REASON_NOT_EMPTY = "ENOTEMPTY, the dir holds unexpected content"
REASON_BOOT = "boot changed since setup, so the cgroup state is gone; controllers left"
REASON_BOOT_UNKNOWN = "boot id unknown, so cgroup controllers are not touched"
REASON_LINK = "service link target is not ours, so it is left"
REASON_SYMLINK = "symbolic link, not followed"
REASON_OTHER_LINES = "holds subordinate ID lines of another record or the admin"
REASON_PRE_EXISTING = "pre-existing, setup did not create it, so undo leaves it"
REASON_SHARED_GONE = "shared.json is already gone, so undo cannot confirm setup created it"
REASON_RECORDS_DIR = "holds no record and no shared.json, so it is left"

FaultHook = Callable[[str], None]
ServiceWait = Callable[[Path, float], None]
RealmNames = Callable[[Path, UserInfo], Sequence[str]]
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
    """What one undo run needs: the root, the injected effects, and what it has reported."""

    root: Path
    user: str
    fault: FaultHook | None
    wait: ServiceWait
    rmdir_cgroup: RemoveDir
    write_control: WriteControl
    left: list[LeftPath]
    notes: list[str]

    def path(self, rel: str) -> Path:
        return _guarded(self.root, rel)

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
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> UndoResult:
    """Undo Host Setup for `user` with every host path under `root_dir`. See the module docstring.

    The root check runs first and reads no target path. The realm check runs before the lock and
    again under it, and a user with realms is refused with nothing changed. `realm_names` lists the
    realms of an account (tests inject it), `wait_service` waits for runsv, `rmdir_cgroup` removes
    a cgroup directory and `write_control` writes one controller change. Tests inject the last two,
    because a plain rmdir of a tmp_path directory with files does not behave like cgroupfs.
    """
    if (os.geteuid() if euid is None else euid) != 0:
        raise make("GINN-E-SETUP-ROOT")
    if user is None:
        raise make("GINN-E-USAGE", detail="--user NAME is required")
    root = _check_root(root_dir)
    info = _resolve(user, lookup_user or _pwd_lookup)
    names: RealmNames = realm_names or _realm_names
    _refuse_realms(root, user, info, names)
    own = root / rec.relpath(user)
    if rec.load(own) is None:
        return _not_configured(root, user)
    with _global_lock(root, lock_timeout):
        _refuse_realms(root, user, info, names)
        current = rec.load(own)
        if current is None:
            return _not_configured(root, user)
        ctx = _Ctx(
            root=root,
            user=user,
            fault=fault,
            wait=wait_service or _default_wait,
            rmdir_cgroup=rmdir_cgroup or os.rmdir,
            write_control=write_control or _write_control,
            left=[],
            notes=[],
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
    """True while runsv serves `svdir`. runit keeps `supervise/ok` open for reading only then.

    A write-only open with O_NONBLOCK fails with ENXIO when nothing reads the FIFO, which is how
    `sv` checks a service. A missing `supervise` directory means runsv is not running.
    """
    ok = svdir / "supervise" / "ok"
    try:
        fd = os.open(ok, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ENOENT, errno.ENOTDIR):
            return False
        raise
    os.close(fd)
    return True


def _default_wait(svdir: Path, seconds: float) -> None:
    wait_until_stopped(svdir, seconds)


def _undo_record(ctx: _Ctx, current: rec.HostSetupRecord) -> UndoResult:
    shared = rec.load_shared(ctx.root / SHARED_RELPATH)
    last = _is_last(ctx.root, ctx.user)
    records_dir = _records_dir_plan(shared, last)
    own = rec.relpath(ctx.user)

    for entry in current.paths:
        if entry.kind == "symlink":
            _undo_link(ctx, entry)
    ctx.hit("link")

    ctx.wait(ctx.root / runit_system.SV_PARENT / f"ginn-{ctx.user}", SERVICE_WAIT_SECONDS)
    ctx.hit("wait")

    for entry in _deepest_first(e for e in current.service_paths if not e.shared):
        _undo_service_dir(ctx, entry)
    ctx.hit("service")

    for entry in current.paths:
        if entry.kind == "append-lines":
            _undo_subids(ctx, current, entry, last)
    ctx.hit("subids")

    for entry in current.paths:
        if entry.kind == "file" and entry.path != own:
            _undo_file(ctx, entry)
    for entry in _deepest_first(e for e in current.paths if e.kind == "dir"):
        _undo_dir(ctx, entry)
    ctx.hit("files")

    if shared is None:
        if last:
            ctx.notes.append(
                "shared.json is already gone; an earlier undo applied the shared steps"
            )
    elif not last:
        _report_shared(ctx, shared)
    else:
        _apply_shared(ctx, shared)

    ctx.unlink(own)
    ctx.hit("record")

    if records_dir == REMOVE_RECORDS_DIR:
        _remove_dir(ctx, RECORD_DIR)
    elif records_dir is not None and _probe(ctx.root / RECORD_DIR)[0] != "absent":
        ctx.leave(RECORD_DIR, records_dir)
    ctx.hit("records-dir")

    return UndoResult(
        user=ctx.user,
        record=str(ctx.root / own),
        status=UNDONE,
        left=tuple(ctx.left),
        notes=tuple(ctx.notes),
    )


def _records_dir_plan(shared: rec.SharedRecord | None, last: bool) -> str | None:
    """Decide step 6 for /etc/ginn before step 4 removes shared.json (Decision 15 (v), Decision 20).

    Returns None when this is not the last record, REMOVE_RECORDS_DIR when shared.json lists the
    directory with pre-image absent, or the reason to leave it.
    """
    if not last:
        return None
    if shared is None:
        return REASON_SHARED_GONE
    for entry in shared.paths:
        if entry.path == RECORD_DIR:
            if entry.kind == "dir" and entry.pre.state == "absent":
                return REMOVE_RECORDS_DIR
            break
    return REASON_PRE_EXISTING


def _undo_link(ctx: _Ctx, entry: rec.PathEntry) -> None:
    """Step 1. Remove the service link, or restore its symbolic-link pre-image."""
    rel = entry.path
    want = f"/{runit_system.SV_PARENT}/ginn-{ctx.user}"
    path = ctx.path(rel)
    state, _, target = _probe(path)
    if entry.pre.state == "absent":
        if state == "absent":
            return
        if state == "symlink" and target == want:
            ctx.unlink(rel)
            return
        ctx.leave(rel, REASON_LINK if state == "symlink" else _state_reason(state))
        return
    if state == "symlink" and target == entry.pre.target:
        return
    if state == "absent" or (state == "symlink" and target == want):
        if entry.pre.target is not None:
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
    if _is_cgroup(rel):
        _remove_dir(ctx, rel)
    else:
        _remove_tree(ctx, rel)


def _undo_subids(
    ctx: _Ctx, current: rec.HostSetupRecord, entry: rec.PathEntry, last: bool
) -> None:
    """Step 3, subordinate ID lines (Decision 15 (i)). Never restores the file whole."""
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
    for line in candidates:
        if line in pre_lines:
            ctx.leave(rel, f"{REASON_ADMIN_LINE}: {line.render()}")
            continue
        removal = subids.remove_exact(
            ctx.path(rel), line.name, line.start, line.count, root_dir=ctx.root
        )
        for kept in removal.kept_differing:
            ctx.leave(rel, f"{REASON_DIFFERING}: {kept.render()}")
    _settle_subid_file(ctx, rel, entry, pre_lines, last)


def _settle_subid_file(
    ctx: _Ctx,
    rel: str,
    entry: rec.PathEntry,
    pre_lines: list[subids.SubIdEntry],
    last: bool,
) -> None:
    """After the lines are gone, remove the file when it holds nothing that undo must keep.

    That holds when the pre-image is absent, which is the file setup created. It also holds for
    the last record when the pre-image had content: a later user's record captured the file after
    an earlier user's line was appended, so its pre-image names lines that the earlier undo already
    removed. An admin's line is never removed by undo, so an empty file at the last undo holds none.
    A pre-existing empty file is kept, because it is restored as it was.
    """
    path = ctx.path(rel)
    state = _probe(path)[0]
    if state == "absent":
        return
    if state != "file":
        ctx.leave(rel, _state_reason(state))
        return
    data = path.read_bytes()
    pre_had_content = entry.pre.state == "file" and bool((entry.pre.data or b"").strip())
    if not data.strip() and (entry.pre.state == "absent" or (last and pre_had_content)):
        ctx.unlink(rel)
        return
    # Another record that remains may have appended this line, even when its pre-image holds it. The
    # last record reports only lines that its own pre-image lacks: a line present before setup is
    # the admin's, and it is not a residual of setup.
    foreign = [
        line
        for line in _parse_bytes(data)
        if line.name != ctx.user and (not last or line not in pre_lines)
    ]
    if foreign:
        ctx.leave(rel, REASON_OTHER_LINES)


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
    """Step 3, a per-user directory. Removed leaf first when its pre-image is absent."""
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


def _report_shared(ctx: _Ctx, shared: rec.SharedRecord) -> None:
    """Another record remains, so every shared path that still exists is left and reported."""
    for entry in (*shared.paths, *shared.service_paths):
        if _probe(ctx.path(entry.path))[0] != "absent":
            ctx.leave(entry.path, REASON_SHARED)


def _apply_shared(ctx: _Ctx, shared: rec.SharedRecord) -> None:
    """Step 4 for the last record: controllers, then shared directories, then shared.json."""
    boot_now = read_boot_id(ctx.root)
    for entry in shared.service_paths:
        if entry.kind == "file":
            _disable_controllers(ctx, entry, shared.boot_id, boot_now)
    ctx.hit("control")

    dirs = [
        entry
        for entry in (*shared.paths, *shared.service_paths)
        if entry.kind == "dir" and entry.path != RECORD_DIR
    ]
    for entry in _deepest_first(dirs):
        _undo_shared_dir(ctx, entry)
    ctx.hit("shared-dirs")

    ctx.unlink(SHARED_RELPATH)
    ctx.hit("shared")


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
        ctx.write_control(path, f"-{ctrl}")


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


def _guarded(root: Path, rel: str) -> Path:
    """Return root/rel after refusing a parent that is a symbolic link or a file."""
    current = root
    for part in PurePosixPath(rel).parts[:-1]:
        current = current / part
        state = _probe(current)[0]
        if state == "absent":
            break
        if state != "dir":
            raise rec.unsafe(current, f"is a {state}, but a directory is needed")
    return root / rel


def _probe(path: Path) -> tuple[str, int, str]:
    """Return (state, mode bits, link target) of `path`. A symbolic link is never followed."""
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


def _write_control(path: Path, text: str) -> None:
    """Write one controller change to a cgroup control file, as the kernel expects."""
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, text.encode("ascii"))
    finally:
        os.close(fd)


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


def _realm_names(root: Path, info: UserInfo) -> list[str]:
    """Names of the realm records under the account's default state directory, re-rooted."""
    directory = root.joinpath(info.home.lstrip("/"), *REALMS_RELPATH.parts)
    try:
        names = os.listdir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(name[: -len(".json")] for name in names if name.endswith(".json"))


def _refuse_realms(root: Path, user: str, info: UserInfo, names: RealmNames) -> None:
    """GINN-E-UNDO-REALMS-EXIST (FR-003 AC-2) when the user still has a realm."""
    if list(names(root, info)):
        raise make("GINN-E-UNDO-REALMS-EXIST", user=user)


@contextmanager
def _global_lock(root: Path, timeout: float) -> Iterator[None]:
    """Hold /run/lock/ginn-host-setup.lock for the whole undo (Decision 11, Decision 15)."""
    path = root / SETUP_LOCK
    fd = _open_lock(path)
    try:
        _take_lock(fd, path, timeout)
        yield
    finally:
        os.close(fd)


def _open_lock(path: Path) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_CREAT
    try:
        fd = os.open(path, flags, 0o600)
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
