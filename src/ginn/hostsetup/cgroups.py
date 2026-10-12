# SPDX-License-Identifier: Apache-2.0
"""cgroup v2 delegation for the user's services (FR-001, FR-007, FR-080; spike S-01).

Layout created by `delegate_user_tree`, with `<uid>` the user:

    <root>/ginn/                  controllers enabled, root-owned
    <root>/ginn/<uid>/            controllers enabled, chowned to uid:gid
    <root>/ginn/<uid>/supervisor/ leaf for the runit supervisor, root-owned

A cgroup that has controllers in subtree_control cannot hold processes, so the processes sit in
the leaves. Only `<root>/ginn/<uid>` and its cgroup.procs, cgroup.threads and cgroup.subtree_control
are chowned. The supervisor leaf stays root-owned: cgroup v2 migration needs write access to the
common ancestor and to the destination, not to the source leaf, so nothing in the leaf has to be
written by the user (S-01, ADR-003). Limit files such as memory.max stay root-owned, so the user
cannot raise its own limits.

NOTE: there is deliberately no leaf for `ginn start` or `podman start`. A session process cannot
move itself into <root>/ginn/<uid>/...: cgroup v2 migration needs write access to cgroup.procs of
the common ancestor, which is the cgroup root here, and that file is root-owned. The placement is
made once, by the Host Setup run script (hostsetup/runit_system.py, step 3). As root, the script
writes its own pid into <root>/ginn/<uid>/supervisor/cgroup.procs, then execs runsvdir, so every
service that runsvdir starts is in the delegated subtree. A services-on realm is started only
through the supervisor, with `--cgroup-parent /ginn/<uid>/<realm>` (ADR-003). `move_self_to` does
the same write for a given leaf. Only tests call it today.

All kernel I/O goes through `_read_text`, `_write_control` and `_make_dir`. Tests replace these to
emulate cgroupfs, because cgroupfs semantics (accumulating `+ctrl` writes, auto-created
interface files) cannot be reproduced on a plain temporary directory.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")
CONTROLLERS: frozenset[str] = frozenset({"cpu", "cpuset", "io", "memory", "pids"})
GINN_DIR = "ginn"
SUPERVISOR_LEAF = "supervisor"
USER_CHOWN_FILES: tuple[str, ...] = ("cgroup.procs", "cgroup.threads", "cgroup.subtree_control")
TOLERATED_ROOT_ERRNOS: frozenset[int] = frozenset({errno.EBUSY, errno.EACCES})


@dataclass
class DelegationPlan:
    """Everything `delegate_user_tree` did, for the Host Setup Record (DR-009) and undo (FR-003).

    `pre_images` maps each control file written to its content before the first write.
    `enabled` maps each node to the controllers this call added there. `created` lists only the
    directories this call made. Directories that already existed are not listed, so undo never
    removes them. `root_refused` is True when the root's subtree_control write was refused with a
    tolerated errno. The record keeps it, and `verify` reads it.
    """

    uid: int
    gid: int
    user_dir: Path
    supervisor: Path
    created: list[Path] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
    chowned: list[Path] = field(default_factory=list)
    enabled: dict[Path, frozenset[str]] = field(default_factory=dict)
    pre_images: dict[Path, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    root_refused: bool = False


@dataclass(frozen=True)
class Verification:
    """Result of `verify`. `problems` make `ginn doctor` fail. `warnings` are reported only."""

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def chown_groups(cgroup_root: Path, uid: int) -> tuple[tuple[Path, ...], ...]:
    """Return the paths chowned to the user, one tuple per directory, in chown order.

    Only `ginn/<uid>` and its three files are listed. The supervisor leaf is not, because it stays
    root-owned (see the module docstring). The runit renderer reads these same groups, so the two
    cannot drift apart.
    """
    user_dir = cgroup_root / GINN_DIR / str(uid)
    return ((user_dir, *(user_dir / name for name in USER_CHOWN_FILES)),)


def chown_paths(cgroup_root: Path, uid: int) -> tuple[Path, ...]:
    """Return every path chowned to the user, flattened from `chown_groups`."""
    return tuple(path for group in chown_groups(cgroup_root, uid) for path in group)


def read_controllers(path: Path) -> frozenset[str]:
    """Controllers that the node at `path` offers (its cgroup.controllers)."""
    return frozenset(_read_text(path / "cgroup.controllers").split())


def read_subtree_control(path: Path) -> frozenset[str]:
    """Controllers that the node at `path` has enabled for its children."""
    return frozenset(_read_text(path / "cgroup.subtree_control").split())


def enable_controllers(path: Path, wanted: Iterable[str]) -> frozenset[str]:
    """Enable each wanted controller that the node offers and does not have yet.

    Writes one `+ctrl` per controller, as the kernel expects, and returns the set actually added.
    A second call adds nothing. If a write fails, the exception propagates, and the controllers
    written before it stay enabled.
    """
    offered = read_controllers(path)
    enabled = read_subtree_control(path)
    added: set[str] = set()
    for ctrl in sorted(set(wanted) & (offered - enabled)):
        _write_control(path / "cgroup.subtree_control", f"+{ctrl}")
        added.add(ctrl)
    return frozenset(added)


def delegate_user_tree(cgroup_root: Path, uid: int, gid: int) -> DelegationPlan:
    """Create and delegate `ginn/<uid>` with its supervisor leaf, as S-01 showed is required.

    The leaf is created by root and is not chowned: the run script writes its pid into the leaf as
    root, and the user never needs to write there (see the module docstring).

    The root `subtree_control` write may fail with EBUSY or EACCES. That sets `root_refused`,
    is recorded in `warnings`, and the rest goes on, so `pids` may be missing and is reported
    there. Any other failure propagates. Running the function again is safe: nothing is created
    twice, nothing is written when controllers are already enabled, and the chown is repeated.
    """
    if uid <= 0 or gid <= 0:
        raise ValueError(f"refusing to delegate a cgroup subtree to uid {uid} gid {gid}")
    ginn = cgroup_root / GINN_DIR
    user_dir = ginn / str(uid)
    supervisor = user_dir / SUPERVISOR_LEAF
    plan = DelegationPlan(uid=uid, gid=gid, user_dir=user_dir, supervisor=supervisor)

    try:
        _enable_recorded(cgroup_root, plan)
    except OSError as exc:
        if exc.errno not in TOLERATED_ROOT_ERRNOS:
            raise
        plan.root_refused = True
        plan.warnings.append(
            f"{cgroup_root / 'cgroup.subtree_control'} not written "
            f"({errno.errorcode.get(exc.errno, exc.errno)}); "
            "controllers missing at root are not delegated"
        )

    _make_recorded(ginn, plan)
    _enable_recorded(ginn, plan)
    _make_recorded(user_dir, plan)
    _make_recorded(supervisor, plan)
    _enable_recorded(user_dir, plan)

    for path in chown_paths(cgroup_root, uid):
        os.chown(path, uid, gid)
        plan.chowned.append(path)

    missing = sorted(CONTROLLERS - read_controllers(user_dir))
    if missing:
        plan.warnings.append(
            f"controllers not offered below {ginn} and not delegated: {', '.join(missing)}"
        )
    return plan


def verify(cgroup_root: Path, uid: int, *, tolerated_root: bool = False) -> Verification:
    """Check the delegated tree for `uid`. An empty `problems` list means it is fine.

    `tolerated_root` is `DelegationPlan.root_refused` from the Host Setup Record. When it is True,
    controllers the root did not enable are reported as warnings, because delegation accepted
    that refusal. Every other gap is a problem. Each message names the path that is wrong. This
    is the check `ginn doctor` uses (FR-007).
    """
    if not (cgroup_root / "cgroup.controllers").exists():
        return Verification(problems=[f"{cgroup_root}: cgroup v2 is not mounted"])
    result = Verification()
    ginn = cgroup_root / GINN_DIR
    user_dir = ginn / str(uid)
    root_gaps = _missing_enabled(cgroup_root, "root")
    if tolerated_root:
        result.warnings.extend(root_gaps)
    else:
        result.problems.extend(root_gaps)
    if not ginn.is_dir():
        result.problems.append(f"{ginn}: missing; run 'sudo ginn host-setup --user <name>'")
    else:
        result.problems.extend(_missing_enabled(ginn, str(ginn)))
    if not user_dir.is_dir():
        result.problems.append(f"{user_dir}: missing; run 'sudo ginn host-setup --user <name>'")
        return result
    result.problems.extend(_missing_enabled(user_dir, str(user_dir)))
    for path in chown_paths(cgroup_root, uid):
        if not path.exists():
            result.problems.append(f"{path}: missing")
            continue
        owner = os.stat(path).st_uid
        if owner != uid:
            result.problems.append(f"{path}: owned by uid {owner}, expected {uid}")
    supervisor = user_dir / SUPERVISOR_LEAF
    if not supervisor.is_dir():
        result.problems.append(f"{supervisor}: missing")
    return result


def move_self_to(leaf: Path) -> None:
    """Move the calling process into `leaf` by writing its pid to cgroup.procs (used by M4)."""
    _write_control(leaf / "cgroup.procs", str(os.getpid()))


def _missing_enabled(node: Path, label: str) -> list[str]:
    offered = read_controllers(node) & CONTROLLERS
    enabled = read_subtree_control(node)
    return [
        f"{label}: controller {ctrl} is not enabled in subtree_control"
        for ctrl in sorted(offered - enabled)
    ]


def _enable_recorded(path: Path, plan: DelegationPlan) -> None:
    control = path / "cgroup.subtree_control"
    plan.pre_images.setdefault(control, _read_text(control))
    added = enable_controllers(path, CONTROLLERS)
    if added:
        plan.written.append(control)
        plan.enabled[path] = added


def _make_recorded(path: Path, plan: DelegationPlan) -> None:
    if _make_dir(path):
        plan.created.append(path)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _write_control(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, text.encode("ascii"))
    finally:
        os.close(fd)


def _make_dir(path: Path) -> bool:
    """Create a cgroup directory. Return False if it already exists."""
    try:
        os.mkdir(path)
    except FileExistsError:
        return False
    return True
