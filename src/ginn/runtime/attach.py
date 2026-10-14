# SPDX-License-Identifier: Apache-2.0
"""Attach variable grants into a running realm, and re-attach them at enter and launch.

ADR-008 (amended 2026-10-12 and 2026-10-13) decides this flow. `mounts.attach_plan` and
`env.session_env` give the grants. This module runs the helper `native/ginn-rebind.c` directly on
the host, not under `podman unshare` (ADR-008 amendment, 2026-10-13): the helper joins the realm's
user namespace and copies the host mount table, so a drive mounted on the host after the realm
started is in the copy. The module keeps the grant list the next enter or launch needs (FR-061,
FR-064, FR-065, FR-066, IR-025).

- `attach_at_start` attaches every grant after `podman start`, each without --force. A missing
  source is skipped and the start goes on (FR-064). A device or inode that differs from the host
  identity, or a realm that shows fewer mounts below the source than the host does, is skipped and
  its mount is removed (NOT_VISIBLE). Any other failure raises GINN-E-ATTACH-FAILED, and the caller
  stops the realm again (ADR-008, T4.10 notes).
- `reattach` runs at enter and launch, before exec. It passes only the grants whose host identity
  changed since their last attach, each with --force. A grant whose source is gone loses its old
  mount. A NOT_VISIBLE grant is held until its host identity changes (see `_held`). Any other
  failure raises, and the realm keeps running (FR-081, ADR-003).
- `needs_reattach` is the pre-check of enter (ADR-008 amendment, ADR-010). It makes the decision
  that `reattach` would make, from the same code, and it reads the host only. It makes no helper
  call and no podman call, so enter takes the realm lock only when it returns True.

Both take `admit`, the DR-004 rules applied again to each grant the helper is about to attach
(`mounts.recheck`, see `admission`). A refused grant is not attached. A grant that was never
attached is skipped with the refusal. A grant that was attached before and is refused now is
unmounted, and it is skipped with the refusal.

The grant list is fixed at the last start (FR-066). Its realm paths stay as they were. A session
grant takes its host from the session current at each start or enter, by its fixed realm path
(ADR-008, fixed names). A session grant with no host now is missing, and its old mount is removed.

The identity of a host source is `(st_dev, st_ino)`, read on the host, as podman follows the path,
and the mounts below it. A folder or a mount root adds the mount points below its real path that
hang from the mount it resolves to, so a drive mounted under `/run/media/<user>` changes the
identity (ADR-008 amendment, Resolution A). The helper prints the device and inode of the tree it
cloned from its copy of the host table. A device or inode that differs from the host identity read
before the attach, or a realm that shows fewer mounts below its path than the identity lists, is not
recorded as attached (NOT_VISIBLE). The mount is removed, and the identity read before the attach is
kept as the hold key (see `_held`).

Symbolic links are resolved here, before admission: the helper is given the resolved path, and it
opens it with no symbolic link on the way (ADR-008, Resolution A). The record keeps the path that
the user named.

The helper makes each missing placeholder itself, a directory or an empty file by source kind. This
module makes only the parent directories, as root, in the container (`mkdir -p`). A placeholder
under the Realm Home is made on the host, so the user keeps the directory.

This module runs no operation. The callers in ops own the state record, the realm lock (FR-123) and
the stop. The helper's per-group status lines are read by position, so argv order is the contract.
"""

import os
import re
import stat
import subprocess  # noqa: S404 - the helper runs from a fixed argv list, never a shell
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

from ginn.domain.grants import GrantRecord, SourceIdentity
from ginn.errors import GinnError, make
from ginn.podman.adapter import Podman
from ginn.podman.models import ExecResult
from ginn.runtime.env import SessionEnv
from ginn.runtime.mounts import AttachPlan, Skipped, recheck

HELPER_ENV: Final = "GINN_REBIND_HELPER"
"""Overrides the helper path, for tests and for a build tree that is not installed."""
DEFAULT_HELPER: Final = "/usr/libexec/ginn/ginn-rebind"
"""Where `make -C native install` puts the helper (native/Makefile, LIBEXECDIR)."""
HELPER_TIMEOUT: Final = 60.0
"""Seconds a helper run may take. A run is a few syscalls, so this only bounds a hang."""
MOUNTINFO: Final = Path("/proc/self/mountinfo")
_OCTAL: Final = re.compile(rb"\\([0-7]{3})")
"""A mount table escape, such as `\\040` for a space. The table is bytes, so it is undone first."""
NOT_VISIBLE: Final = "the realm does not show the host source as it is; retried on a host change"
"""The skip reason for an attach whose tree does not match the host (FR-064): a device or inode that
differs, or fewer mounts below the path than the host identity lists. The grant is held until the
host identity changes, so a repeated mismatch costs no helper run at enter (see `_held`)."""
Op = Literal["attach", "force", "unmount"]
"""One helper group: a first attach, a forced attach, or an unmount of the realm path alone."""
HelperRun = Callable[[Podman, Sequence[str]], ExecResult]
"""Runs the helper from an argv list and returns its status and output (see `run_on_host`)."""


Admit = Callable[[GrantRecord], str | None]
"""Applies the DR-004 rules to a grant. Returns the refusal, or None if it may be attached."""


def run_on_host(podman: Podman, argv: Sequence[str]) -> ExecResult:
    """The default `HelperRun`: the helper on the host. `podman` is not used (see `run_helper`).

    It is passed so that a unit test can answer the helper through its own fake podman, which logs
    the call (tests/unit/conftest.py). Nothing else depends on it.
    """
    del podman
    return run_helper(argv)


def run_helper(argv: Sequence[str]) -> ExecResult:
    """Run the helper on the host, with no shell (NFR-SEC-005) and no podman (ADR-008 amendment).

    The helper must start in the host's mount namespace: it copies the mount table of the caller's
    namespace, and a podman-started process sees the store's table, which is the stale copy.
    """
    try:
        done = subprocess.run(  # noqa: S603 - argv list built by rebind_argv, no shell
            list(argv),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=HELPER_TIMEOUT,
            env={"LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _failed(f"cannot run the helper {argv[0]}: {exc}") from exc
    return ExecResult(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class AttachResult:
    """The grant list after one run, with the identities that now hold, and the skips it has."""

    records: list[GrantRecord]
    skipped: list[Skipped]


@dataclass(frozen=True)
class _Row:
    """One helper status line. `seen` is the device and inode, set only for `attached`."""

    status: str
    seen: SourceIdentity | None = None


def _failed(detail: str) -> GinnError:
    return make("GINN-E-ATTACH-FAILED", detail=detail)


def _decisive(stderr: str) -> str:
    """The helper's own line when it printed one, else the first line, else a fixed phrase."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("ginn-rebind:"):
            return line
    return lines[0] if lines else "the helper printed nothing"


def records_from_plan(plan: AttachPlan, session: SessionEnv) -> list[GrantRecord]:
    """The grant list for a start, from the plan and the session environment.

    It holds the attached grants of `plan`. Then it holds each skip for a missing source, from the
    plan and from the session, with `identity=None`, so each enter or launch tries it again (FR-064,
    FR-066). A refused or wrong-type skip is not in the list, because it is never retried. Last, it
    holds the session slots the session does not bind, which are not reported as skips (FR-084).
    Each realm path appears once, and the first entry wins.
    """
    is_dir = {point.path: point.is_dir for point in plan.mountpoints}
    grants = [GrantRecord(a.kind, a.host, a.realm, a.mode, is_dir[a.realm]) for a in plan.attach]
    skips = [s for s in [*plan.skipped, *session.skipped] if s.missing and s.realm]
    grants += [GrantRecord(s.kind, s.host, s.realm, s.mode, s.is_dir) for s in skips]
    grants += [GrantRecord("session", b.host, b.realm, b.mode, b.is_dir) for b in session.absent]
    unique: dict[str, GrantRecord] = {}
    for grant in grants:
        unique.setdefault(grant.realm, grant)
    return list(unique.values())


def share_skipped(plan: AttachPlan, session: SessionEnv, result: AttachResult) -> list[str]:
    """The strings for RealmRecord.share_skipped after a start (FR-064 AC-1).

    The plan's share skips come first, except for a grant that is attached now. The session skips
    come next and always stay, since each one names a variable the user set. Then comes each
    attach-time skip whose realm path is not already listed. A session slot that the session does
    not bind is silent, so it is never listed. Duplicates are dropped.
    """
    attached = {record.realm for record in result.records if record.identity is not None}
    silent = {bind.realm for bind in session.absent}
    decided = [s for s in plan.skipped if s.realm not in attached]
    decided += session.skipped
    listed = {s.realm for s in decided if s.realm}
    later = [s for s in result.skipped if s.realm not in listed and s.realm not in silent]
    return list(dict.fromkeys(s.message() for s in [*decided, *later]))


def admission(
    *, home: Path, owned_paths: Sequence[Path], runtime_root: Path, env: Mapping[str, str]
) -> Admit:
    """The `admit` for a realm: the DR-004 rules of `mounts.recheck`, applied to each grant."""

    def admit(grant: GrantRecord) -> str | None:
        return recheck(
            grant.kind,
            grant.host,
            grant.realm,
            grant.mode,
            home=home,
            owned_paths=owned_paths,
            runtime_root=runtime_root,
            env=env,
        )

    return admit


def helper_path(environ: Mapping[str, str] | None = None) -> str:
    """The helper's absolute path: `GINN_REBIND_HELPER` when set, else the installed location."""
    env = os.environ if environ is None else environ
    path = env.get(HELPER_ENV) or DEFAULT_HELPER
    if not path.startswith("/"):
        raise ValueError(f"{HELPER_ENV} must be an absolute path, not {path!r}")
    return path


def read_identity(host: str, *, mountinfo: Path = MOUNTINFO) -> SourceIdentity | None:
    """The identity of a host source, followed as podman follows it. None when it is missing."""
    try:
        st = os.stat(host)
    except (OSError, ValueError):
        return None
    if not stat.S_ISDIR(st.st_mode):
        return SourceIdentity(st.st_dev, st.st_ino)
    below = _mounts_below(os.path.realpath(host), mountinfo)
    return SourceIdentity(st.st_dev, st.st_ino, below)


def _mounts_below(real: str, mountinfo: Path) -> tuple[str, ...]:
    """The mount points below `real` that hang from the mount `real` resolves to (see `_below`).

    The table is read as bytes. Its octal escapes are undone before decoding, so a name that is not
    valid UTF-8 still compares equal. `real` is a resolved path, as the table records it.
    """
    try:
        table = mountinfo.read_bytes()
    except OSError as exc:
        raise _failed(f"cannot read {mountinfo}: {exc.strerror or exc}") from exc
    return _below(_mounts(table), real)


@dataclass(frozen=True)
class _Mount:
    """One line of a mount table: its id, its parent's id, and its mount point, escapes undone."""

    ident: int
    parent: int
    point: str


def _mounts(table: bytes) -> list[_Mount]:
    """Every line of a mount table, in table order (a later line of the same point is on top)."""
    mounts: list[_Mount] = []
    for line in table.split(b"\n"):
        fields = line.split(b" ")
        if len(fields) > 4 and fields[0].isdigit() and fields[1].isdigit():
            escaped = _OCTAL.sub(lambda m: bytes([int(m.group(1), 8)]), fields[4])
            mounts.append(_Mount(int(fields[0]), int(fields[1]), os.fsdecode(escaped)))
    return mounts


def _below(mounts: Sequence[_Mount], target: str) -> tuple[str, ...]:
    """The mount points strictly below `target` that hang from the mount `target` resolves to.

    `target` resolves to the last mount, in table order, whose point is the longest prefix of it.
    A mount below `target` counts when its chain of parents (by mount id) reaches that mount. A
    mount hidden under another mount at the same place is not on that chain, and open_tree does not
    clone it, so it does not count. A mount that is not below the source but is mounted on the same
    parent does not count either. The realm's table follows the same rule, with `target` as the
    realm path the clone was moved to, so both sides count the same tree (ADR-008, Resolution A).
    """
    prefix = target.rstrip("/") + "/"
    root: _Mount | None = None
    for mount in mounts:
        covers = target == mount.point or prefix.startswith(mount.point.rstrip("/") + "/")
        if covers and (root is None or len(mount.point) >= len(root.point)):
            root = mount
    if root is None:
        return ()
    by_id = {mount.ident: mount for mount in mounts}
    points = [
        mount.point
        for mount in mounts
        if mount.point != target
        and mount.point.startswith(prefix)
        and _hangs_from(mount, root.ident, by_id)
    ]
    return tuple(sorted(points))


def _hangs_from(mount: _Mount, ident: int, by_id: Mapping[int, _Mount]) -> bool:
    """True when the parent chain of `mount` reaches the mount with id `ident`."""
    current = mount
    for _ in range(len(by_id) + 1):
        if current.parent == ident:
            return True
        parent = by_id.get(current.parent)
        if parent is None:
            return False
        current = parent
    return False


def _resolved(grant: GrantRecord) -> GrantRecord:
    """The grant with its host path resolved: the path the helper opens and admission checks.

    A symbolic link on the way is followed here, so the helper never meets one: it refuses a link
    with RESOLVE_NO_SYMLINKS (ADR-008, Resolution A). The record keeps the path the user named.
    """
    return grant if not grant.host else replace(grant, host=os.path.realpath(grant.host))


def _plain(path: str) -> bool:
    """True for an absolute path with no ".." component. The helper refuses one as well."""
    return path.startswith("/") and ".." not in path.split("/")


def rebind_argv(helper: str, pid: int, ops: Sequence[tuple[GrantRecord, Op]]) -> list[str]:
    """The helper argv for `ops`, in ADR-008's argument form, with the `--unmount` group.

    An `attach` group is `[--ro] <host> <realm>`. A `force` group is
    `--force [--ro] <host> <realm>`,
    and it skips the helper's own compare, because the caller found the identity changed (ADR-008
    review amendment). An `unmount` group is `--unmount <realm>` alone.
    """
    if not helper.startswith("/"):
        raise ValueError(f"helper path must be absolute, not {helper!r}")
    if pid < 2:
        raise ValueError(f"pid must be the container's init process, above 1, not {pid}")
    argv = [helper, str(pid)]
    for grant, op in ops:
        if not _plain(grant.realm) or (op != "unmount" and not _plain(grant.host)):
            raise ValueError(f"grant paths must be absolute, without '..': {grant!r}")
        if op == "unmount":
            argv += ["--unmount", grant.realm]
            continue
        if op == "force":
            argv.append("--force")
        if grant.mode == "ro":
            argv.append("--ro")
        argv += [grant.host, grant.realm]
    return argv


_RUNTIME_SUBDIR: Final = re.compile(r"/run/user/([0-9]+)/[^/]+(?:/[^/]+)*")
"""A directory strictly below a user's runtime directory, such as `gnupg` or `pulse`."""


def _runtime_subdir(podman: Podman, container: str, parent: str) -> None:
    """Make a runtime subdirectory, such as `/run/user/<uid>/gnupg`, as the realm user at mode 0700.

    The realm user owns `/run/user/<uid>` (its tmpfs is chowned to it), so the directory is made
    with that uid and no root command runs on it. A root `mkdir` would leave the directory
    root-owned, which gpg refuses, and a root `chown` or `chmod` would follow a link that the realm
    planted. Only a directory strictly below `/run/user/<uid>` is handled, and only for that uid. A
    `.` or `..` part is refused, and a symbolic link is refused before the directory is made. Every
    argument is one argv element (NFR-SEC-005).
    """
    match = _RUNTIME_SUBDIR.fullmatch(parent)
    if match is None:
        return
    if any(part in (".", "..") for part in parent.split("/")):
        raise _failed(f"a runtime subdirectory may not hold '.' or '..': {parent}")
    user = match.group(1)
    if _in_container_result(podman, container, ["test", "-L", parent], user=user) == 0:
        raise _failed(f"a runtime subdirectory is a symbolic link, refused: {parent}")
    _in_container(podman, container, ["mkdir", "-p", "-m", "0700", "--", parent], user=user)


def _in_container_result(
    podman: Podman, container: str, argv: Sequence[str], *, user: str = "0:0"
) -> int:
    """The exit status of one command in the realm, with no failure raised."""
    return podman.exec(container, argv, user=user).exit_code


def make_mountpoints(
    podman: Podman,
    container: str,
    grants: Sequence[GrantRecord],
    *,
    realm_home: Path,
    host_home: Path,
) -> None:
    """Make the directories that the placeholders of `grants` sit in. The helper makes the rest.

    A realm path under `host_home` is a placeholder in `realm_home` on the host, so it is made here
    and the user keeps the directory. A parent under `/run/user/<uid>` is made as the realm user at
    mode 0700 (`_runtime_subdir`, FR-061). Any other parent is made in the container, as root, by
    `mkdir -p`. The helper then makes the placeholder itself, a directory or an empty file.
    """
    parents: dict[str, None] = {}
    for grant in grants:
        realm = Path(grant.realm)
        if realm == host_home or host_home in realm.parents:
            _make_home_point(realm_home, realm.relative_to(host_home), is_dir=grant.is_dir)
        else:
            parents[str(realm.parent)] = None
    roots = [parent for parent in parents if _RUNTIME_SUBDIR.fullmatch(parent) is None]
    if roots:
        _in_container(podman, container, ["mkdir", "-p", "--", *roots])
    for parent in parents:
        _runtime_subdir(podman, container, parent)


def _make_home_point(realm_home: Path, rel: Path, *, is_dir: bool) -> None:
    """Make one placeholder under the Realm Home. A symbolic link on the way is refused.

    Each part is checked with lstat, so a link planted in the Realm Home cannot send the write to
    the host outside Owned Paths (DR-010). The check and the write are not atomic, which leaves a
    race only for the realm itself, the one party that could plant a link.
    """
    parts = rel.parts if is_dir else rel.parts[:-1]
    current = realm_home
    try:
        for part in parts:
            current = current / part
            mode = _lstat_mode(current)
            if mode is None:
                os.mkdir(current, 0o755)
            elif not stat.S_ISDIR(mode):
                raise _failed(f"{current} is not a real directory under the Realm Home")
        if not is_dir:
            flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
            os.close(os.open(realm_home / rel, flags, 0o644))
    except OSError as exc:
        raise _failed(f"cannot make {current}: {exc.strerror or exc}") from exc


def _lstat_mode(path: Path) -> int | None:
    try:
        return os.lstat(path).st_mode
    except FileNotFoundError:
        return None


def _in_container(
    podman: Podman, container: str, argv: Sequence[str], *, user: str = "0:0"
) -> None:
    """Run one command in the realm, as container root unless `user` says otherwise (ADR-008)."""
    result = podman.exec(container, argv, user=user)
    if result.exit_code != 0:
        err = _failed(_decisive(result.stderr))
        err.details["exit"] = result.exit_code
        raise err


def _attach(
    podman: Podman,
    run: HelperRun,
    helper: str,
    pid: int,
    ops: Sequence[tuple[GrantRecord, Op]],
) -> list[_Row]:
    """Run the helper once for `ops`, and return one row per group, in argv order."""
    result = run(podman, rebind_argv(helper, pid, ops))
    if result.exit_code != 0:
        err = _failed(_decisive(result.stderr))
        err.details["exit"] = result.exit_code
        raise err
    lines = result.stdout.splitlines()
    if len(lines) != len(ops):
        raise _failed(f"the helper reported {len(lines)} of {len(ops)} groups")
    return [_row(index, text) for index, text in enumerate(lines)]


def _row(index: int, text: str) -> _Row:
    """Parse `<index> attached <dev> <ino>`, `<index> skipped`, or `<index> missing`."""
    parts = text.split()
    if not parts or parts[0] != str(index):
        raise _failed(f"unexpected helper output {text!r}")
    if len(parts) == 2 and parts[1] in ("skipped", "missing"):
        return _Row(parts[1])
    if len(parts) == 4 and parts[1] == "attached" and parts[2].isdigit() and parts[3].isdigit():
        return _Row("attached", SourceIdentity(int(parts[2]), int(parts[3])))
    raise _failed(f"unexpected helper output {text!r}")


def _with_session(grants: Sequence[GrantRecord], session: SessionEnv) -> list[GrantRecord]:
    """Each session grant takes its host from the current session, by its fixed realm path.

    A realm path that this session does not bind keeps its recorded host. A caller with no session
    variables (SSH, a TTY, cron) therefore leaves an attached grant alone, while its source exists.
    A source that is gone is still removed, because a missing source is missing (ADR-008, fixed
    names). A slot that was never bound keeps its empty host.
    """
    bound = {bind.realm: bind.host for bind in session.binds}
    return [
        replace(grant, host=bound.get(grant.realm, grant.host))
        if grant.kind == "session"
        else grant
        for grant in grants
    ]


def _refused(
    grants: Sequence[GrantRecord],
    current: Sequence[SourceIdentity | None],
    admit: Admit,
    indices: Sequence[int],
) -> dict[int, str]:
    """The grants among `indices` whose source is present and that `admit` refuses now."""
    refused: dict[int, str] = {}
    for index in indices:
        if current[index] is None:
            continue
        reason = admit(grants[index]) or admit(_resolved(grants[index]))
        if reason is not None:
            refused[index] = reason
    return refused


def _run(
    podman: Podman,
    container: str,
    *,
    pid: int,
    grants: Sequence[GrantRecord],
    ops: Sequence[tuple[int, Op]],
    points: Sequence[GrantRecord],
    realm_home: Path,
    host_home: Path,
    helper: str | None,
    run: HelperRun,
) -> list[_Row]:
    """Make the parent directories that `points` need, then run the helper for `ops`.

    Nothing runs when there is no group, so a realm with nothing to change sees no helper call.
    """
    if not ops:
        return []
    make_mountpoints(podman, container, points, realm_home=realm_home, host_home=host_home)
    attach_ops = [(_resolved(grants[i]), op) for i, op in ops]
    return _attach(podman, run, helper or helper_path(), pid, attach_ops)


def _settle(
    grants: Sequence[GrantRecord],
    current: Sequence[SourceIdentity | None],
    ops: Sequence[tuple[int, Op]],
    rows: Sequence[_Row],
    reasons: dict[int, str],
    children: Mapping[int, int],
) -> tuple[list[GrantRecord], list[int]]:
    """Fold the helper's rows into the grant list. Return it, and the grants to unmount.

    A grant the helper attached or found identical takes the identity read before the run. When the
    source was not there at the read, the helper's device and inode stand for it. A grant that is
    missing now, or that the helper found missing, has no identity, so it is tried again later. An
    attached tree that does not match is a NOT_VISIBLE skip, and it is listed for unmount. It does
    not match when its device and inode differ from the identity read, or when the realm shows
    fewer mounts below it than the identity lists (`children`, see `_visible_children`). More
    mounts in the realm are not a mismatch: a nested grant may be one. Such a grant keeps the
    identity read before the run as its hold key (`not_visible`), so `_held` stops the retries.
    """
    records = list(grants)
    mismatched: list[int] = []
    for (index, op), row in zip(ops, rows, strict=True):
        grant, now = grants[index], current[index]
        if op == "unmount" or row.status == "missing":
            records[index] = replace(grant, identity=None)
        elif row.status == "skipped":
            records[index] = replace(grant, identity=now, not_visible=None)
        else:
            seen = row.seen
            if seen is None:
                raise _failed(f"{grant.host} came back attached without an identity")
            if now is None:
                records[index] = replace(grant, identity=seen, not_visible=None)
            elif (seen.dev, seen.ino) != (now.dev, now.ino) or _fewer(children.get(index), now):
                reasons[index] = NOT_VISIBLE
                mismatched.append(index)
                records[index] = replace(grant, identity=None, not_visible=now)
            else:
                records[index] = replace(grant, identity=now, not_visible=None)
    return records, mismatched


def _fewer(shown: int | None, now: SourceIdentity) -> bool:
    """True when the realm shows fewer mounts below its path than the identity lists. None: not
    counted, which is the same as shown in full (no mounts below)."""
    return shown is not None and shown < len(now.below)


def _visible_children(
    podman: Podman,
    container: str,
    grants: Sequence[GrantRecord],
    current: Sequence[SourceIdentity | None],
    ops: Sequence[tuple[int, Op]],
    rows: Sequence[_Row],
) -> dict[int, int]:
    """The mounts the realm shows below each attached realm path, for the groups with mounts below.

    ADR-008, Resolution A. The identity lists the host mounts that hang from the source's mount, and
    the realm must show at least as many below the clone. One `cat` in the realm reads its mount
    table, and it runs only when a group with mounts below its source was attached.
    """
    checked = [
        index
        for (index, _), row in zip(ops, rows, strict=True)
        if row.status == "attached" and (now := current[index]) is not None and now.below
    ]
    if not checked:
        return {}
    result = podman.exec(container, ["cat", "/proc/self/mountinfo"], user="0:0")
    if result.exit_code != 0:
        raise _failed(_decisive(result.stderr))
    mounts = _mounts(result.stdout.encode())
    return {index: len(_below(mounts, grants[index].realm)) for index in checked}


def _held(grant: GrantRecord, now: SourceIdentity | None) -> bool:
    """True while the host identity is the one a NOT_VISIBLE attach was made against.

    Such a grant is not passed again, so a repeated mismatch costs no helper run at enter. It is
    passed again when the host identity changes (ADR-008, Resolution A).
    """
    return grant.not_visible is not None and now == grant.not_visible


def _unmount(
    podman: Podman,
    container: str,
    *,
    pid: int,
    grants: Sequence[GrantRecord],
    indices: Sequence[int],
    realm_home: Path,
    host_home: Path,
    helper: str | None,
    run: HelperRun,
) -> None:
    """Remove the mount that a group left on each realm path whose source is not visible (FR-064).

    A failure raises, so the caller does not record a mount it could not remove.
    """
    rows = _run(
        podman,
        container,
        pid=pid,
        grants=grants,
        ops=[(i, "unmount") for i in indices],
        points=[],
        realm_home=realm_home,
        host_home=host_home,
        helper=helper,
        run=run,
    )
    if any(row.status != "missing" for row in rows):
        raise _failed("a mount over a source that is not visible could not be removed")


def _skipped(records: Sequence[GrantRecord], reasons: Mapping[int, str]) -> list[Skipped]:
    """The grants that have no identity, each with its reason (FR-064 AC-1).

    A session slot with no host is not a skip, so it is left out here.
    """
    skipped: list[Skipped] = []
    for index, grant in enumerate(records):
        if grant.identity is not None or not grant.host:
            continue
        reason = reasons.get(index, "missing")
        skipped.append(
            Skipped(
                grant.kind,
                grant.host,
                reason,
                host=grant.host,
                realm=grant.realm,
                mode=grant.mode,
                is_dir=grant.is_dir,
                missing=reason == "missing",
            )
        )
    return skipped


def attach_at_start(
    podman: Podman,
    container: str,
    *,
    pid: int,
    grants: Sequence[GrantRecord],
    session: SessionEnv,
    realm_home: Path,
    host_home: Path,
    admit: Admit,
    helper: str | None = None,
    mountinfo: Path = MOUNTINFO,
    run: HelperRun | None = None,
) -> AttachResult:
    """Attach every grant after `podman start`. Each one is passed without --force.

    `session` gives the host of each session grant now. `admit` is applied to each present grant
    first, as named and as resolved. A missing source is skipped. A device or inode that differs
    from the host identity, or a realm that shows fewer mounts below it than the host does, is
    skipped with NOT_VISIBLE, and its mount is removed. The start goes on in both cases (FR-064).
    Any other failure raises GINN-E-ATTACH-FAILED, and the caller stops the realm again (ADR-008,
    T4.10 notes). `pid` is the container's init process, read after start. `run` runs the helper
    (see `run_on_host`).
    """
    runner = run or run_on_host
    grants = _with_session(grants, session)
    current = [read_identity(grant.host, mountinfo=mountinfo) for grant in grants]
    refused = _refused(grants, current, admit, range(len(grants)))
    ops: list[tuple[int, Op]] = [
        (i, "attach") for i, now in enumerate(current) if now is not None and i not in refused
    ]
    reasons: dict[int, str] = dict(refused)
    rows = _run(
        podman,
        container,
        pid=pid,
        grants=grants,
        ops=ops,
        points=[grants[i] for i, _ in ops if grants[i].identity is None],
        realm_home=realm_home,
        host_home=host_home,
        helper=helper,
        run=runner,
    )
    children = _visible_children(podman, container, grants, current, ops, rows)
    records, mismatched = _settle(grants, current, ops, rows, reasons, children)
    if mismatched:
        _unmount(
            podman,
            container,
            pid=pid,
            grants=grants,
            indices=mismatched,
            realm_home=realm_home,
            host_home=host_home,
            helper=helper,
            run=runner,
        )
    return AttachResult(records=records, skipped=_skipped(records, reasons))


@dataclass(frozen=True)
class _Decision:
    """What one re-attach would do, from the host alone. Nothing is changed to make it."""

    grants: list[GrantRecord]
    current: list[SourceIdentity | None]
    reasons: dict[int, str]
    ops: list[tuple[int, Op]]


def _decide(
    grants: Sequence[GrantRecord],
    session: SessionEnv,
    admit: Admit,
    mountinfo: Path,
) -> _Decision:
    """The helper groups of a re-attach, from one identity read per grant and one admission check.

    Shared by `reattach` and `needs_reattach`, so the pre-check never disagrees with the attach.
    """
    grants = _with_session(grants, session)
    current = [read_identity(grant.host, mountinfo=mountinfo) for grant in grants]
    held = [_held(grant, now) for grant, now in zip(grants, current, strict=True)]
    changed = [i for i, grant in enumerate(grants) if current[i] != grant.identity and not held[i]]
    # Every present grant is checked again, so a mount refused now goes, changed or not.
    refused = _refused(grants, current, admit, range(len(grants)))
    reasons: dict[int, str] = {i: NOT_VISIBLE for i, is_held in enumerate(held) if is_held}
    reasons.update(refused)
    ops: list[tuple[int, Op]] = []
    for index in sorted({*changed, *refused}):
        if index in refused:
            if grants[index].identity is not None:
                ops.append((index, "unmount"))
        elif not grants[index].host:
            ops.append((index, "unmount"))
        else:
            ops.append((index, "force"))
    return _Decision(grants=grants, current=current, reasons=reasons, ops=ops)


def needs_reattach(
    grants: Sequence[GrantRecord],
    session: SessionEnv,
    *,
    admit: Admit,
    mountinfo: Path = MOUNTINFO,
) -> bool:
    """True when `reattach` would call the helper or unmount a mount (ADR-008 pre-check).

    It is the same decision as `reattach`, made by the same code, and it changes nothing: it reads
    the host sources and runs the admission checks, and it makes no podman call. Enter runs it on
    every enter of a running realm, so the common case, nothing changed, costs only these reads.
    """
    return bool(_decide(grants, session, admit, mountinfo).ops)


def reattach(
    podman: Podman,
    container: str,
    *,
    pid: int,
    grants: Sequence[GrantRecord],
    session: SessionEnv,
    realm_home: Path,
    host_home: Path,
    admit: Admit,
    helper: str | None = None,
    mountinfo: Path = MOUNTINFO,
    run: HelperRun | None = None,
) -> AttachResult:
    """Re-attach the grants whose host identity changed since their last attach (ADR-008 amendment).

    Enter and launch call this before exec, with the session that is current. Only a changed grant
    is passed, with --force, and an unchanged grant makes no helper call. A grant whose source is
    now gone is passed too, so the helper removes its old mount. A grant that was attached before
    and is refused now is unmounted and skipped with the refusal. A device or inode that differs
    from the host identity, or a realm that shows fewer mounts below it than the host does, is
    skipped with NOT_VISIBLE and unmounted. Such a grant is held until its host identity changes, so
    the next enter costs no helper run while nothing changed. Any other failure raises, and the
    caller reports it without stopping the realm: a services-on realm is stopped only by its `sv`
    service (FR-081).
    """
    runner = run or run_on_host
    decision = _decide(grants, session, admit, mountinfo)
    checked = decision.grants
    forced = [i for i, op in decision.ops if op == "force" and checked[i].identity is None]
    rows = _run(
        podman,
        container,
        pid=pid,
        grants=checked,
        ops=decision.ops,
        points=[checked[i] for i in forced],
        realm_home=realm_home,
        host_home=host_home,
        helper=helper,
        run=runner,
    )
    children = _visible_children(podman, container, checked, decision.current, decision.ops, rows)
    records, mismatched = _settle(
        checked, decision.current, decision.ops, rows, decision.reasons, children
    )
    if mismatched:
        _unmount(
            podman,
            container,
            pid=pid,
            grants=checked,
            indices=mismatched,
            realm_home=realm_home,
            host_home=host_home,
            helper=helper,
            run=runner,
        )
    return AttachResult(records=records, skipped=_skipped(records, decision.reasons))
