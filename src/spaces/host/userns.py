"""User namespace for LXC guests (root in the guest is not host root).

Without it root in a space is host root behind a seccomp filter, a capability set and an AppArmor
profile, and the new mount API (fsopen, open_tree, mount_setattr) gets around the profile's mount
rules: guest root can write the host's /proc/sys/kernel/core_pattern and /proc/sysrq-trigger
(void/docs/apparmor-review.md, finding 1). With it the guest runs in a user namespace whose map
shifts every id except a few that stay identical, so guest root is the unprivileged kuid
SHIFT_BASE and the kernel's own permission checks refuse those writes.

The map keeps the ids that must mean the same thing on both sides: the users of the space (home
directories, XDG_RUNTIME_DIR sockets, PipeWire and the D-Bus proxy need no idmapped binds for
them) and the groups that own device nodes (audio, video, input, ...). Everything else is
SHIFT_BASE + id. The root filesystem and the host-root-owned directories that are bound into the
guest are idmapped mounts (``idmap=container``), so nothing is chowned.

LXC 6.0 run as root still goes through newuidmap, which only accepts ids that /etc/subuid and
/etc/subgid list for root: `spaces-void userns setup` adds the missing lines.

The choice is per space: the file /var/lib/spaces/NAME/userns holds ``on`` or ``off``; without it
the ``userns`` key of /etc/spaces/void.json decides (default off). `spaces create` writes the file
for every space it creates: ``on`` (it also seeds subuid and subgid), or ``off`` when the user passed
--no-userns or the host cannot do it (see choose_for_new_space). Spaces made before that have no
file and stay as they were, until `spaces-void userns enable NAME`.
"""

from __future__ import annotations

import glob
import grp
import json
import os
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

STATE_ROOT = Path("/var/lib/spaces")
EXTRAS_PATH = Path("/etc/spaces/void.json")
SUBUID = Path("/etc/subuid")
SUBGID = Path("/etc/subgid")
MOUNTINFO = Path("/proc/self/mountinfo")
MARKER = "userns"
# Root's range in /etc/subuid: the usual first allocation after the users' (65536 ids each).
SHIFT_BASE = 1_000_000
SHIFT_SIZE = 65536
# Host groups that own device nodes a guest is handed; they stay identical in the guest.
DEVICE_GROUPS = ("audio", "video", "input", "render", "kvm", "plugdev")
# Where those nodes are: sound, graphics, input, video capture and compute.
DEVICE_NODES = ("/dev/snd/*", "/dev/dri/*", "/dev/input/*", "/dev/video*", "/dev/kfd", "/dev/media*")
# Host-root-owned places that are bound into a guest and must look root-owned to guest root.
IDMAP_SOURCES = ("/var/lib/spaces/", "/var/cache/spaces/", "/run/spaces/")
# The shared NVIDIA userspace farm: read-only files that any user may read, nothing to idmap.
HOST_FARM = "/var/lib/spaces/.host/"
SYS = "/sys"
# Idmapped mounts need kernel 5.12 and a filesystem that supports them.
IDMAP_KERNEL = (5, 12)
IDMAP_FILESYSTEMS = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs", "f2fs", "overlay", "erofs", "squashfs"})


class UsernsError(ValueError):
    """The user namespace cannot be set up as configured."""


@dataclass(frozen=True)
class Plan:
    """The id map of one space: identical ids plus everything else shifted by base."""

    uids: tuple[int, ...]
    gids: tuple[int, ...]
    base: int = SHIFT_BASE
    size: int = SHIFT_SIZE

    @property
    def root_uid(self) -> int:
        """What guest root is on the host (the kuid broker sockets see as the peer)."""

        return self.base

    def _map(self, kind: str, identity: Sequence[int]) -> list[str]:
        lines: list[str] = []
        position = 0
        for ident in sorted(set(identity)):
            if ident >= self.size:
                break
            if ident > position:
                lines.append(f"lxc.idmap = {kind} {position} {self.base + position} {ident - position}")
            lines.append(f"lxc.idmap = {kind} {ident} {ident} 1")
            position = ident + 1
        if position < self.size:
            lines.append(f"lxc.idmap = {kind} {position} {self.base + position} {self.size - position}")
        lines.extend(f"lxc.idmap = {kind} {ident} {ident} 1" for ident in sorted(set(identity)) if ident >= self.size)
        return lines

    def idmap_lines(self) -> list[str]:
        return [*self._map("u", self.uids), *self._map("g", self.gids)]

    def required(self) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        """(uid ranges, gid ranges) that root needs in /etc/subuid and /etc/subgid."""

        def ranges(identity: Iterable[int]) -> list[tuple[int, int]]:
            return [(self.base, self.size), *((i, 1) for i in sorted(set(identity)))]

        return ranges(self.uids), ranges(self.gids)


# ----------------------------------------------------------------- the choice


def configured_default(extras: Path | None = None) -> bool:
    try:
        value = json.loads((extras or EXTRAS_PATH).read_text(encoding="utf-8")).get("userns", False)
    except (OSError, ValueError, AttributeError):
        return False
    return value is True


def enabled(name: str, root: Path | None = None, extras: Path | None = None) -> bool:
    """Whether the space runs in a user namespace (the per-space file wins)."""

    try:
        value = ((root or STATE_ROOT) / name / MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return configured_default(extras)
    if value in ("on", "off"):
        return value == "on"
    return configured_default(extras)


def set_enabled(name: str, value: bool | None, root: Path | None = None) -> bool:
    """Write the per-space choice (None removes it); True when something changed."""

    path = (root or STATE_ROOT) / name / MARKER
    if not path.parent.is_dir():
        raise UsernsError(f"no such space: {name}")
    try:
        before = path.read_text(encoding="utf-8").strip()
    except OSError:
        before = None
    if value is None:
        path.unlink(missing_ok=True)
        return before is not None
    text = "on" if value else "off"
    if before == text:
        return False
    path.write_text(text + "\n", encoding="utf-8")
    path.chmod(0o644)
    return True


# ------------------------------------------------------------- a new space


def kernel_version(release: str | None = None) -> tuple[int, int]:
    parts = (release or os.uname().release).split(".")
    try:
        return int(parts[0]), int("".join(c for c in parts[1] if c.isdigit()) or 0)
    except (ValueError, IndexError):
        return 0, 0


def filesystem_of(path: Path, mountinfo: str) -> str | None:
    """The filesystem type of the mount that holds path (the longest mount point that contains it)."""

    best: tuple[int, str] | None = None
    for line in mountinfo.splitlines():
        head, separator, tail = line.partition(" - ")
        fields = head.split()
        if not separator or len(fields) < 5 or not tail:
            continue
        point = Path(_unescape(fields[4]))
        if point == path or point in path.parents:
            kind = tail.split()[0]
            if best is None or len(point.parts) >= best[0]:
                best = (len(point.parts), kind)
    return best[1] if best else None


def unsupported_reason(
    root: Path | None = None,
    release: str | None = None,
    mountinfo: str | None = None,
) -> str | None:
    """Why this host cannot run a space in a user namespace, or None when it can."""

    version = kernel_version(release)
    if version < IDMAP_KERNEL:
        return f"the kernel is {version[0]}.{version[1]}, idmapped mounts need {IDMAP_KERNEL[0]}.{IDMAP_KERNEL[1]}"
    if mountinfo is None:
        try:
            mountinfo = MOUNTINFO.read_text(encoding="utf-8")
        except OSError:
            mountinfo = ""
    kind = filesystem_of((root or STATE_ROOT).absolute(), mountinfo)
    if kind is not None and kind not in IDMAP_FILESYSTEMS:
        return f"{root or STATE_ROOT} is on {kind}, which has no idmapped mounts"
    return None


def choose_for_new_space(
    name: str,
    want: bool = True,
    root: Path | None = None,
    subuid: Path | None = None,
    subgid: Path | None = None,
    release: str | None = None,
    mountinfo: str | None = None,
) -> tuple[bool, str | None]:
    """Write the marker of a space that was just created; return (enabled, why not).

    On: the host can do it (unsupported_reason) and subuid/subgid cover the map (they are added here;
    create runs as root). Off with a reason otherwise; this never fails the creation. want=False
    (--no-userns) writes off without a reason. The marker is always written, so a space that is
    created again gets a fresh choice.
    """

    reason = None if not want else unsupported_reason(root, release, mountinfo)
    if want and reason is None:
        try:
            setup_subids(plan_for(name, root=root), subuid, subgid)
        except (UsernsError, OSError) as error:
            reason = f"subuid and subgid could not be set up: {error}"
    try:
        set_enabled(name, want and reason is None, root)
    except (UsernsError, OSError) as error:
        return False, reason or f"the choice could not be saved: {error}"
    return want and reason is None, reason


# --------------------------------------------------------------------- the plan


def _space_users(name: str, root: Path) -> list[tuple[int, int]]:
    try:
        info = json.loads((root / name / "info.json").read_text(encoding="utf-8"))
        users = info["permissions"]["users"]
        return [(int(uid), int(value["gid"])) for uid, value in users.items()]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise UsernsError(f"cannot read the users of space {name}: {error}") from error


def _group_ids(names: Iterable[str]) -> list[int]:
    ids = []
    for name in names:
        try:
            ids.append(grp.getgrnam(name).gr_gid)
        except KeyError:
            continue
    return ids


def node_gids(patterns: Sequence[str] = DEVICE_NODES) -> list[int]:
    """Group owners (other than root) of the device nodes a space is typically handed."""

    found: set[int] = set()
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                gid = os.stat(path).st_gid
            except OSError:
                continue
            if gid:
                found.add(gid)
    return sorted(found)


def plan_for(
    name: str,
    device_sources: Iterable[str] = (),
    root: Path | None = None,
    group_ids: Sequence[int] | None = None,
) -> Plan:
    """The map of a space: its users' ids, the device groups, and the groups of the nodes it is bound.

    Only those groups stay identical: a group like disk or tty would hand guest root that host group
    on everything it can reach. A node with any other group is still usable through its ACL.
    """

    users = _space_users(name, root or STATE_ROOT)
    uids = {uid for uid, _gid in users}
    gids = {gid for _uid, gid in users}
    gids.update([*_group_ids(DEVICE_GROUPS), *node_gids()] if group_ids is None else group_ids)
    for source in device_sources:
        try:
            owner = os.stat(source).st_gid
        except OSError:
            continue
        if owner:
            gids.add(owner)
    uids.discard(0)
    gids.discard(0)
    return Plan(tuple(sorted(uids)), tuple(sorted(gids)))


# ------------------------------------------------------------ subuid / subgid


def parse_subids(text: str, owner: str = "root", owner_id: int = 0) -> list[tuple[int, int]]:
    """The (start, count) ranges that /etc/subuid or /etc/subgid lists for root."""

    ranges: list[tuple[int, int]] = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 3 or line.lstrip().startswith("#"):
            continue
        if parts[0] not in (owner, str(owner_id)):
            continue
        try:
            ranges.append((int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return ranges


def covered(ranges: Sequence[tuple[int, int]], start: int, count: int) -> bool:
    return any(a <= start and start + count <= a + n for a, n in ranges)


def missing(need: Sequence[tuple[int, int]], have: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(start, count) for start, count in need if not covered(have, start, count)]


def subid_status(plan: Plan, subuid: Path | None = None, subgid: Path | None = None) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(missing uid ranges, missing gid ranges) of root's /etc/subuid and /etc/subgid."""

    subuid, subgid = subuid or SUBUID, subgid or SUBGID
    need_u, need_g = plan.required()

    def have(path: Path) -> list[tuple[int, int]]:
        try:
            return parse_subids(path.read_text(encoding="utf-8"))
        except OSError:
            return []

    return missing(need_u, have(subuid)), missing(need_g, have(subgid))


def add_subids(path: Path, ranges: Sequence[tuple[int, int]]) -> None:
    """Append `root:START:COUNT` lines (the file is root's to edit; mode and owner are kept)."""

    if not ranges:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if text and not text.endswith("\n"):
        text += "\n"
    text += "".join(f"root:{start}:{count}\n" for start, count in ranges)
    temporary = path.with_name(path.name + ".spaces-new")
    temporary.write_text(text, encoding="utf-8")
    try:
        status = path.stat()
        os.chown(temporary, status.st_uid, status.st_gid)
        temporary.chmod(stat.S_IMODE(status.st_mode))
    except FileNotFoundError:
        temporary.chmod(0o644)
    os.replace(temporary, path)


def setup_subids(plan: Plan, subuid: Path | None = None, subgid: Path | None = None) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Add what root's /etc/subuid and /etc/subgid lack for the plan; return what was added."""

    subuid, subgid = subuid or SUBUID, subgid or SUBGID
    need_u, need_g = subid_status(plan, subuid, subgid)
    add_subids(subuid, need_u)
    add_subids(subgid, need_g)
    return need_u, need_g


def fit(plan: Plan, have_uid: Sequence[tuple[int, int]], have_gid: Sequence[tuple[int, int]]) -> tuple[Plan, list[str]]:
    """The plan restricted to what root's subuid and subgid ranges allow, and what was dropped.

    The shifted range and the users' uids are required (UsernsError); a group that is not listed
    is only dropped: its files then show a nobody group, access through users and ACLs is unchanged.
    """

    if not covered(have_uid, plan.base, plan.size) or not covered(have_gid, plan.base, plan.size):
        raise UsernsError(f"root has no subuid/subgid range {plan.base}:{plan.size}")
    lost = [uid for uid in plan.uids if not covered(have_uid, uid, 1)]
    if lost:
        raise UsernsError(f"root may not map uid {', '.join(map(str, lost))}")
    dropped = [gid for gid in plan.gids if not covered(have_gid, gid, 1)]
    kept = Plan(plan.uids, tuple(g for g in plan.gids if g not in dropped), plan.base, plan.size)
    return kept, [f"gid {gid} is not in root's subgid ranges and stays shifted" for gid in dropped]


def describe(ranges: Sequence[tuple[int, int]], path: Path) -> str:
    return ", ".join(f"{path}: root:{start}:{count}" for start, count in ranges)


# ------------------------------------------------------------------------- sys


def _unescape(field: str) -> str:
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def mountinfo_entries(text: str) -> list[tuple[str, str]]:
    """(mount point, per-mount options) of every line of a mountinfo file."""

    entries = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 5:
            entries.append((_unescape(fields[4]), fields[5]))
    return entries


# Options of a /sys mount that a child user namespace cannot change (they are locked), so the bind
# has to repeat them: a missing atime mode alone makes the bind fail with EINVAL.
_LOCKED_FLAGS = ("nosuid", "nodev", "noexec", "noatime", "nodiratime", "relatime")


def sys_bind_options(entries: Sequence[tuple[str, str]]) -> str:
    """The mount options for the bind of /sys: read-only plus the host's locked flags."""

    options = next((o for point, o in reversed(entries) if point == SYS), "")
    kept = [flag for flag in options.split(",") if flag in _LOCKED_FLAGS]
    return ",".join(["rbind", "ro", *kept])


def sys_submounts(entries: Sequence[tuple[str, str]]) -> list[str]:
    """Mount points below /sys, topmost only (a parent that is hidden hides what is below it)."""

    points = [point for point, _o in entries if point.startswith(SYS + "/")]
    top = [p for p in dict.fromkeys(points) if not any(p.startswith(q + "/") for q in points)]
    return sorted(top, key=lambda item: (item.count("/"), item))


def read_mountinfo(path: Path = MOUNTINFO) -> list[tuple[str, str]]:
    try:
        return mountinfo_entries(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def needs_idmap(source: str) -> bool:
    """A bind of a host-root-owned Spaces directory or file is idmapped, so guest root owns it."""

    if not source.startswith(IDMAP_SOURCES) or source.startswith(HOST_FARM):
        return False
    try:
        status = os.stat(source)
    except OSError:
        return False
    return status.st_uid == 0 and not (stat.S_ISCHR(status.st_mode) or stat.S_ISBLK(status.st_mode))


def allow_traversal(sources: Iterable[str], below: str = "/run/spaces") -> list[str]:
    """Let guest root walk to the sources it is bound: o+x on the 0700 directories above them.

    The binds are set up as root of the user namespace (the idmapped ones by a helper that is not
    host root either), which is not root for the host's 0700 runtime directories. Only the
    directories *above* a source change (search permission, no listing); the source keeps its mode.
    """

    changed: list[str] = []
    prefix = below.rstrip("/") + "/"
    for source in sources:
        if not source.startswith(prefix):
            continue
        parts = Path(source).relative_to(below).parts[:-1]
        directory = Path(below)
        for part in parts:
            directory = directory / part
            try:
                status = os.lstat(directory)
            except OSError:
                break
            if not stat.S_ISDIR(status.st_mode) or status.st_uid != 0:
                break
            if not status.st_mode & 0o001:
                os.chmod(directory, stat.S_IMODE(status.st_mode) | 0o001)
                changed.append(str(directory))
    return changed
