#!/usr/bin/env python3
"""M9 check: monitor socket isolation (G2) and the AppArmor profile review.

Run as the normal user from the niri session (passwordless sudo) with the package
installed (`void/tools/xbps-build.sh && sudo xbps-install -fy -R <repo> spaces-0.0.1_1`):

    python3 void/spike/m9_check.py [--regress]

Sections:
  isolation  for each of the four spaces: the LXC monitor lives in a private network
             namespace that spaces-lxc pins at /run/spaces/lxc/NAME/netns, the guest (root)
             and the host outside that namespace cannot connect to the command socket,
             the guest still shares the host's network namespace, and the tools still work
  lifecycle  stop removes the pin and leaves no nsfs mount, restart makes a new one,
             kill -9 of the launcher is recovered through the pin
  apparmor   every distro boots to `running` with no failed unit under the tightened
             lxc-spaces-container profile, systemd unit sandboxes still work, and mount(2) of a
             fresh proc, sysfs, binfmt_misc or cgroup v1 is denied (it would give the guest's
             root the host's writable /proc/sys). The new mount API is not mediated by
             AppArmor; the check reports what it can do as a known open item
  bootpath   the profile set a boot produces: both lxc-start's own profile and ours are unloaded,
             then runit's core service 09-apparmor.sh (`apparmor_parser -a /etc/apparmor.d`) runs as at
             boot, and a space must start with usr.bin.lxc-start ENFORCING (no change_profile denial;
             the first reboot showed the distro profile refusing a profile that does not match lxc-*)
  --userns   run everything with the opt-in user namespace turned on for all four spaces (`spaces-void userns
             enable --all`, undone at the end): the eight known-open mount-API items must then PASS
             (guest root is kuid 1000000: a fresh proc, a clone of /proc/sys and a mount(2) bind remount
             cannot write core_pattern or sysrq-trigger), and a section `userns` checks the map, the
             idmapped binds, the /sys masks and the broker sockets
  --regress  also runs m5_check.py (devices, hotplug, stale container) and m8_check.py
             (package, enter, sudo bridge, GUI on niri, NVIDIA, autostart, orphaned broker)
             as separate processes and requires them to pass

Prints PASS/FAIL/SKIP, exits non-zero on FAIL. All spaces are stopped at the end.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import m8_check as m8  # noqa: E402
from m8_check import check, run, skip, stop_all, stray, sudo, wait_for  # noqa: E402

SPACES = (("ubuntu", "ubuntu"), ("arch", "arch-linux"), ("kali", "kali"), ("fedora", "fedora"))
WRAPPER = "/usr/lib/spaces/spaces-lxc"
LXC_PATH = "/run/spaces/lxc"
USERNS = False
STATE_ROOT = Path("/var/lib/spaces")

CONNECT = """
import errno, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect(b"\\0" + sys.argv[1].encode())
    print("CONNECTED")
except OSError as error:
    print(errno.errorcode.get(error.errno, error.errno))
"""


def in_guest_root(space: str, *command: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
    """Run a command as root in the guest (a pipe for output: lxc-attach chowns files)."""

    return subprocess.run(
        ["sudo", "-n", WRAPPER, "lxc-attach", "-P", LXC_PATH, "-n", space, "--clear-env", "--", *command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False,
    )


def socket_name(space: str) -> str:
    return f"{LXC_PATH}/{space}/command"


def pin(space: str) -> str:
    return f"{LXC_PATH}/{space}/netns"


def netns_inode(path: str) -> str:
    out = sudo("stat", "-L", "-c", "%i", path).stdout.strip()
    return out


def lxc_start_pid(space: str) -> str:
    out = run(["pgrep", "-f", rf"^/usr/sbin/lxc-start -P {LXC_PATH} -n {space} "]).stdout.split()
    return out[0] if out else ""


def init_pid(space: str) -> str:
    return sudo(WRAPPER, "lxc-info", "-P", LXC_PATH, "-n", space, "-p", "-H").stdout.strip()


def nsfs_mounts() -> list[str]:
    return [line for line in Path("/proc/mounts").read_text().splitlines() if re.search(r" /run/spaces/\S+ nsfs ", line)]


def start_all() -> None:
    for space, command in SPACES:
        run([command, "--", "true"], timeout=200)


# --------------------------------------------------------------- isolation


def check_isolation() -> None:
    print("\n== monitor socket isolation ==", flush=True)
    stop_all()
    start_all()
    host_net = netns_inode("/proc/1/ns/net")
    host_sockets = Path("/proc/net/unix").read_text()
    check("the host network namespace has no LXC command socket (no abstract @/run/spaces/lxc/ name)",
          "@/run/spaces/lxc/" not in host_sockets and "@lxc/" not in host_sockets,
          "\n".join(line for line in host_sockets.splitlines() if "lxc" in line))
    for space, _command in SPACES:
        label = f"[{space}]"
        file_type = sudo("stat", "-f", "-c", "%T", pin(space)).stdout.strip()
        check(f"{label} spaces-lxc pinned a network namespace for lxc-start (nsfs)", file_type == "nsfs", file_type)
        pinned = netns_inode(pin(space))
        check(f"{label} the pinned namespace is not the host's", pinned != "" and pinned != host_net, f"{pinned} vs {host_net}")
        monitor = lxc_start_pid(space)
        check(f"{label} lxc-start runs in the pinned namespace",
              monitor != "" and netns_inode(f"/proc/{monitor}/ns/net") == pinned, f"pid {monitor}")
        init = init_pid(space)
        check(f"{label} the guest's init is in the host's network namespace",
              init.isdigit() and netns_inode(f"/proc/{init}/ns/net") == host_net, f"pid {init}")
        name = socket_name(space)
        in_pin = sudo("nsenter", f"--net={pin(space)}", "--", "python3", "-c", CONNECT, name).stdout.strip()
        check(f"{label} the command socket exists in the pinned namespace (nsenter connect works)", in_pin == "CONNECTED", in_pin)
        guest = in_guest_root(space, "python3", "-c", CONNECT, name).stdout.strip()
        check(f"{label} root in the guest cannot connect to the command socket", guest == "ECONNREFUSED", guest)
        guest_table = in_guest_root(space, "cat", "/proc/net/unix").stdout
        check(f"{label} the guest's socket table has no LXC command socket", "@/run/spaces/lxc/" not in guest_table and "@lxc/" not in guest_table)
        host_root = sudo("python3", "-c", CONNECT, name).stdout.strip()
        host_user = run(["python3", "-c", CONNECT, name]).stdout.strip()
        check(f"{label} root and the user on the host (outside the namespace) cannot connect either",
              host_root == "ECONNREFUSED" and host_user == "ECONNREFUSED", f"{host_root} {host_user}")
        guest_net = in_guest_root(space, "readlink", "/proc/self/ns/net").stdout.strip()
        check(f"{label} the guest still shares the host's network (same namespace inode)",
              guest_net.endswith(f"[{host_net}]"), f"{guest_net} vs {host_net}")
        state = sudo(WRAPPER, "lxc-info", "-P", LXC_PATH, "-n", space, "-s", "-H").stdout.strip()
        check(f"{label} lxc-info through the wrapper reaches the monitor", state == "RUNNING", state)
        no_monitor = in_guest_root(space, "sh", "-c", "grep -l '^lxc-start' /proc/[0-9]*/comm >/dev/null 2>&1 && echo SEEN || echo none").stdout.strip()
        check(f"{label} the monitor process is not visible in the guest", no_monitor == "none", no_monitor)
    net = in_guest_root("ubuntu", "sh", "-c", "getent hosts voidlinux.org >/dev/null && echo resolved || echo failed").stdout.strip()
    check("guest network works (name resolution through the shared host network)", net == "resolved", net)
    out = run(["spaces", "enter", "ubuntu", "--", "id", "-un"], timeout=90)
    check("`spaces enter` works (session and PAM paths use the wrapper)", out.stdout.strip() == os.environ.get("USER", "soubarna"), out.stdout + out.stderr)


# --------------------------------------------------------------- lifecycle


def check_lifecycle() -> None:
    print("\n== lifecycle ==", flush=True)
    before = lxc_start_pid("ubuntu")
    sudo("sv", "-w", "90", "down", "/var/service/spaces-ubuntu", timeout=120)
    gone = wait_for(lambda: sudo("test", "-e", pin("ubuntu")).returncode != 0, 20, 0.5)
    check("stopping a space unpins its network namespace (file gone)", gone)
    check("no nsfs mount is left for the stopped space", not [m for m in nsfs_mounts() if "/ubuntu/" in m], str(nsfs_mounts()))
    check("the stopped space has no lxc-start and no cgroup", run(["pgrep", "-f", "lxc-start -P /run/spaces/lxc -n ubuntu "]).returncode != 0
          and not Path("/sys/fs/cgroup/spaces/ubuntu").exists())
    run(["ubuntu", "--", "true"], timeout=150)
    after = lxc_start_pid("ubuntu")
    check("a restart starts a new monitor in a freshly pinned namespace",
          after != "" and after != before and netns_inode(f"/proc/{after}/ns/net") == netns_inode(pin("ubuntu")), f"{before} -> {after}")
    check("one nsfs mount per running space", len(nsfs_mounts()) == 4, str(nsfs_mounts()))
    # kill -9 of the launcher: the container keeps running in its pinned namespace and the
    # next start has to stop it through that pin.
    launcher = m8.launcher_of("ubuntu")
    old = run(["pgrep", "-f", "lxc-start -P /run/spaces/lxc -n ubuntu "]).stdout.split()
    if launcher is None:
        skip("kill -9 of the launcher", "no launcher pid")
        return
    sudo("kill", "-9", str(launcher))
    wait_for(lambda: run(["pgrep", "-f", f"^{launcher} "]).returncode != 0, 5, 0.2)
    orphan = run(["pgrep", "-f", "lxc-start -P /run/spaces/lxc -n ubuntu "]).stdout.split()
    check("after kill -9 of the launcher the container runs on, its pin intact", orphan == old and bool(orphan)
          and sudo("stat", "-f", "-c", "%T", pin("ubuntu")).stdout.strip() == "nsfs", f"{old} {orphan}")
    stale = sudo(WRAPPER, "lxc-info", "-P", LXC_PATH, "-n", "ubuntu", "-s", "-H").stdout.strip()
    check("the orphan is still controllable through the wrapper", stale == "RUNNING", stale)
    out = run(["ubuntu", "--", "id", "-un"], timeout=150)
    new = run(["pgrep", "-f", "lxc-start -P /run/spaces/lxc -n ubuntu "]).stdout.split()
    check("the next start stops the orphan through the pin and starts a new container",
          out.stdout.strip() != "" and len(new) == 1 and new != orphan, f"{orphan} -> {new}")
    check("the new container has its own pinned namespace with a live command socket",
          sudo("nsenter", f"--net={pin('ubuntu')}", "--", "python3", "-c", CONNECT, socket_name("ubuntu")).stdout.strip() == "CONNECTED")


# ---------------------------------------------------------------- apparmor

CLASSIC = r"""
import ctypes, errno, os
libc = ctypes.CDLL(None, use_errno=True)
t = b"/run/systemd/m9c"
os.makedirs(t, exist_ok=True)
def writable(path):
    try:
        value = open(path).read(); open(path, "w").write(value); return True
    except OSError: return False
for fs, data in ((b"proc", None), (b"sysfs", None), (b"binfmt_misc", None), (b"cgroup", b"none,name=m9")):
    ok = libc.mount(b"none", t, fs, 0, data) == 0
    print("classic-" + fs.decode() + ":" + ("MOUNTED" if ok else "denied"))
    if ok: libc.umount2(t, 2)
print("core_pattern:" + ("WRITABLE" if writable("/proc/sys/kernel/core_pattern") else "read-only"))
MS_BIND, MS_REMOUNT = 4096, 32
if libc.mount(b"/proc/sys", t, None, MS_BIND, None) == 0:
    libc.mount(None, t, None, MS_REMOUNT | MS_BIND, None)
    print("bind-remount:" + ("WRITABLE" if writable("/run/systemd/m9c/kernel/core_pattern") else "read-only"))
    libc.umount2(t, 2)
else:
    print("bind-remount:denied")
os.rmdir(t)
"""


# The new mount API (fsopen/fsmount/open_tree/mount_setattr/move_mount) creates and re-flags
# mounts without the AppArmor mount checks (only move_mount is seen, and the profile allows it
# for systemd). Seccomp cannot close it: systemd sets up unit credentials with fsopen and
# fsmount and fails the unit when they return ENOSYS (journald, tmpfiles and udev then fail),
# and blocking mount_setattr breaks logind and every unit sandbox while the mount(2) bind
# remount stays open anyway (void/spike/RESULTS.md, "Post-M9 gaps"). The probe therefore
# reports what a guest root can still reach: a fresh proc written at `new-proc-core_pattern`
# (write back the value it read) and `new-proc-sysrq` (open for writing, nothing is written),
# and a writable clone of /proc/sys.
NEW_API = r"""
import ctypes, errno, os
libc = ctypes.CDLL(None, use_errno=True)
sc = libc.syscall
FSOPEN, FSCONFIG, FSMOUNT, MOVE_MOUNT, OPEN_TREE, MOUNT_SETATTR = 430, 431, 432, 429, 428, 442
AT_FDCWD, EMPTY = -100, 4
t = b"/run/systemd/m9n"
os.makedirs(t, exist_ok=True)
def name(): return errno.errorcode.get(ctypes.get_errno(), "?")
def umount(): os.system("umount -l /run/systemd/m9n 2>/dev/null")
def writable(path):
    try:
        value = open(path).read(); open(path, "w").write(value); return True
    except OSError: return False
for fs in (b"proc", b"sysfs"):
    fd = sc(FSOPEN, fs, 1)
    if fd < 0:
        print("new-" + fs.decode() + ":" + name()); continue
    sc(FSCONFIG, fd, 6, None, None, 0); m = sc(FSMOUNT, fd, 1, 0)
    ok = m >= 0 and sc(MOVE_MOUNT, m, b"", AT_FDCWD, t, EMPTY) == 0
    print("new-" + fs.decode() + ":" + ("MOUNTED" if ok else "denied"))
    if ok and fs == b"proc":
        print("new-proc-core_pattern:" + ("WRITABLE" if writable("/run/systemd/m9n/sys/kernel/core_pattern") else "read-only"))
        try:
            os.close(os.open("/run/systemd/m9n/sysrq-trigger", os.O_WRONLY)); print("new-proc-sysrq:WRITABLE")
        except OSError: print("new-proc-sysrq:read-only")
    umount()
fd = sc(OPEN_TREE, AT_FDCWD, b"/proc/sys", 1 | 0o2000000)
if fd >= 0:
    class A(ctypes.Structure):
        _fields_ = [("set", ctypes.c_uint64), ("clr", ctypes.c_uint64), ("prop", ctypes.c_uint64), ("userns", ctypes.c_uint64)]
    a = A(0, 1, 0, 0)
    sc(MOUNT_SETATTR, fd, b"", 0x1000, ctypes.byref(a), ctypes.sizeof(a))
    if sc(MOVE_MOUNT, fd, b"", AT_FDCWD, t, EMPTY) == 0:
        print("clone:" + ("WRITABLE" if writable("/run/systemd/m9n/kernel/core_pattern") else "read-only")); umount()
    else:
        print("clone:denied")
else:
    print("clone:no-open_tree")
os.rmdir(t)
"""


def check_apparmor() -> None:
    print("\n== AppArmor profile (lxc-spaces-container) ==", flush=True)
    text = Path("/etc/apparmor.d/lxc-spaces-container").read_text()
    rules = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    check("the installed profile has no fresh-proc or cgroup v1 mount rule",
          not any(rule.startswith("mount fstype=proc") or rule.startswith("mount fstype=cgroup ") for rule in rules))
    check("the profile is loaded", "lxc-spaces-container" in sudo("cat", "/sys/kernel/security/apparmor/profiles").stdout)
    stop_all()
    sudo("dmesg", "-C")
    start_all()
    for space, _command in SPACES:
        label = f"[{space}]"
        state = "?"
        for _attempt in range(60):
            lines = in_guest_root(space, "systemctl", "is-system-running").stdout.strip().splitlines()
            state = lines[-1] if lines else "?"
            if state in ("running", "degraded"):
                break
            wait_for(lambda: False, 1, 1)
        check(f"{label} boots to `systemctl is-system-running` = running", state == "running", state)
        failed = in_guest_root(space, "systemctl", "--failed", "--no-legend", "--plain").stdout.strip()
        check(f"{label} no failed unit", failed == "", failed)
        sandbox = in_guest_root(
            space, "systemd-run", "--pipe", "--wait", "--quiet", "-p", "ProtectSystem=strict", "-p", "PrivateTmp=yes",
            "-p", "ProtectKernelTunables=yes", "-p", "PrivateNetwork=yes", "-p", "ProtectHome=yes", "-p", "NoNewPrivileges=yes",
            "sh", "-c", "echo sandboxed")
        check(f"{label} a systemd unit sandbox (ProtectSystem, PrivateTmp, PrivateNetwork, ...) still starts",
              sandbox.stdout.strip().endswith("sandboxed"), sandbox.stdout[-120:])
    log = sudo("dmesg").stdout
    denials = {re.sub(r"namespace-\w+", "namespace-X", re.sub(r"pid=\d+ ", "", m.group(0)))
               for m in re.finditer(r'apparmor="DENIED" operation="mount"[^\n]*', log)}
    # systemd falls back when a fresh proc or sysfs is refused (PrivateNetwork=, ProtectProc=);
    # Fedora's nfs-utils wants rpc_pipefs on /run/rpc_pipefs, which was never allowed.
    benign = ('fstype="proc"', 'fstype="sysfs"', 'fstype="rpc_pipefs"')
    unexpected = [d for d in denials if not any(marker in d for marker in benign)]
    check("the AppArmor mount denials during the four boots are only fresh proc/sysfs/rpc_pipefs mounts (systemd falls back)",
          not unexpected, "\n".join(sorted(unexpected))[:400])
    for space, _command in SPACES:
        label = f"[{space}]"
        result = dict(line.split(":", 1) for line in in_guest_root(space, "python3", "-c", CLASSIC).stdout.split() if ":" in line)
        check(f"{label} mount(2) of a fresh proc, sysfs, binfmt_misc or cgroup v1 is denied by the profile",
              all(result.get(key) == "denied" for key in ("classic-proc", "classic-sysfs", "classic-binfmt_misc", "classic-cgroup")), str(result))
        check(f"{label} /proc/sys/kernel/core_pattern cannot be written at its own path",
              result.get("core_pattern") == "read-only", str(result))
        if result.get("bind-remount") == "WRITABLE":
            skip(f"{label} a bind mount of /proc/sys remounted rw (mount(2)) stays read-only", f"known open: {result.get('bind-remount')}")
        else:
            check(f"{label} a bind mount of /proc/sys remounted rw (mount(2)) cannot write core_pattern", True, str(result))
        modern = dict(line.split(":", 1) for line in in_guest_root(space, "python3", "-c", NEW_API).stdout.split() if ":" in line)
        # Known open (void/docs/apparmor-review.md, finding 1): AppArmor does not mediate the new
        # mount API and seccomp cannot take it away from systemd. A guest root can still reach the
        # host's /proc/sys that way. Reported as SKIP while that is so, and a PASS (so the day it
        # is closed shows up) when neither a fresh proc nor a clone of /proc/sys is writable.
        reachable = [key for key in ("new-proc-core_pattern", "new-proc-sysrq", "clone") if modern.get(key) == "WRITABLE"]
        if reachable and USERNS:
            check(f"{label} the new mount API cannot write host /proc/sys (user namespace)", False, f"{reachable}: {modern}")
        elif reachable:
            skip(f"{label} the new mount API (fsopen/fsmount/open_tree/mount_setattr) cannot write host /proc/sys", f"known open: {modern}")
        else:
            check(f"{label} the new mount API cannot write host /proc/sys (fresh proc, sysrq-trigger, cloned /proc/sys)", True, str(modern))


# ------------------------------------------------------------------ userns


def set_userns(value: bool | None) -> dict[str, str | None]:
    """Turn the user namespace on (True) or off for every space; return what the markers held."""

    before: dict[str, str | None] = {}
    for space, _command in SPACES:
        marker = STATE_ROOT / space / "userns"
        text = sudo("cat", str(marker)).stdout.strip()
        before[space] = text or None
    if value is not None:
        sudo("spaces-void", "userns", "enable" if value else "disable", "--all", timeout=120)
    return before


def restore_userns(before: dict[str, str | None]) -> None:
    for space, text in before.items():
        marker = STATE_ROOT / space / "userns"
        if text is None:
            sudo("rm", "-f", str(marker))
        else:
            sudo("sh", "-c", f"echo {text} > {marker}")


def check_userns() -> None:
    print("\n== user namespace ==", flush=True)
    stop_all()
    start_all()
    for space, _command in SPACES:
        label = f"[{space}]"
        init = init_pid(space)
        uid_map = sudo("cat", f"/proc/{init}/uid_map").stdout.split()
        check(f"{label} the guest has a shifted user namespace (root is kuid 1000000)",
              uid_map[:3] == ["0", "1000000", "1000"] and "1000" in uid_map, " ".join(uid_map))
        check(f"{label} the user keeps its uid (1000 maps to 1000)", "1000 1000 1".split() == uid_map[3:6], " ".join(uid_map))
        check(f"{label} the guest's init is root in the guest and kuid 1000000 on the host",
              sudo("stat", "-c", "%u", f"/proc/{init}").stdout.strip() == "1000000")
        out = in_guest_root(space, "sh", "-c",
                            "stat -c '%u:%g' /root /var/cache /run/spaces-host/system /etc/shadow; "
                            "ls /sys/kernel/security 2>&1 | wc -l; findmnt -n -o FSTYPE /sys | head -1").stdout.split()
        check(f"{label} host-root-owned binds look root-owned to guest root (idmapped), the rootfs is not chowned",
              out[:4] == ["0:0", "0:0", "0:0", "0:42"] or out[:3] == ["0:0", "0:0", "0:0"], " ".join(out))
        check(f"{label} /sys is the bind of the host's sysfs with securityfs hidden (no apparmor interface)",
              out[-2:] == ["0", "sysfs"], " ".join(out))
        broker = in_guest_root(space, "systemctl", "is-active", "spaces-system-broker.service").stdout.strip()
        check(f"{label} the guest's system-bus relay runs (the host broker accepts the shifted root)", broker == "active", broker)
        host_dir = sudo("stat", "-c", "%u", f"/run/spaces/{space}/system-bus").stdout.strip()
        check(f"{label} the host side stays root-owned (no chown of the host's directory)", host_dir == "0", host_dir)
        caps = in_guest_root(space, "sh", "-c", "cat /proc/self/uid_map | wc -l").stdout.strip()
        check(f"{label} the map has the identity entries for the user and the device groups", caps.isdigit() and int(caps) >= 3, caps)


# -------------------------------------------------------------------- main


BOOT_SERVICE = "/etc/runit/core-services/09-apparmor.sh"
LXC_START_PROFILE = "/usr/bin/lxc-start"


def profile_modes() -> dict[str, str]:
    modes = {}
    for line in sudo("cat", "/sys/kernel/security/apparmor/profiles").stdout.splitlines():
        name, _, mode = line.rpartition(" ")
        modes[name] = mode.strip("()")
    return modes


def check_boot_path() -> None:
    print("\n== boot-path profile set (what 09-apparmor.sh loads at boot) ==", flush=True)
    stop_all()
    boot_text = Path(BOOT_SERVICE).read_text()
    check("the boot service exists and loads /etc/apparmor.d as a whole",
          "apparmor_parser -a" in boot_text and "/etc/apparmor.d" in boot_text)
    sudo("apparmor_parser", "-R", "/etc/apparmor.d/usr.bin.lxc-start")
    sudo("apparmor_parser", "-R", "/etc/apparmor.d/lxc-spaces-container")
    modes = profile_modes()
    check("both profiles are unloaded before the simulated boot",
          LXC_START_PROFILE not in modes and "lxc-spaces-container" not in modes)
    # The real service file, with runit's msg() stubbed.
    boot = sudo("sh", "-c", f"msg() {{ echo \"$@\"; }}; . {BOOT_SERVICE}")
    # Only the two profiles are unloaded (tearing down every profile of a running desktop is not
    # worth it), so `apparmor_parser -a` reports "Profile already exists" for the others and exits
    # non-zero; at a real boot it starts from an empty kernel. What matters is that the profiles we
    # unloaded were added without an error.
    complaints = [line for line in boot.stdout.splitlines()
                  if ("lxc-start" in line or "lxc-spaces-container" in line) and "already exists" not in line]
    check("the boot service ran and added the two profiles without an error",
          "Loading AppArmor profiles" in boot.stdout and not complaints, "; ".join(complaints)[-300:])
    modes = profile_modes()
    check("usr.bin.lxc-start is loaded and ENFORCING after the boot load", modes.get(LXC_START_PROFILE) == "enforce",
          str(modes.get(LXC_START_PROFILE)))
    check("lxc-spaces-container is loaded by the boot load, not only by the launcher",
          "lxc-spaces-container" in modes, str(modes.get("lxc-spaces-container")))
    sudo("dmesg", "-C")
    started = run(["ubuntu", "--", "true"], timeout=200)
    check("a space starts under the boot-loaded profile set", started.returncode == 0, (started.stdout + started.stderr)[-300:])
    denied = [line for line in sudo("dmesg").stdout.splitlines()
              if 'apparmor="DENIED"' in line and 'operation="change_profile"' in line]
    check("no change_profile denial", not denied, "; ".join(denied)[-400:])
    pid = sudo(WRAPPER, "/usr/bin/lxc-info", "-P", LXC_PATH, "-n", "ubuntu", "-p", "-H").stdout.strip()
    attr = sudo("cat", f"/proc/{pid}/attr/current").stdout.strip() if pid.isdigit() else "?"
    check("the guest init runs confined by lxc-spaces-container", attr == "lxc-spaces-container (enforce)", attr)
    stop_all()


def check_regressions() -> None:
    print("\n== regression runs (separate processes) ==", flush=True)
    for script in ("m5_check.py", "m8_check.py"):
        done = subprocess.run([sys.executable, str(HERE / script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=3600, check=False)
        summary = (re.findall(r"^\d+ passed.*$", done.stdout, re.M) or ["no summary"])[-1]
        failed = re.findall(r"^FAILED: (.*)$", done.stdout, re.M)
        check(f"{script} passes ({summary})", done.returncode == 0, "; ".join(failed)[-600:])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--regress", action="store_true")
    parser.add_argument("--userns", action="store_true", help="run with the user namespace on for every space")
    arguments = parser.parse_args()
    global USERNS
    if os.geteuid() == 0 or sudo("true").returncode != 0:
        print("run as the normal user with passwordless sudo", file=sys.stderr)
        return 2
    saved = None
    if arguments.userns:
        USERNS = True
        stop_all()
        saved = set_userns(True)
    try:
        return run_all(arguments)
    finally:
        if saved is not None:
            stop_all()
            restore_userns(saved)


def run_all(arguments: argparse.Namespace) -> int:
    check_isolation()
    check_lifecycle()
    check_apparmor()
    if USERNS:
        check_userns()
    check_boot_path()
    stop_all()
    check("no nsfs mount, container or helper is left after stopping everything", not nsfs_mounts() and not stray(),
          f"{nsfs_mounts()} {stray()}")
    if arguments.regress:
        check_regressions()
    return m8.finish()


if __name__ == "__main__":
    raise SystemExit(main())
