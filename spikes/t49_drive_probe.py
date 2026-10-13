#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""T4.9 drive probe (ADR-008, section 4.2 and Resolution A): can a host mount made after the store's
pause process exists reach that store's mount namespace?

The store's mount namespace is a copy of the host mount table taken when the pause process started.
Host mounts are private, so a later host mount is absent from it, and `podman unshare` (which joins
that namespace) does not see the drive. The probe measures, with the errno of every call, these
routes, in a store of its own (temp base under /tmp, the isolation of tests/integration/conftest.py):

Route A: join the store's user namespace, then unshare(CLONE_NEWNS).
Route B: a child unshares CLONE_NEWUSER|CLONE_NEWNS from the host namespaces (a copy of the current
  host table), clones the drive there, and sends the clone to the parent over a socket. The parent
  joins the store's user and mount namespaces and move_mount(2)s the clone in.
Route D: one host process, the same steps as route A with the clone made in between.
Under podman unshare: unshare(CLONE_NEWNS) under `podman unshare`, which copies the store's table.

Run it from the repository root: python3 spikes/t49_drive_probe.py
It prints one JSON object. The realm form, which is the product's case, is in
spikes/t49_drive_probe_container.py.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import shutil
import signal
import socket
import subprocess  # noqa: S404 - fixed argv lists, never a shell
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
AT_FDCWD = -100
AT_RECURSIVE = 0x8000
OPEN_TREE_CLONE = 0x1
MOVE_MOUNT_F_EMPTY_PATH = 0x4
O_CLOEXEC = os.O_CLOEXEC
SYS_OPEN_TREE = 428
SYS_MOVE_MOUNT = 429
PAUSE_COMM = "catatonit"

libc = ctypes.CDLL(None, use_errno=True)
libc.unshare.argtypes = [ctypes.c_int]
libc.setns.argtypes = [ctypes.c_int, ctypes.c_int]


def _name(code: int) -> str:
    return errno.errorcode.get(code, str(code))


def _res(rc: int) -> dict[str, Any]:
    """The return value of one call, with its errno name when it failed."""
    if rc >= 0:
        return {"ok": True, "rc": rc}
    code = ctypes.get_errno()
    return {"ok": False, "errno": code, "name": _name(code), "text": os.strerror(code)}


def run(argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, no shell
        argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
        timeout=120, env=env,
    )


def store_env(base: Path) -> dict[str, str]:
    xdg = base / "x"
    home = base / "home"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(xdg),
        "XDG_DATA_HOME": str(base / "data"),
        "XDG_CONFIG_HOME": str(base / "config"),
        "XDG_STATE_HOME": str(base / "state"),
    }
    return env


def podman(base: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Every podman call passes its own storage flags (AGENTS.md, ADR-004)."""
    argv = [
        shutil.which("podman") or "podman",
        "--root", str(base / "g"), "--runroot", str(base / "r"), "--tmpdir", str(base / "t"),
        "--runtime", "crun", *args,
    ]
    return run(argv, store_env(base))


def pause_pid(base: Path) -> int | None:
    try:
        pid = int((base / "x" / "libpod" / "tmp" / "pause.pid").read_text().strip())
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except (OSError, ValueError):
        return None
    return pid if comm == PAUSE_COMM else None


def drive_mount(drive: Path, src: Path) -> subprocess.CompletedProcess[str]:
    return run(["fuse-overlayfs", "-o", f"lowerdir={src}", str(drive)])


def stop_store_processes(base: Path) -> list[int]:
    """SIGTERM every catatonit or conmon whose XDG_RUNTIME_DIR is this probe's own runtime dir.

    The name is checked and the runtime dir is compared exactly, so no other process is signalled.
    Returns the pids that were signalled.
    """
    wanted = f"XDG_RUNTIME_DIR={base / 'x'}".encode()
    signalled: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            env = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if comm in ("catatonit", "conmon") and wanted in env:
            with contextlib.suppress(OSError):
                os.kill(int(entry.name), signal.SIGTERM)
                signalled.append(int(entry.name))
    return signalled


def open_tree_clone(path: str) -> int:
    buf = os.fsencode(path)
    return libc.syscall(SYS_OPEN_TREE, AT_FDCWD, buf, OPEN_TREE_CLONE | AT_RECURSIVE | O_CLOEXEC)


def move_mount_into(fd: int, dst: str) -> int:
    return libc.syscall(
        SYS_MOVE_MOUNT, fd, b"", AT_FDCWD, os.fsencode(dst), MOVE_MOUNT_F_EMPTY_PATH
    )


def route_a(pause: int) -> dict[str, Any]:
    """Join the store's user namespace only, then unshare(CLONE_NEWNS). Runs in a forked child."""
    report: dict[str, Any] = {}
    fd = os.open(f"/proc/{pause}/ns/user", os.O_RDONLY)
    report["setns_user"] = _res(libc.setns(fd, CLONE_NEWUSER))
    report["unshare_newns"] = _res(libc.unshare(CLONE_NEWNS))
    return report


def route_d(base: Path, drive: Path, pause: int) -> dict[str, Any]:
    """Route D, one process: join the store's user namespace, unshare(CLONE_NEWNS) for a copy of
    the mount table of the process's CURRENT mount namespace (the host's, when started there),
    open_tree(OPEN_TREE_CLONE) the drive in that copy, join the store's mount namespace and
    move_mount the clone there. Runs in a forked child."""
    report: dict[str, Any] = {}
    user_fd = os.open(f"/proc/{pause}/ns/user", os.O_RDONLY)
    mnt_fd = os.open(f"/proc/{pause}/ns/mnt", os.O_RDONLY)
    report["setns_user"] = _res(libc.setns(user_fd, CLONE_NEWUSER))
    report["unshare_newns"] = _res(libc.unshare(CLONE_NEWNS))
    fd = open_tree_clone(str(drive))
    report["open_tree_clone_drive"] = _res(fd)
    report["setns_mnt"] = _res(libc.setns(mnt_fd, CLONE_NEWNS))
    dst = base / "media" / "user" / "route-d"
    if fd >= 0 and report["setns_mnt"]["ok"]:
        with contextlib.suppress(OSError):
            dst.mkdir()
        report["move_mount"] = _res(move_mount_into(fd, str(dst)))
        report["store_ns_marker_after_move"] = (dst / "drive-marker.txt").exists()
    Path(base / "d.json").write_text(json.dumps(report))
    os._exit(0)


def route_b_child(sock: socket.socket, drive: str) -> dict[str, Any]:
    """A fresh copy of the current host mount table, then the drive cloned from it. Child only."""
    report: dict[str, Any] = {}
    report["unshare_newuser_newns"] = _res(libc.unshare(CLONE_NEWUSER | CLONE_NEWNS))
    fd = open_tree_clone(drive)
    report["open_tree_clone_drive"] = _res(fd)
    if fd >= 0:
        socket.send_fds(sock, [b"fd"], [fd])
        os.close(fd)
    sock.close()
    return report


def route_b(base: Path, drive: Path, pause: int) -> dict[str, Any]:
    """Parent side of route B: receive the clone, join the store's namespaces, move it in."""
    report: dict[str, Any] = {}
    parent_sock, child_sock = socket.socketpair()
    child_pid = os.fork()
    if child_pid == 0:  # child
        parent_sock.close()
        code = 0
        try:
            child_report = route_b_child(child_sock, str(drive))
            Path(base / "child.json").write_text(json.dumps(child_report))
        except BaseException:
            code = 1
        os._exit(code)
    child_sock.close()
    _, fds, _, _ = socket.recv_fds(parent_sock, 16, 1)
    parent_sock.close()
    os.waitpid(child_pid, 0)
    report["child"] = json.loads((base / "child.json").read_text())
    if not fds:
        report["received_fd"] = False
        return report
    tree = fds[0]
    user_fd = os.open(f"/proc/{pause}/ns/user", os.O_RDONLY)
    report["parent_setns_user"] = _res(libc.setns(user_fd, CLONE_NEWUSER))
    mnt_fd = os.open(f"/proc/{pause}/ns/mnt", os.O_RDONLY)
    report["parent_setns_mnt"] = _res(libc.setns(mnt_fd, CLONE_NEWNS))
    dst = base / "media" / "user" / "probe-drive"
    if report["parent_setns_mnt"]["ok"]:
        with contextlib.suppress(OSError):
            dst.mkdir()
        report["move_mount_into_store_ns"] = _res(move_mount_into(tree, str(dst)))
        marker = dst / "drive-marker.txt"
        report["store_ns_sees_marker_after_move"] = marker.exists()
    os.close(tree)
    return report


def under_unshare(root: Path, dst: Path) -> None:
    """Started by `podman unshare`, so the process is already in the store's mount namespace.

    unshare(CLONE_NEWNS) then copies THAT namespace, which is the stale table. The clone of the
    mount root is attached at `dst` and the drive is looked for below it.
    """
    report: dict[str, Any] = {}
    report["unshare_newns"] = _res(libc.unshare(CLONE_NEWNS))
    fd = open_tree_clone(str(root))
    report["open_tree_clone_mount_root"] = _res(fd)
    if fd >= 0:
        report["move_mount"] = _res(move_mount_into(fd, str(dst)))
    report["drive_below_root_seen"] = (dst / "drive" / "drive-marker.txt").exists()
    print(json.dumps(report, sort_keys=True))


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="ginn-it-probe-", dir="/tmp"))
    base.chmod(0o700)
    result: dict[str, Any] = {"base": str(base), "root_propagation": None}
    result["root_propagation"] = run(["findmnt", "-o", "TARGET,PROPAGATION", "/"]).stdout.split()
    try:
        for name in ("x", "g", "r", "t", "home", "data", "config", "state", "src", "session"):
            (base / name).mkdir(parents=True, exist_ok=True)
        (base / "x").chmod(0o700)
        (base / "media" / "user").mkdir(parents=True)
        (base / "under-unshare").mkdir()
        drive = base / "media" / "user" / "drive"
        drive.mkdir()
        (base / "src" / "drive-marker.txt").write_text("drive\n")

        # 1. Start the store's pause process while the drive does not exist yet.
        podman(base, "unshare", "/bin/true")
        pause = pause_pid(base)
        result["pause_pid"] = pause
        if pause is None:
            result["error"] = "no pause process after podman unshare"
            return 1

        # 2. Host mounts after the pause exists: the drive, and a control mount in the store.
        mounted = drive_mount(drive, base / "src")
        result["drive_mount_rc"] = mounted.returncode
        result["drive_mount_stderr"] = mounted.stderr.strip()[:200]
        result["host_mountinfo_has_drive"] = str(drive) in Path("/proc/self/mountinfo").read_text()
        result["host_drive_in_store_view_before_route_b"] = podman(
            base, "unshare", "/bin/sh", "-c", "test -e $1 && echo SEEN || echo NOT_SEEN", "sh",
            str(drive / "drive-marker.txt"),
        ).stdout.strip()

        # 3. Route A, in a forked child so this process keeps its namespaces.
        pid = os.fork()
        if pid == 0:
            payload = route_a(pause)
            Path(base / "a.json").write_text(json.dumps(payload))
            os._exit(0)
        os.waitpid(pid, 0)
        result["route_a"] = json.loads((base / "a.json").read_text())

        # 3b. Route D, one process, in a forked child.
        pid = os.fork()
        if pid == 0:
            route_d(base, drive, pause)
        os.waitpid(pid, 0)
        result["route_d"] = json.loads((base / "d.json").read_text())

        # 3c. The same clone, started under `podman unshare` (the helper's current launch).
        under = podman(base, "unshare", sys.executable, str(Path(__file__).resolve()),
                       "--under-unshare", str(base / "media" / "user"),
                       str(base / "under-unshare"))
        result["under_podman_unshare"] = json.loads(under.stdout) if under.stdout else under.stderr

        # 4. Route B, the fresh host copy moved into the store's namespace.
        result["route_b"] = route_b(base, drive, pause)

        return 0
    finally:
        # Unmount the drive, stop the store's pause process by name, remove the base.
        with contextlib.suppress(Exception):
            run(["fusermount3", "-u", str(base / "media" / "user" / "drive")])
        stop_store_processes(base)
        time.sleep(1)
        shutil.rmtree(base, ignore_errors=True)
        print(json.dumps(result, indent=2, sort_keys=True), file=sys.stdout)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--under-unshare":
        under_unshare(Path(sys.argv[2]), Path(sys.argv[3]))
        sys.exit(0)
    sys.exit(main())
