#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
#
# S-08s rebind (CL-5). Attaches a fresh host socket over the stale bind mount of a running
# container, with no root and no container restart. Run it from one of the user's own
# namespace views: the plain session, or `podman unshare`.
#
#   python3 -I s04s_s08s_rebind.py <container init pid> <host path> <container path> [--unmount-first]
#
# Steps, each printed with its errno on failure:
#   1. open_tree(OPEN_TREE_CLONE) of the host path, in the caller's mount namespace (the host
#      path is visible only there);
#   2. setns into the container's user namespace, skipped when it is already the caller's;
#   3. setns into the container's mount namespace;
#   3b. with --unmount-first: umount2 of the container path (removes the stale bind mount);
#   4. move_mount of the detached tree onto the container path.
# Exit 0 only when move_mount succeeds.
#
# Without --unmount-first, move_mount stacks on top of the stale mount and fails with ENOENT
# when that mount's socket was unlinked by the logout (observed; see results/S-08.md).
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
libc.syscall.restype = ctypes.c_long
libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
libc.umount2.restype = ctypes.c_int


def step(name, ok, detail=""):
    print(f"{name}: {'ok' if ok else 'FAILED'} {detail}".rstrip(), flush=True)


def errno_text():
    e = ctypes.get_errno()
    return f"errno={e} ({os.strerror(e)})"


def cap_eff():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("CapEff:"):
                return line.split()[1]
    return "?"


def main():
    args = sys.argv[1:]
    unmount_first = "--unmount-first" in args
    args = [a for a in args if a != "--unmount-first"]
    if len(args) != 3:
        print("usage: s04s_s08s_rebind.py <container init pid> <host path> <container path> [--unmount-first]")
        return 2
    pid, src, dst = args
    print(f"caller: uid={os.getuid()} euid={os.geteuid()} CapEff={cap_eff()}", flush=True)
    print(f"caller user ns: {os.readlink('/proc/self/ns/user')}", flush=True)
    print(f"caller mnt ns:  {os.readlink('/proc/self/ns/mnt')}", flush=True)
    print(f"target user ns: {os.readlink(f'/proc/{pid}/ns/user')}", flush=True)
    print(f"target mnt ns:  {os.readlink(f'/proc/{pid}/ns/mnt')}", flush=True)

    fd_src = libc.syscall(SYS_OPEN_TREE, AT_FDCWD, src.encode(),
                          OPEN_TREE_CLONE | os.O_CLOEXEC)
    if fd_src < 0:
        step("1 open_tree", False, errno_text())
        return 1
    step("1 open_tree", True, f"fd={fd_src} src={src}")

    try:
        fd_user = os.open(f"/proc/{pid}/ns/user", os.O_RDONLY)
        fd_mnt = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
    except OSError as exc:
        step("open target ns files", False, f"errno={exc.errno} ({exc.strerror})")
        return 1

    if os.readlink("/proc/self/ns/user") == os.readlink(f"/proc/{pid}/ns/user"):
        step("2 setns user", True, "skipped: target user ns is already ours")
    else:
        try:
            os.setns(fd_user, CLONE_NEWUSER)
            step("2 setns user", True, f"CapEff={cap_eff()}")
        except OSError as exc:
            step("2 setns user", False, f"errno={exc.errno} ({exc.strerror})")
            return 1
    # Read CapEff before the mount namespace changes: after setns(mnt), /proc/self is the
    # container's procfs and does not contain this process.
    caps = cap_eff()
    try:
        os.setns(fd_mnt, CLONE_NEWNS)
        step("3 setns mnt", True, f"caller CapEff={caps}")
    except OSError as exc:
        step("3 setns mnt", False, f"errno={exc.errno} ({exc.strerror})")
        return 1

    if unmount_first:
        rc = libc.umount2(dst.encode(), 0)
        if rc != 0:
            step("3b umount2", False, errno_text())
            return 1
        step("3b umount2", True, f"removed the mount on {dst}")

    rc = libc.syscall(SYS_MOVE_MOUNT, fd_src, b"", AT_FDCWD, dst.encode(),
                      MOVE_MOUNT_F_EMPTY_PATH)
    if rc < 0:
        step("4 move_mount", False, errno_text())
        return 1
    step("4 move_mount", True, f"onto {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
