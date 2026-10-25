#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""T4.9 drive probe, container form (ADR-008, section 4.2: the mechanism probe on a realm).

A keep-id realm has its own user namespace, a child of the store's, and its own mount namespace.
The product's case is a MOUNT ROOT granted to the realm (`/run/media/<user>`) with a drive mounted
under it after the realm started. This probe checks, in a store of its own (temp base under /tmp,
--root, --runroot and --tmpdir inside it, as in tests/integration/conftest.py):

1. control: the realm does not see the drive, which was mounted after the realm started;
2. route D, from a host process (not under `podman unshare`): setns into the realm's user namespace,
   unshare(CLONE_NEWNS) copies the CURRENT host mount table, open_tree(OPEN_TREE_CLONE |
   AT_RECURSIVE) clones the mount root there (with the drive below it), setns into the realm's
   mount namespace, and move_mount(2) attaches the clone at a realm placeholder;
3. the realm reads the drive as uid 1000 (the realm user) through `podman exec`.

Run from the repository root: python3 spikes/t49_drive_probe_container.py
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import t49_drive_probe as probe  # noqa: E402 - the shared syscall wrappers and store helpers

IMAGE = (
    "docker.io/library/debian@sha256:d405c1e1d9eecbf30022d23766a0eea235e7f499aa33e357dfd29271a839f82a"
)
REALM_DST = "/run/media/user/route-d"
UID = os.getuid()
GID = os.getgid()


def route_d_into_realm(base: Path, init_pid: int, root: Path, dst: str) -> None:
    """Route D with the realm's user and mount namespaces. Runs in a forked child, then exits."""
    report: dict[str, Any] = {}
    # The report file is opened before the namespaces change: the host's /tmp is not in the realm.
    out = os.open(base / "d.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    user_fd = os.open(f"/proc/{init_pid}/ns/user", os.O_RDONLY)
    mnt_fd = os.open(f"/proc/{init_pid}/ns/mnt", os.O_RDONLY)
    report["setns_realm_user"] = probe._res(probe.libc.setns(user_fd, probe.CLONE_NEWUSER))
    report["unshare_newns"] = probe._res(probe.libc.unshare(probe.CLONE_NEWNS))
    fd = probe.open_tree_clone(str(root))
    report["open_tree_clone_mount_root"] = probe._res(fd)
    report["setns_realm_mnt"] = probe._res(probe.libc.setns(mnt_fd, probe.CLONE_NEWNS))
    if fd >= 0 and report["setns_realm_mnt"]["ok"]:
        os.mkdir(dst)  # the placeholder, made in the realm (the helper makes it too)
        report["move_mount"] = probe._res(probe.move_mount_into(fd, dst))
    os.write(out, json.dumps(report).encode())
    os._exit(0)


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="ginn-it-probe-c-", dir="/tmp"))
    base.chmod(0o700)
    cid = "probe-c"
    root = base / "media" / "user"
    drive = root / "drive"
    result: dict[str, Any] = {}

    def podman(*args: str) -> Any:
        return probe.podman(base, *args)

    try:
        for name in ("x", "g", "r", "t", "home", "data", "config", "state", "src"):
            (base / name).mkdir(parents=True, exist_ok=True)
        (base / "x").chmod(0o700)
        root.mkdir(parents=True)
        drive.mkdir()
        (base / "src" / "drive-marker.txt").write_text("drive\n")
        result["root_propagation"] = probe.run(["findmnt", "-o", "TARGET,PROPAGATION", "/"]).stdout

        pulled = podman("pull", IMAGE)
        result["pull_rc"] = pulled.returncode
        if pulled.returncode != 0:
            result["pull_stderr"] = pulled.stderr.strip()[-300:]
            return 1
        made = podman("create", "--name", cid, "--userns", "keep-id", "--user", f"{UID}:{GID}",
                      "--network", "none", IMAGE, "sleep", "infinity")
        result["create_rc"] = made.returncode
        result["start_rc"] = podman("start", cid).returncode
        init_pid = int(podman("inspect", "--format", "{{.State.Pid}}", cid).stdout.strip())
        result["realm_userns_is_child_of_store"] = (
            os.readlink(f"/proc/{init_pid}/ns/user") != os.readlink("/proc/self/ns/user")
        )
        podman("exec", "--user", "0:0", cid, "mkdir", "-p", "/run/media/user")

        # The drive is mounted AFTER the realm started, under the granted mount root.
        mounted = probe.drive_mount(drive, base / "src")
        result["drive_mount_rc"] = mounted.returncode
        result["host_mountinfo_has_drive"] = str(drive) in Path("/proc/self/mountinfo").read_text()
        result["control_realm_sees_drive_before"] = podman(
            "exec", "--user", f"{UID}:{GID}", cid, "/bin/sh", "-c",
            "test -e /run/media/user/drive/drive-marker.txt && echo SEEN || echo NOT_SEEN",
        ).stdout.strip()

        pid = os.fork()
        if pid == 0:
            route_d_into_realm(base, init_pid, root, REALM_DST)
        os.waitpid(pid, 0)
        result["route_d"] = json.loads((base / "d.json").read_text())

        result["realm_reads_drive_as_1000"] = podman(
            "exec", "--user", f"{UID}:{GID}", cid, "/bin/sh", "-c",
            "cat /run/media/user/route-d/drive/drive-marker.txt 2>&1; echo rc=$?",
        ).stdout.strip()
        return 0
    finally:
        with contextlib.suppress(Exception):
            probe.run(["fusermount3", "-u", str(drive)])
        with contextlib.suppress(Exception):
            podman("rm", "-f", cid)
        probe.stop_store_processes(base)
        time.sleep(1)
        shutil.rmtree(base, ignore_errors=True)
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
