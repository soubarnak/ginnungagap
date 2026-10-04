"""Bind a host path into, or unmount a path from, a running guest.

Run as ``python3 -m spaces.host.mountns bind|unbind ...`` in a subprocess:
setns(CLONE_NEWNS) needs a single-threaded process, which the launcher is not.
The source is cloned as a detached mount tree on the host side first, so a
/proc/PID/fd/N source keeps working after the namespace switch.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import stat
import sys

SYS_OPEN_TREE = 428
SYS_MOVE_MOUNT = 429
SYS_MOUNT_SETATTR = 442
OPEN_TREE_CLONE = 1
OPEN_TREE_CLOEXEC = os.O_CLOEXEC
AT_FDCWD = -100
AT_EMPTY_PATH = 0x1000
AT_RECURSIVE = 0x8000
MOVE_MOUNT_F_EMPTY_PATH = 4
MOUNT_ATTR_RDONLY = 1
CLONE_NEWNS = 0x00020000
MNT_DETACH = 2

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


class _MountAttr(ctypes.Structure):
    _fields_ = [
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    ]


def _check(result: int) -> int:
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _enter(pid: int) -> None:
    descriptor = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY | os.O_CLOEXEC)
    try:
        _check(_libc.setns(descriptor, CLONE_NEWNS))
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _as_owner_of(directory: str, enabled: bool):
    """Create or remove entries in `directory` with the file system ids of its owner.

    A guest with a user namespace has file systems (its tmpfs mounts, the idmapped rootfs and
    binds) that cannot map the host's root: creating or removing an entry there as host root fails
    with EOVERFLOW. Taking the owner's ids works for every directory the guest has (guest root's
    tmpfs, the user's home); only the file system ids change, the capabilities for the mount calls
    stay and the ids go back to root afterwards.
    """

    if not enabled:
        yield
        return
    status = os.stat(directory)
    _libc.setfsuid(status.st_uid)
    _libc.setfsgid(status.st_gid)
    try:
        yield
    finally:
        _libc.setfsuid(0)
        _libc.setfsgid(0)


def _makedirs(path: str, owner: bool) -> None:
    missing = []
    while path and not os.path.isdir(path):
        missing.append(path)
        path = os.path.dirname(path)
    for directory in reversed(missing):
        with _as_owner_of(os.path.dirname(directory) or "/", owner):
            os.mkdir(directory)


def bind(
    pid: int,
    source: str,
    destination: str,
    read_only: bool,
    mkdir: bool,
    owner: bool = False,
) -> None:
    mode = os.stat(source).st_mode
    tree = _check(
        _libc.syscall(
            SYS_OPEN_TREE,
            AT_FDCWD,
            os.fsencode(source),
            OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC | AT_RECURSIVE,
        )
    )
    if read_only:
        attribute = _MountAttr(MOUNT_ATTR_RDONLY, 0, 0, 0)
        _check(
            _libc.syscall(
                SYS_MOUNT_SETATTR,
                tree,
                b"",
                AT_EMPTY_PATH | AT_RECURSIVE,
                ctypes.byref(attribute),
                ctypes.sizeof(attribute),
            )
        )
    _enter(pid)
    if mkdir:
        if stat.S_ISDIR(mode):
            _makedirs(destination, owner)
        else:
            _makedirs(os.path.dirname(destination), owner)
            if not os.path.exists(destination):
                with _as_owner_of(os.path.dirname(destination), owner):
                    os.close(os.open(destination, os.O_CREAT | os.O_WRONLY, 0o644))
    _check(
        _libc.syscall(
            SYS_MOVE_MOUNT,
            tree,
            b"",
            AT_FDCWD,
            os.fsencode(destination),
            MOVE_MOUNT_F_EMPTY_PATH,
        )
    )


def _prune_device_placeholder(destination: str, root: str = "", owner: bool = False) -> None:
    """Remove what bind() created for a device node once it is unmounted.

    The empty file left in the guest's /dev (and directories that became
    empty, e.g. /dev/input) would otherwise outlive the hot-unplugged device.
    """

    parts = destination.split("/")
    if len(parts) < 3 or parts[:2] != ["", "dev"]:
        return
    try:
        status = os.lstat(root + destination)
        if stat.S_ISREG(status.st_mode) and status.st_size == 0:
            with _as_owner_of(os.path.dirname(root + destination), owner):
                os.unlink(root + destination)
    except OSError:
        return
    while len(parts) > 3:
        parts.pop()
        try:
            with _as_owner_of(os.path.dirname(root + "/".join(parts)), owner):
                os.rmdir(root + "/".join(parts))
        except OSError:
            break


def unbind(pid: int, destination: str, owner: bool = False) -> None:
    _enter(pid)
    _check(_libc.umount2(os.fsencode(destination), MNT_DETACH))
    _prune_device_placeholder(destination, owner=owner)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="spaces.host.mountns")
    commands = parser.add_subparsers(dest="command", required=True)
    bind_parser = commands.add_parser("bind")
    bind_parser.add_argument("pid", type=int)
    bind_parser.add_argument("source")
    bind_parser.add_argument("destination")
    bind_parser.add_argument("--read-only", action="store_true")
    bind_parser.add_argument("--no-mkdir", action="store_true")
    bind_parser.add_argument("--as-owner", action="store_true", help="guest with a user namespace")
    unbind_parser = commands.add_parser("unbind")
    unbind_parser.add_argument("pid", type=int)
    unbind_parser.add_argument("destination")
    unbind_parser.add_argument("--as-owner", action="store_true", help="guest with a user namespace")
    arguments = parser.parse_args(argv)
    if not arguments.destination.startswith("/"):
        print("destination must be absolute", file=sys.stderr)
        return 2
    try:
        if arguments.command == "bind":
            bind(
                arguments.pid,
                arguments.source,
                arguments.destination,
                arguments.read_only,
                not arguments.no_mkdir,
                arguments.as_owner,
            )
        else:
            unbind(arguments.pid, arguments.destination, arguments.as_owner)
    except OSError as error:
        print(f"{arguments.command}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
