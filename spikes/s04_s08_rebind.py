#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
#
# S-08 rebind (CL-5): mount a fresh host socket over the stale bind mount inside a running
# container, without restarting the container and without root.
#
#   python3 -I - <container init pid> <host socket path> <container path> < s04_s08_rebind.py
#
# Steps: open_tree(OPEN_TREE_CLONE) in the caller's mount view (the host path is visible
# here, not inside the container), setns into the container's user and mount namespaces
# (owner match: same euid as the namespace owner), then move_mount the detached tree onto
# the container path. Exit 0 only when move_mount succeeds.
import ctypes
import os
import sys

SYS_OPEN_TREE = 428   # x86_64
SYS_MOVE_MOUNT = 429  # x86_64
AT_FDCWD = -100
OPEN_TREE_CLONE = 0x1
MOVE_MOUNT_F_EMPTY_PATH = 0x4
CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000

libc = ctypes.CDLL(None, use_errno=True)


def report(step, ok, detail=""):
    print(f"{step}: {'ok' if ok else 'FAILED'} {detail}".rstrip(), flush=True)


def errno_detail():
    e = ctypes.get_errno()
    return f"errno={e} ({os.strerror(e)})"


def main():
    pid, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
    print(f"uid={os.getuid()} euid={os.geteuid()} target_pid={pid}", flush=True)

    fd_src = libc.syscall(SYS_OPEN_TREE, AT_FDCWD, src.encode(),
                          OPEN_TREE_CLONE | os.O_CLOEXEC)
    if fd_src < 0:
        report("open_tree", False, errno_detail())
        return 1
    report("open_tree", True, f"fd={fd_src} src={src}")

    try:
        fd_user = os.open(f"/proc/{pid}/ns/user", os.O_RDONLY)
        fd_mnt = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
    except OSError as exc:
        report("open ns files", False, f"errno={exc.errno} ({exc.strerror})")
        return 1

    try:
        os.setns(fd_user, CLONE_NEWUSER)
        report("setns user", True)
    except OSError as exc:
        report("setns user", False, f"errno={exc.errno} ({exc.strerror})")
        return 1
    try:
        os.setns(fd_mnt, CLONE_NEWNS)
        report("setns mnt", True)
    except OSError as exc:
        report("setns mnt", False, f"errno={exc.errno} ({exc.strerror})")
        return 1

    rc = libc.syscall(SYS_MOVE_MOUNT, fd_src, b"", AT_FDCWD, dst.encode(),
                      MOVE_MOUNT_F_EMPTY_PATH)
    if rc < 0:
        report("move_mount", False, errno_detail())
        return 1
    report("move_mount", True, f"onto {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
