#!/usr/bin/env python3
"""M10 spike: does a shifted user namespace work for a Spaces guest on LXC?

Gated, throw-away experiment on a *stopped* space's rootfs (default `ubuntu`) with a scratch
LXC configuration under /run/spaces-m10 (never under /run/spaces/lxc, so the real space state is
untouched). Run as the normal user with passwordless sudo, the package installed, all spaces stopped:

    python3 void/spike/m10_userns.py [--space ubuntu] [--keep]

It stops at the first blocker and prints the facts. Gates, in the order they can break:

  0  idmapped mounts on the filesystem of the rootfs, the id map and the subuid/subgid ranges
  1  start with the shifted map, `lxc.rootfs.options = idmap=container`, the wrapper's pinned
     monitor namespace and `lxc.namespace.share.net = /proc/1/ns/net` (a setns into the host's
     network namespace from inside a new user namespace may be refused)
  2  a fresh sysfs mount in a user namespace that shares the host's network namespace
  3  boot to `running` without a failed unit
  4  the mount-API probes of m9_check.py (fresh proc core_pattern and sysrq-trigger, a clone of
     /proc/sys, mount(2) bind remount): all must be refused
  5  ownership: host-root-owned things the guest needs (home/root, the package cache, sockets)

The scratch guest has no desktop integration (no broker, no home binds of the user's entries);
those are checked with the real launcher once the translator knows the option.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import m9_check as m9  # noqa: E402
from m8_check import check, run, skip, sudo  # noqa: E402

WRAPPER = "/usr/lib/spaces/spaces-lxc"
LXC_PATH = "/run/spaces-m10"
NAME = "m10"
CGROUP = Path("/sys/fs/cgroup/spaces") / NAME

# Host ids that stay identical in the guest (ns id == host id): the user, and the groups that own
# device nodes the guest is handed (audio 12, video 13, kvm 24, input 25 on this host).
SHIFT_BASE = 1_000_000
IDENTITY_UID = (1000,)
IDENTITY_GID = (12, 13, 24, 25, 1000)


def idmap_lines(identity_uid: tuple[int, ...], identity_gid: tuple[int, ...], base: int = SHIFT_BASE, size: int = 65536) -> list[str]:
    """`lxc.idmap` lines: ids 0..size-1 shifted by base, except those that map to themselves."""

    lines: list[str] = []
    for kind, identity in (("u", identity_uid), ("g", identity_gid)):
        position = 0
        for ident in sorted(set(identity)):
            if ident > position:
                lines.append(f"lxc.idmap = {kind} {position} {base + position} {ident - position}")
            lines.append(f"lxc.idmap = {kind} {ident} {ident} 1")
            position = ident + 1
        if position < size:
            lines.append(f"lxc.idmap = {kind} {position} {base + position} {size - position}")
    return lines


def sys_submounts() -> list[str]:
    """Mount points below /sys in the host's mount namespace, shallowest first."""

    points = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        target = line.split()[4].replace("\\040", " ")
        if target.startswith("/sys/"):
            points.append(target)
    # A mount below another one is covered when its parent is hidden.
    top = [p for p in points if not any(p.startswith(q + "/") for q in points)]
    return sorted(top, key=lambda item: (item.count("/"), item))


def config_text(space: str, *, sys_mode: str, share_net: bool = True) -> str:
    runtime = f"{LXC_PATH}/{NAME}"
    base = [
        "lxc.uts.name = m10",
        f"lxc.rootfs.path = dir:/var/lib/spaces/{space}/rootfs",
        "lxc.rootfs.options = idmap=container",
        *idmap_lines(IDENTITY_UID, IDENTITY_GID),
        "lxc.net.0.type = none",
        *(["lxc.namespace.share.net = /proc/1/ns/net"] if share_net else []),
        "lxc.autodev = 1",
        "lxc.tty.max = 0",
        "lxc.pty.max = 1024",
        "lxc.console.path = none",
        "lxc.init.cmd = /usr/lib/systemd/systemd",
        "lxc.signal.halt = SIGRTMIN+3",
        # sys_mode: "mixed" is the nspawn-like default; "bind" avoids a fresh sysfs
        f"lxc.mount.auto = proc:mixed {'sys:mixed ' if sys_mode == 'mixed' else ''}cgroup:rw:force",
        "lxc.apparmor.profile = lxc-spaces-container",
        "lxc.cgroup.relative = 1",
        "lxc.cgroup.dir.monitor = monitor",
        "lxc.cgroup.dir.monitor.pivot = pivot",
        "lxc.cgroup.dir.container = payload",
        "lxc.cgroup.dir.container.inner = guest",
        "lxc.cap.keep = chown dac_override dac_read_search fowner fsetid ipc_owner kill lease linux_immutable "
        "setgid setfcap setpcap setuid sys_admin sys_chroot sys_nice sys_resource sys_boot mknod",
        "lxc.seccomp.profile = /usr/share/lxc/config/common.seccomp",
        "lxc.environment = SYSTEMD_GETTY_AUTO=no",
        f"lxc.include = {runtime}/devices.conf",
        "lxc.mount.entry = tmpfs run tmpfs rw,nosuid,nodev,mode=755 0 0",
        "lxc.mount.entry = tmpfs tmp tmpfs rw,nosuid,nodev 0 0",
    ]
    if sys_mode == "bind":
        # Mount flags that a child user namespace inherits are locked: the bind has to repeat
        # nosuid, nodev, noexec and the atime mode of the host's /sys (EINVAL without relatime).
        flags = run(["findmnt", "-no", "OPTIONS", "/sys"]).stdout.strip().split(",")
        keep = [f for f in flags if f in ("nosuid", "nodev", "noexec", "relatime", "noatime", "nodiratime")]
        base.append(f"lxc.mount.entry = /sys sys none rbind,ro,{','.join(keep)} 0 0")
        # A plain `bind` of /sys is refused (EINVAL: the host's sysfs has locked submounts), so the
        # recursive bind drags them in (securityfs, efivarfs, the host's cgroup2 root). Hide each
        # one below an empty read-only tmpfs; LXC's own cgroup mount goes on top of the last.
        for mount_point in sys_submounts():
            base.append(f"lxc.mount.entry = tmpfs {mount_point.lstrip('/')} tmpfs ro,nosuid,nodev,noexec,size=4k 0 0")
    # Host-root-owned sources: with and without an idmapped mount, to see what the guest's root gets.
    base += [
        f"lxc.mount.entry = /var/lib/spaces/{space}/home/root root none rbind,idmap=container 0 0",
        f"lxc.mount.entry = /var/cache/spaces/{space} var/cache none rbind,idmap=container 0 0",
        f"lxc.mount.entry = /var/lib/spaces/{space}/home/root run/plain-root none rbind,create=dir 0 0",
        f"lxc.mount.entry = /run/spaces/{space}/system-bus run/idmapped-bus none rbind,idmap=container,create=dir 0 0",
        f"lxc.mount.entry = /run/spaces/{space}/system-bus run/plain-bus none rbind,create=dir 0 0",
    ]
    return "\n".join(base) + "\n"


DEVICES = """lxc.cgroup2.devices.deny = a
lxc.cgroup2.devices.allow = c *:* m
lxc.cgroup2.devices.allow = b *:* m
lxc.cgroup2.devices.allow = c 1:3 rwm
lxc.cgroup2.devices.allow = c 1:5 rwm
lxc.cgroup2.devices.allow = c 1:7 rwm
lxc.cgroup2.devices.allow = c 5:0 rwm
lxc.cgroup2.devices.allow = c 5:1 rwm
lxc.cgroup2.devices.allow = c 5:2 rwm
lxc.cgroup2.devices.allow = c 1:8 rwm
lxc.cgroup2.devices.allow = c 1:9 rwm
lxc.cgroup2.devices.allow = c 136:* rwm
"""


def write_runtime(space: str, sys_mode: str, share_net: bool = True) -> Path:
    runtime = Path(LXC_PATH) / NAME
    sudo("mkdir", "-p", str(runtime))
    for name, text in (("config", config_text(space, sys_mode=sys_mode, share_net=share_net)), ("devices.conf", DEVICES)):
        subprocess.run(["sudo", "tee", str(runtime / name)], input=text, text=True, stdout=subprocess.DEVNULL, check=True)
    return runtime


def lxc(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return sudo(WRAPPER, *args, timeout=timeout)


def stop_scratch() -> None:
    lxc("lxc-stop", "-P", LXC_PATH, "-n", NAME, "-k", timeout=60)
    time.sleep(1)
    run(["sudo", "-n", "umount", f"{LXC_PATH}/{NAME}/netns"])
    sudo("sh", "-c", f"for d in $(find {CGROUP} -mindepth 1 -type d 2>/dev/null | sort -r) {CGROUP}; do rmdir $d 2>/dev/null; done")


def start_scratch() -> tuple[bool, str]:
    """Start the scratch container the way the launcher does (monitor in its own base cgroup)."""

    sudo("mkdir", "-p", str(CGROUP))
    log = f"{LXC_PATH}/{NAME}/lxc.log"
    sudo("rm", "-f", log)
    command = [
        "sudo", "-n", "sh", "-c", 'echo $$ >"$1" && shift && exec "$@"', "sh", str(CGROUP / "cgroup.procs"),
        WRAPPER, "lxc-start", "-P", LXC_PATH, "-n", NAME, "-d", "-o", log, "-l", "DEBUG",
    ]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90, check=False)
    state = lxc("lxc-info", "-P", LXC_PATH, "-n", NAME, "-s", "-H").stdout.strip()
    return state == "RUNNING", result.stdout.strip()[-400:]


def guest(*command: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "-n", WRAPPER, "lxc-attach", "-P", LXC_PATH, "-n", NAME, "--clear-env", "--", *command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False,
    )


def log_tail(pattern: str = r"ERROR|Failed|EPERM|denied", lines: int = 12) -> str:
    text = sudo("cat", f"{LXC_PATH}/{NAME}/lxc.log").stdout.splitlines()
    return "\n".join([line for line in text if re.search(pattern, line)][-lines:])


def gate_prereqs(space: str) -> bool:
    print("\n== gate 0: prerequisites ==", flush=True)
    fstype = run(["findmnt", "-T", f"/var/lib/spaces/{space}/rootfs", "-no", "FSTYPE"]).stdout.strip()
    print(f"INFO  filesystem of the rootfs: {fstype}; kernel {os.uname().release}")
    check("the rootfs filesystem supports idmapped mounts (ext4, xfs, btrfs, ...)", fstype in ("ext4", "xfs", "btrfs", "f2fs", "tmpfs"), fstype)
    for name in ("/etc/subuid", "/etc/subgid"):
        print(f"INFO  {name}: " + " ".join(Path(name).read_text().split()))
    print("INFO  idmap lines:\n      " + "\n      ".join(idmap_lines(IDENTITY_UID, IDENTITY_GID)))
    check("no space is running", not m9.stray(), str(m9.stray()))
    return bool(fstype) and not m9.stray()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--space", default="ubuntu")
    parser.add_argument("--keep", action="store_true", help="leave the scratch container running")
    parser.add_argument("--sys", choices=("mixed", "bind"), default="mixed", help="how /sys is provided to the guest")
    arguments = parser.parse_args()
    space = arguments.space
    if not gate_prereqs(space):
        return 1
    try:
        print("\n== gate 1/2: start with the shifted map and the shared network namespace ==", flush=True)
        write_runtime(space, arguments.sys)
        started, output = start_scratch()
        check(f"lxc-start with `sys:{arguments.sys}` reaches RUNNING", started, output + "\n" + log_tail())
        if not started:
            print("\nBLOCKER: the container did not start. Log excerpt:\n" + log_tail(lines=30))
            return 2
        init = lxc("lxc-info", "-P", LXC_PATH, "-n", NAME, "-p", "-H").stdout.strip()
        uid_map = sudo("cat", f"/proc/{init}/uid_map").stdout
        print(f"INFO  guest init pid {init}, uid_map:\n{uid_map}")
        net_same = sudo("readlink", f"/proc/{init}/ns/net").stdout == sudo("readlink", "/proc/1/ns/net").stdout
        check("the guest's init is in the host's network namespace", net_same)
        check("the guest has its own user namespace", sudo("readlink", f"/proc/{init}/ns/user").stdout != sudo("readlink", "/proc/1/ns/user").stdout)
        print("\n== gate 3: boot ==", flush=True)
        state = "?"
        for _attempt in range(60):
            lines = guest("systemctl", "is-system-running").stdout.strip().splitlines()
            state = lines[-1] if lines else "?"
            if state in ("running", "degraded"):
                break
            time.sleep(1)
        check("`systemctl is-system-running` = running", state == "running", state)
        failed = guest("systemctl", "--failed", "--no-legend", "--plain").stdout.strip()
        check("no failed unit", failed == "", failed)
        print("\n== gate 4: mount API probes ==", flush=True)
        classic = dict(line.split(":", 1) for line in guest("python3", "-c", m9.CLASSIC).stdout.split() if ":" in line)
        modern = dict(line.split(":", 1) for line in guest("python3", "-c", m9.NEW_API).stdout.split() if ":" in line)
        print(f"INFO  classic: {classic}\nINFO  new API: {modern}")
        check("mount(2) bind of /proc/sys remounted rw cannot write core_pattern", classic.get("bind-remount") in ("denied", "read-only"), str(classic))
        check("a fresh proc via the new mount API cannot write core_pattern or open sysrq-trigger",
              modern.get("new-proc-core_pattern") != "WRITABLE" and modern.get("new-proc-sysrq") != "WRITABLE", str(modern))
        check("a cloned /proc/sys cannot be written", modern.get("clone") != "WRITABLE", str(modern))
        print("\n== gate 5: identity ==", flush=True)
        print("INFO  " + guest("sh", "-c", "id; stat -c '%u:%g %a %n' / /etc/shadow /root /var/cache /run/plain-root /run/idmapped-bus /run/plain-bus; touch /root/x /var/cache/x && echo wrote-root-and-cache; rm -f /root/x /var/cache/x").stdout.replace("\n", "\n      "))
        print("INFO  /sys submounts:\n      " + guest("findmnt", "-R", "/sys", "-o", "TARGET,FSTYPE,OPTIONS").stdout.replace("\n", "\n      "))
        print("INFO  write probes as guest root:\n      " + guest("sh", "-c", "for f in /sys/kernel/security/apparmor/.load /sys/kernel/uevent_helper /sys/class/leds /sys/fs/cgroup/cgroup.procs /sys/power/state; do (echo x > $f) 2>&1 | head -1; echo \"$f -> $?\"; done").stdout.replace("\n", "\n      "))
        print("INFO  resolved:\n      " + guest("journalctl", "-u", "systemd-resolved", "-b", "--no-pager", "-n", "12").stdout.replace("\n", "\n      "))
    finally:
        if not arguments.keep:
            stop_scratch()
    return 1 if m9.m8.failed else 0


if __name__ == "__main__":
    sys.exit(main())
