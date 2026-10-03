"""Bind a host path into, or unmount a path from, a running guest.

Run as ``python3 -m spaces.host.mountns bind|unbind ...`` in a subprocess:
setns(CLONE_NEWNS) needs a single-threaded process, which the launcher is not.
The source is cloned as a detached mount tree on the host side first, so a
/proc/PID/fd/N source keeps working after the namespace switch.
"""

from __future__ import annotations

import argparse
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


def bind(pid: int, source: str, destination: str, read_only: bool, mkdir: bool) -> None:
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
            os.makedirs(destination, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            if not os.path.exists(destination):
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


def unbind(pid: int, destination: str) -> None:
    _enter(pid)
    _check(_libc.umount2(os.fsencode(destination), MNT_DETACH))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="spaces.host.mountns")
    commands = parser.add_subparsers(dest="command", required=True)
    bind_parser = commands.add_parser("bind")
    bind_parser.add_argument("pid", type=int)
    bind_parser.add_argument("source")
    bind_parser.add_argument("destination")
    bind_parser.add_argument("--read-only", action="store_true")
    bind_parser.add_argument("--no-mkdir", action="store_true")
    unbind_parser = commands.add_parser("unbind")
    unbind_parser.add_argument("pid", type=int)
    unbind_parser.add_argument("destination")
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
            )
        else:
            unbind(arguments.pid, arguments.destination)
    except OSError as error:
        print(f"{arguments.command}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
