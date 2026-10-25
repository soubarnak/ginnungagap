# SPDX-License-Identifier: Apache-2.0
"""Basic-tier mounts for one realm (FR-061, FR-064, FR-065, FR-048, IR-043, AS-009, DR-004).

The plan has two parts, as ADR-008 (amended 2026-10-12) decides.
- `create_binds` is what podman binds at `podman create`. Only stable sources: the Realm Home at
  the host home path, the host fonts and timezone (read-only) while `fonts_timezone` is on, the GPU
  device nodes while `gpu` is on, and the `--tmpfs` for the realm's runtime directory (FR-061,
  FR-063). A fixed path that is a symlink is checked at its
  target, but the bind source is the literal path, because podman resolves it at each start.
- `attach_plan` is the variable grants: `~/Downloads`, the external mount roots, and the session
  paths. ginn-rebind (ADR-008) attaches them into the running realm after `podman start`, and again
  at enter and launch when a host source has changed. Each attach entry carries its mode, and the
  helper applies a read-only mode when it attaches.

Both are pure planning. They run no podman and change nothing on the host. The only filesystem
reads are the `exists` calls and `os.path.realpath`. A missing folder is reported and never
created (FR-064).

A grant whose source is missing, forbidden or outside the allowed roots is skipped and recorded in
a `Skipped` with its rule id, and the rest of the plan is still built. Only malformed input raises:
a relative home, a bad user name, a Realm Home outside Owned Paths (DR-021), a session path that is
not absolute. Development adds `~/Projects` and the shell startup files (FR-062), and Custom takes
its toggles and shares (FR-063). A share is refused with GINN-E-PATH-FORBIDDEN when `perms` adds
it, so a share refused here is a skip with its reason, as the other grants are.

Each grant sits at the same absolute path inside the realm as on the host (FR-065), with two
exceptions. The Realm Home is mounted at the host home path (FR-048), from its resolved source
under Owned Paths. The session paths sit under `/run/user/<uid>`, the realm's runtime directory,
at fixed names: the Wayland socket, the authority file, and PipeWire and Pulse at their standard
names. `/tmp/.X11-unix` keeps its own path. runtime/env.py chooses the names.

`AttachPlan.mountpoints` lists the realm-side paths that must exist before each attach, parents
first. A `realm_home` entry is a directory that ginn makes in the Realm Home on the host, under
Owned Paths, before start. A `container` entry is made inside the running realm after start.
"""

import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from ginn.domain.enums import Tier
from ginn.domain.paths import ROOTS_REASON, Mode, SharedPath, path_refusal, under_allowed_root
from ginn.domain.permissions import (
    BASIC_TOGGLES,
    CustomShare,
    Permissions,
    Toggle,
    effective_toggles,
)
from ginn.errors import GinnError
from ginn.podman.adapter import Mount

Exists = Callable[[str], bool]
"""Filesystem view for the skip decisions. Tests pass a fake, and the default reads the host."""
IsDir = Callable[[str], bool]
"""Whether a host path is a directory. It sets the placeholder kind of a Custom share."""

Location = Literal["realm_home", "container"]

DOWNLOADS: Final = "Downloads"
PROJECTS: Final = "Projects"
SHELL_RC_FILES: Final = (".bashrc", ".zshrc")
USER_MEDIA_ROOT: Final = "/run/media"
MOUNT_ROOTS: Final = ("/media", "/mnt")
FONTS: Final = "/usr/share/fonts"
LOCALTIME: Final = "/etc/localtime"
GPU_DEVICE: Final = "/dev/dri"
RUNTIME_PARENT: Final = "/run/user"
_IMAGE_DIRS: Final = ("/tmp",)  # noqa: S108 - the image directory, not a temp file
"""Directories the image holds, so an attach does not list them as mountpoints."""
SYMLINK_ROOT_REASON: Final = "a symbolic link, not a mount root"
INTERNAL_KIND: Final = "internal"
"""The grant kind of a ginn-made socket under the Owned runtime root (ADR-008, CL-14)."""
_USER_NAME: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class SessionBind:
    """A host session path and its fixed realm-side name (ADR-008 pairs share this shape).

    Both paths are absolute. `mode` is "ro" or "rw". `is_dir` is True for a directory, such as the
    X11 socket directory. A socket or the authority file is a file. runtime/env.py builds these.
    """

    host: str
    realm: str
    mode: Mode = "rw"
    is_dir: bool = False
    internal: bool = False
    """True for a socket that ginn made under its own runtime root (ADR-008, CL-14). Such a source
    is admitted by its typed grant (kind `internal`), which DR-004's Owned Path rule does not apply
    to, because it governs shares that the user chooses, not ginn's own sockets."""


@dataclass(frozen=True)
class Skipped:
    """A grant that was not made. `name` is the name the user sees, such as `~/Downloads`.

    A grant skipped for a missing source also names what a later enter or launch needs to try it
    again: `host`, `realm`, `mode`, `is_dir`, and `missing=True` (FR-064, FR-066, ADR-008). A skip
    for any other reason, such as a refused path or a source of the wrong type, leaves the realm
    fields empty, so it is never retried.
    """

    kind: str
    name: str
    reason: str
    host: str = ""
    realm: str = ""
    mode: Mode = "rw"
    is_dir: bool = False
    missing: bool = False

    def message(self) -> str:
        return f"{self.kind} skipped: {self.name} ({self.reason})"


@dataclass(frozen=True)
class CreateBinds:
    """The `podman create` part of the plan. `mounts` is parent-first. `tmpfs` holds `--tmpfs`."""

    mounts: list[Mount]
    devices: list[str]
    tmpfs: list[str]
    skipped: list[Skipped]


@dataclass(frozen=True)
class Attach:
    """One variable grant, attached into the running realm after start.

    `kind` is "share" for a folder or a mount root, and "session" for a display, audio or
    authority path. `host` is the resolved source that was validated. `realm` is the target inside
    the realm, and `mode` is "ro" or "rw".
    """

    kind: str
    host: str
    realm: str
    mode: Mode


@dataclass(frozen=True)
class Mountpoint:
    """A realm-side path that must exist before its attach. `is_dir` says which kind to make.

    `location` says where it is made. "realm_home" is a directory in the Realm Home on the host,
    under Owned Paths, made before start. "container" is made inside the running realm, after start.
    """

    path: str
    is_dir: bool
    location: Location


@dataclass(frozen=True)
class AttachPlan:
    """The variable-grant part of the plan. `mountpoints` lists parents before children."""

    attach: list[Attach]
    mountpoints: list[Mountpoint]
    skipped: list[Skipped]


_Grant = tuple[Attach, list[Mountpoint]]


def runtime_dir(uid: int) -> str:
    """The realm's XDG_RUNTIME_DIR, `/run/user/<uid>`. The session paths sit under it."""
    return f"{RUNTIME_PARENT}/{uid}"


def create_binds(
    tier: Tier,
    *,
    host_home: Path,
    uid: int,
    realm_home: Path,
    owned_paths: Sequence[Path],
    env: Mapping[str, str],
    exists: Exists = os.path.exists,
    toggles: frozenset[Toggle] = BASIC_TOGGLES,
) -> CreateBinds:
    """The stable binds for `podman create`. `env` carries the AS-011 XDG overrides.

    `realm_home` must lie inside `owned_paths` by its resolved path (DR-021). That resolved path
    is the mount source, so the bind is exactly what was checked. It is mounted at `host_home`
    (FR-048). `toggles` are the effective toggles of the realm (FR-061, FR-063): the fonts and
    timezone binds need FONTS_TIMEZONE, and the GPU device needs GPU. A toggle that is off plans
    no bind and reports nothing for it.
    """
    _check_tier(tier)
    _check_home(host_home)
    source = _realm_home_source(realm_home, owned_paths)
    mounts = [Mount("bind", str(source), str(host_home), ())]
    skipped: list[Skipped] = []
    fixed_paths = (FONTS, LOCALTIME) if Toggle.FONTS_TIMEZONE in toggles else ()
    for fixed in fixed_paths:
        entry = _fixed_bind(fixed, home=host_home, owned_paths=owned_paths, env=env, exists=exists)
        if isinstance(entry, Skipped):
            skipped.append(entry)
        else:
            mounts.append(entry)
    devices: list[str] = []
    if Toggle.GPU in toggles:
        if exists(GPU_DEVICE):
            devices.append(GPU_DEVICE)
        else:
            skipped.append(Skipped("device", GPU_DEVICE, "missing"))
    return CreateBinds(
        mounts=mounts,
        devices=devices,
        tmpfs=[f"{runtime_dir(uid)}:mode=0700,U"],
        skipped=skipped,
    )


def attach_plan(
    tier: Tier,
    *,
    host_home: Path,
    uid: int,
    user: str,
    owned_paths: Sequence[Path],
    runtime_root: Path,
    session_binds: Sequence[SessionBind],
    env: Mapping[str, str],
    exists: Exists = os.path.exists,
    permissions: Permissions | None = None,
    is_dir: IsDir = os.path.isdir,
) -> AttachPlan:
    """The variable grants for a running realm. Each is checked now, and skipped when refused.

    The grants follow the toggles of `tier` (domain/permissions.py, FR-061..063): Basic gets
    Downloads and the mount roots, Development adds `~/Projects` and the shell startup files, and
    Custom gets the toggles it stores and its shares. `permissions` is read only for Custom. A
    share is checked again here, so a refused one is a skip with its reason. `session_binds` come
    from runtime/env.py. Each one keeps its fixed realm name. A grant that another grant already
    names is skipped, because two binds cannot share one realm path. `runtime_root` is ginn's own
    runtime root, which is the only place an internal grant (a ginn-made socket) may come from.
    """
    _check_tier(tier)
    _check_home(host_home)
    _check_user(user)
    custom = Permissions() if permissions is None else permissions
    toggles = effective_toggles(tier, custom.toggles)
    shares = custom.shares if tier is Tier.CUSTOM else ()
    grants: list[_Grant | Skipped | None] = []
    if Toggle.DOWNLOADS in toggles:
        grants.append(
            _folder(
                f"~/{DOWNLOADS}",
                host_home / DOWNLOADS,
                realm=str(host_home / DOWNLOADS),
                parents=(),
                home=host_home,
                owned_paths=owned_paths,
                env=env,
                exists=exists,
            )
        )
    if Toggle.MOUNT_ROOTS in toggles:
        grants.append(
            _folder(
                f"{USER_MEDIA_ROOT}/{user}",
                Path(USER_MEDIA_ROOT) / user,
                realm=f"{USER_MEDIA_ROOT}/{user}",
                parents=(USER_MEDIA_ROOT,),
                home=host_home,
                owned_paths=owned_paths,
                env=env,
                exists=exists,
            )
        )
        for root in MOUNT_ROOTS:
            grants.append(
                _mount_root(root, home=host_home, owned_paths=owned_paths, env=env, exists=exists)
            )
    if Toggle.PROJECTS in toggles:
        grants.append(
            _folder(
                f"~/{PROJECTS}",
                host_home / PROJECTS,
                realm=str(host_home / PROJECTS),
                parents=(),
                home=host_home,
                owned_paths=owned_paths,
                env=env,
                exists=exists,
            )
        )
    if Toggle.SHELL_RC in toggles:
        for rc_name in SHELL_RC_FILES:
            grants.append(
                _startup_file(
                    rc_name, host_home=host_home, owned_paths=owned_paths, env=env, exists=exists
                )
            )
    for share in shares:
        grants.append(
            _share(
                share,
                home=host_home,
                owned_paths=owned_paths,
                runtime_root=runtime_root,
                env=env,
                exists=exists,
                is_dir=is_dir,
            )
        )
    for bind in session_binds:
        grants.append(
            _session(
                bind,
                uid=uid,
                home=host_home,
                owned_paths=owned_paths,
                runtime_root=runtime_root,
                env=env,
                exists=exists,
            )
        )
    attach: list[Attach] = []
    points: list[Mountpoint] = []
    skipped: list[Skipped] = []
    realms: set[str] = set()
    for grant in grants:
        if grant is None:
            continue
        if isinstance(grant, Skipped):
            skipped.append(grant)
            continue
        entry, needed = grant
        if entry.realm in realms:
            skipped.append(Skipped("share", entry.host, "already granted by another grant"))
            continue
        realms.add(entry.realm)
        attach.append(entry)
        points.extend(point for point in needed if point not in points)
    return AttachPlan(attach=attach, mountpoints=points, skipped=skipped)


def _check_tier(tier: Tier) -> None:
    if not isinstance(tier, Tier):
        raise TypeError(f"tier must be a Tier, not {type(tier).__name__}")


def _check_home(host_home: Path) -> None:
    if not host_home.is_absolute():
        raise ValueError(f"host home must be absolute, not {host_home}")


def _check_user(user: str) -> None:
    if _USER_NAME.fullmatch(user) is None:
        raise ValueError(f"user name {user!r} is not one path component")


def _real(path: str | Path) -> Path:
    return Path(os.path.realpath(path))


def _realm_home_source(realm_home: Path, owned_paths: Sequence[Path]) -> Path:
    """The resolved Realm Home. It must lie strictly inside an Owned Path (DR-021).

    The check uses resolved paths, so `..` and symlinks cannot take it outside an Owned Path.
    """
    if not realm_home.is_absolute():
        raise ValueError(f"Realm Home must be absolute, not {realm_home}")
    source = _real(realm_home)
    if not any(_real(root) in source.parents for root in owned_paths):
        raise ValueError(f"Realm Home {realm_home} must lie inside Owned Paths (DR-021)")
    return source


def _location(path: str, home: Path) -> Location:
    return "realm_home" if home in Path(path).parents else "container"


def _points(realm: str, *, is_dir: bool, parents: Sequence[str], home: Path) -> list[Mountpoint]:
    """The mountpoints for one attach: its missing parents first, then the target itself."""
    chain = [Mountpoint(parent, True, _location(parent, home)) for parent in parents]
    chain.append(Mountpoint(realm, is_dir, _location(realm, home)))
    return chain


def _fixed_bind(
    path: str,
    *,
    home: Path,
    owned_paths: Sequence[Path],
    env: Mapping[str, str],
    exists: Exists,
) -> Mount | Skipped:
    """A fixed system path, read-only, bound at the same path (AS-009).

    The check runs on the resolved path, so a link into a forbidden place is refused. The bind
    source is the literal path. Podman resolves a link again at each start, so the source stays
    the path ginn was asked for (ADR-008 amendment).
    """
    if not exists(path):
        return Skipped("share", path, "missing")
    resolved = os.path.realpath(path)
    reason = path_refusal(
        resolved, env=env, home=home, owned_paths=owned_paths, mode="ro", fixed=path
    )
    if reason is not None:
        return Skipped("share", path, reason)
    return Mount("bind", path, path, ("ro",))


def _folder(
    name: str,
    raw: Path,
    *,
    realm: str,
    parents: Sequence[str],
    home: Path,
    owned_paths: Sequence[Path],
    env: Mapping[str, str],
    exists: Exists,
) -> _Grant | Skipped:
    """A user folder, read-write. `SharedPath.parse` checks it, so DR-004 applies in full."""
    if not exists(str(raw)):
        return Skipped("share", name, "missing", str(raw), realm, "rw", True, missing=True)
    try:
        shared = SharedPath.parse(str(raw), home=home, owned_paths=owned_paths, mode="rw", env=env)
    except GinnError as err:
        return Skipped("share", name, str(err.details["reason"]))
    entry = Attach("share", str(shared.path), realm, "rw")
    return entry, _points(realm, is_dir=True, parents=parents, home=home)


def _mount_root(
    root: str,
    *,
    home: Path,
    owned_paths: Sequence[Path],
    env: Mapping[str, str],
    exists: Exists,
) -> _Grant | Skipped:
    """A fixed mount root, read-write (FR-061). It is attached as itself, so it may be the target.

    A root that is itself a symbolic link is skipped. A link such as `/mnt -> /var` would attach
    whatever it points at, and that target can lie inside an allowed root. The target still has to
    pass the DR-004 check and lie inside an allowed root.
    """
    if not exists(root):
        return Skipped("share", root, "missing", root, root, "rw", True, missing=True)
    if _real(root) != Path(root):
        return Skipped("share", root, SYMLINK_ROOT_REASON)
    target = _real(root)
    reason = path_refusal(target, env=env, home=home, owned_paths=owned_paths, mode="rw")
    if reason is None and not under_allowed_root(target, home=home, root_itself=True):
        reason = ROOTS_REASON
    if reason is not None:
        return Skipped("share", root, reason)
    entry = Attach("share", str(target), root, "rw")
    return entry, _points(root, is_dir=True, parents=(), home=home)


def _session(
    bind: SessionBind,
    *,
    uid: int,
    home: Path,
    owned_paths: Sequence[Path],
    runtime_root: Path,
    env: Mapping[str, str],
    exists: Exists,
) -> _Grant | Skipped:
    """A session path from runtime/env.py, with the full DR-004 check (subtree included)."""
    if not os.path.isabs(bind.host) or not bind.realm.startswith("/"):
        raise ValueError(f"session bind must use absolute paths: {bind!r}")
    kind = INTERNAL_KIND if bind.internal else "session"
    if not exists(bind.host):
        return Skipped(
            kind, bind.host, "missing", bind.host, bind.realm, bind.mode, bind.is_dir, True
        )
    target = _real(bind.host)
    if bind.internal:
        reason = _internal_refusal(
            target, runtime_root=runtime_root, env=env, home=home, mode=bind.mode
        )
    else:
        reason = path_refusal(target, env=env, home=home, owned_paths=owned_paths, mode=bind.mode)
    if reason is not None:
        return Skipped(kind, bind.host, reason)
    entry = Attach(kind, str(target), bind.realm, bind.mode)
    parents = _session_parents(bind.realm, uid)
    return entry, _points(bind.realm, is_dir=bind.is_dir, parents=parents, home=home)


def _startup_file(
    rc_name: str,
    *,
    host_home: Path,
    owned_paths: Sequence[Path],
    env: Mapping[str, str],
    exists: Exists,
) -> _Grant | Skipped | None:
    """A shell startup file, read-only (FR-062, AS-010). A missing one is no grant and no skip.

    The file is optional: most hosts lack some of them, and a skip for each would only add noise
    to `ginn status`. The next start plans it again, so it is attached once the file exists.
    """
    raw = host_home / rc_name
    if not exists(str(raw)):
        return None
    try:
        shared = SharedPath.parse(
            str(raw), home=host_home, owned_paths=owned_paths, mode="ro", env=env
        )
    except GinnError as err:
        return Skipped("share", f"~/{rc_name}", str(err.details["reason"]))
    realm = str(raw)
    entry = Attach("share", str(shared.path), realm, "ro")
    return entry, _points(realm, is_dir=False, parents=(), home=host_home)


def _share(
    share: CustomShare,
    *,
    home: Path,
    owned_paths: Sequence[Path],
    runtime_root: Path,
    env: Mapping[str, str],
    exists: Exists,
    is_dir: IsDir,
) -> _Grant | Skipped:
    """A Custom share (FR-063), at the same path in the realm (FR-065), with its own mode.

    `perms` checked the path when it was added, with GINN-E-PATH-FORBIDDEN. The check runs again
    here, since a link may now point elsewhere. A missing source is a skip that the next enter or
    launch retries (FR-064), and a refused one is a skip that is not retried.
    """
    raw = share.path
    if not exists(raw):
        return Skipped("share", raw, "missing", raw, raw, share.mode, True, missing=True)
    reason = recheck(
        "share",
        raw,
        raw,
        share.mode,
        home=home,
        owned_paths=owned_paths,
        runtime_root=runtime_root,
        env=env,
    )
    if reason is not None:
        return Skipped("share", raw, reason)
    entry = Attach("share", raw, raw, share.mode)
    parents = _share_parents(raw, home)
    return entry, _points(raw, is_dir=is_dir(raw), parents=parents, home=home)


def _share_parents(realm: str, home: Path) -> tuple[str, ...]:
    """The directory above a Custom share, unless it is `/` or the Realm Home itself."""
    parent = os.path.dirname(realm)
    return () if parent in ("/", str(home)) else (parent,)


def _internal_refusal(
    target: Path,
    *,
    runtime_root: Path,
    env: Mapping[str, str],
    home: Path,
    mode: Mode,
) -> str | None:
    """The refusal of a ginn-made socket: a socket under ginn's runtime root (DR-004, CL-14).

    The Owned Path rule of DR-004 is not applied to it (ADR-008, CL-14): the socket is ginn's own.
    It must lie under the runtime root, resolved, and be a socket (`S_ISSOCK`, lstat of the
    resolved target), so no other Owned Path, such as the state root or the audit log, can stand
    in for it. Every other rule of `path_refusal` still applies, with no Owned Path list.

    The relay's socket and its lock directory are ginn's too, but they are not clients' sockets:
    a bind of either one would hand a container the relay's control (ADR-014). So they are refused
    by name, before the socket test.
    """
    root = Path(os.path.realpath(runtime_root))
    if root not in target.parents:
        return "not under the ginn runtime root (ADR-008)"
    if _is_exec_relay_path(target, root):
        return "the exec relay's socket or lock directory, which only ginn uses (ADR-014)"
    if not _is_socket(target):
        return "not a socket, and the ginn runtime root holds only ginn's sockets (ADR-008)"
    return path_refusal(target, env=env, home=home, owned_paths=(), mode=mode)


def _is_socket(path: Path) -> bool:
    """True when `path` is a socket node. A path that cannot be read is not one."""
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_exec_relay_path(target: Path, root: Path) -> bool:
    """True for the relay's socket, `<root>/exec.sock`, and the lock directory and all below it."""
    # Imported here: services/exec_relay reaches this module through runtime/spec (a cycle).
    from ginn.services.exec_relay import LOCK_DIR_NAME, SOCKET_NAME

    lock_dir = root / LOCK_DIR_NAME
    return target == root / SOCKET_NAME or target == lock_dir or lock_dir in target.parents


def _session_parents(realm: str, uid: int) -> tuple[str, ...]:
    """The directory above a session path, unless it is the runtime dir or an image directory."""
    parent = os.path.dirname(realm)
    return () if parent in (runtime_dir(uid), *_IMAGE_DIRS) else (parent,)


def _is_mount_root(realm: str) -> bool:
    """True for `/media`, `/mnt`, and the user's `/run/media/<user>`."""
    user_root = realm.startswith(f"{USER_MEDIA_ROOT}/") and realm.count("/") == 3
    return realm in MOUNT_ROOTS or user_root


def recheck(
    kind: str,
    host: str,
    realm: str,
    mode: Mode,
    *,
    home: Path,
    owned_paths: Sequence[Path],
    runtime_root: Path,
    env: Mapping[str, str],
) -> str | None:
    """The DR-004 rules for one grant, applied again before it is attached after start.

    The plan checked the grant once, but its host path may now resolve somewhere else: a link can
    be retargeted, or a folder replaced by one. So every grant that an enter or launch is about to
    attach is checked again. The rules are those of plan time: a session path by the path rule on
    its resolved target, a folder by `SharedPath.parse`, and a mount root by the link rule, then the
    path rule, then the allowed-root rule. Returns the refusal reason, or None if it may go.
    """
    if kind == "session":
        resolved = os.path.realpath(host)
        return path_refusal(resolved, env=env, home=home, owned_paths=owned_paths, mode=mode)
    if kind == INTERNAL_KIND:
        resolved_internal = Path(os.path.realpath(host))
        return _internal_refusal(
            resolved_internal, runtime_root=runtime_root, env=env, home=home, mode=mode
        )
    if _is_mount_root(realm):
        if os.path.realpath(host) != host:
            return SYMLINK_ROOT_REASON
        target = _real(host)
        reason = path_refusal(target, env=env, home=home, owned_paths=owned_paths, mode=mode)
        if reason is None and not under_allowed_root(target, home=home, root_itself=True):
            reason = ROOTS_REASON
        return reason
    try:
        SharedPath.parse(host, home=home, owned_paths=owned_paths, mode=mode, env=env)
    except GinnError as err:
        return str(err.details.get("reason", err.message))
    return None
