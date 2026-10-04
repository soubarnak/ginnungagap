#!/usr/bin/env python3
"""M6 check: the kali, arch and fedora spaces, plus the full-level watchdog fix.

Run as the normal user from the niri session (needs passwordless sudo):

    python3 void/spike/m6_check.py [--distro arch|kali|fedora] [--no-create]
                                   [--no-extras] [--watchdog]

For every selected distro: create the space when it does not exist yet
(`sudo spaces create D --preset basic`, slow; --no-create reports FAIL
instead), boot it, check os-release, enter as the user, the host-PAM sudo
bridge (reuses m3_check with a throwaway host user), install a small GTK app
and the Vulkan/GL tools (once, kept; --no-extras skips that and everything
that needs it), a window on niri, nvidia-smi through the NVIDIA farm, the farm
destination directory and guest glibc, vulkaninfo and EGL hardware rendering.
--watchdog additionally runs the `full` device level test on the ubuntu space
(guest root cannot reach watchdog or VT devices; `basic` is restored; the
host watchdog is only read through sysfs and disarmed if it ever arms).
Prints PASS/FAIL/SKIP per item and exits non-zero when anything failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

import m3_check as m3  # noqa: E402
import m5_check as m5  # noqa: E402

DISTROS = {
    "kali": {
        "os_id": "kali",
        "libdir": "/usr/lib/x86_64-linux-gnu",
        "install": "apt-get install -y --no-install-recommends gnome-calculator vulkan-tools mesa-utils "
                   "mesa-vulkan-drivers libgl1-mesa-dri libegl1 libgbm1 libvulkan1",
        "env": "DEBIAN_FRONTEND=noninteractive",
    },
    "arch": {
        "os_id": "arch",
        "libdir": "/usr/lib",
        "install": "pacman -Syu --noconfirm --needed gnome-calculator vulkan-tools mesa-utils "
                   "vulkan-radeon vulkan-icd-loader mesa libglvnd",
        "env": "LC_ALL=C",
    },
    "fedora": {
        "os_id": "fedora",
        "libdir": "/usr/lib64",
        "install": "dnf5 install -y --setopt=install_weak_deps=False gnome-calculator vulkan-tools "
                   "glx-utils mesa-vulkan-drivers mesa-dri-drivers mesa-libEGL mesa-libgbm vulkan-loader",
        "env": "LC_ALL=C",
    },
}
WATCHDOG_STATE = Path("/sys/class/watchdog/watchdog0/state")
results = m5.results


def _run(argv, *, timeout=120, input=None):
    """m5.run without stderr (the launcher prints warnings there)."""

    try:
        return subprocess.run(
            argv, input=input, stdin=None if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        out = error.stdout or ""
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return subprocess.CompletedProcess(argv, 124, out + "\n[timeout]")


m5.run = _run
m3.run = _run
report, check, skip = m5.report, m5.check, m5.skip


def use(distro: str) -> None:
    """Point the m3/m5 helpers at another space."""

    m5.SPACE = distro
    m5.SERVICE = f"/var/service/spaces-{distro}"
    m5.LOG = f"/var/log/spaces/{distro}/current"
    m5.INFO = f"/var/lib/spaces/{distro}/info.json"
    m5.ROOTFS = f"/var/lib/spaces/{distro}/rootfs"
    m3.SPACE = distro
    m3.STATE = Path("/var/lib/spaces") / distro
    m3.SERVICE = m5.SERVICE
    m3.RUNTIME = Path("/run/spaces/lxc") / distro


def guest_user(command: str, **kw):
    return m5.guest(command, **kw)


def guest_root(command: str, **kw):
    return m5.guest_root(command, **kw)


def exists(distro: str) -> bool:
    return m5.sudo("test", "-f", f"/var/lib/spaces/{distro}/info.json").returncode == 0


def create(distro: str) -> None:
    started = time.monotonic()
    done = m5.run(["sudo", "-n", "spaces", "create", distro, "--preset", "basic"], timeout=4 * 3600)
    minutes = (time.monotonic() - started) / 60
    print(done.stdout[-600:])
    report(f"{distro}: spaces create --preset basic", "PASS" if done.returncode == 0 else "FAIL",
           f"{minutes:.1f} min")


def disk_used(distro: str) -> str:
    done = m5.sudo("du", "-sxh", f"/var/lib/spaces/{distro}", "/var/cache/spaces/" + distro)
    return " ".join(done.stdout.split()[::2][:2]) if done.returncode == 0 else "?"


def check_basics(distro: str, spec: dict) -> bool:
    started = time.monotonic()
    ok = m5.start_space()
    check(f"{distro}: spaces enter starts the space and runs a command", ok,
          f"{time.monotonic() - started:.1f}s")
    if not ok:
        return False
    done = m5.sudo(m5.LXC_WRAPPER, "/usr/bin/lxc-info", "-P", m5.LXC_PATH, "-n", distro, "-s", "-H")
    check(f"{distro}: container state is RUNNING", done.stdout.strip() == "RUNNING", done.stdout.strip())
    done = guest_root("systemctl is-system-running --wait", timeout=180)
    state = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    detail = state
    if state != "running":
        failed = guest_root("systemctl --failed --no-legend --plain | head -5").stdout.strip()
        detail += " | " + failed.replace("\n", "; ")
    check(f"{distro}: guest systemd reports running", state == "running", detail[:160])
    osrel = guest_user("cat /etc/os-release").stdout
    check(f"{distro}: /etc/os-release is {spec['os_id']}", f"\nID={spec['os_id']}\n" in "\n" + osrel + "\n",
          (re.findall(r'PRETTY_NAME="?([^"\n]*)', osrel) or ["?"])[0])
    noisy = subprocess.run(["spaces", "enter", distro, "--", "true"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    check(f"{distro}: spaces enter prints no launcher warnings", noisy.returncode == 0 and not noisy.stderr.strip(),
          noisy.stderr.strip()[:100])
    done = guest_user("id -u; id -un")
    check(f"{distro}: enter runs as the host user", done.stdout.split()[:1] == [str(os.getuid())], done.stdout.strip()[:40])
    return True


def check_sudo_bridge(distro: str) -> None:
    """Guest sudo accepts the HOST password (throwaway host user, as in m3_check)."""

    use(distro)
    state = Path("/var/lib/spaces") / distro
    info_path = state / "info.json"
    backup = Path(f"/run/spaces-m6check-{distro}")
    password = secrets.token_urlsafe(14)
    files = ("passwd", "group", "shadow", "gshadow", "passwd-", "group-", "shadow-", "gshadow-")
    try:
        m3.stop_space()
        m5.sudo("rm", "-rf", str(backup))
        m5.sudo("install", "-d", "-m", "0700", str(backup))
        for name in files:
            m5.sudo("cp", "-a", str(state / "rootfs/etc" / name), str(backup / name))
        m5.sudo("cp", "-a", str(info_path), str(backup / "info.json"))
        done = m5.sudo("useradd", "-m", "-u", str(m3.TEST_UID), "-U", "-s", "/bin/bash", m3.TEST_USER)
        hashed = m5.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n").stdout.strip()
        done2 = m5.sudo("usermod", "-p", hashed, m3.TEST_USER)
        code = (
            "import json\nfrom spaces import priv\n"
            f"info=json.load(open({str(info_path)!r}))\n"
            f"rec=info['permissions']['users'][{str(os.getuid())!r}]['permissions']\n"
            f"priv.configure({{'schema_version':1,'name':{distro!r},'permissions':{{'user':"
            f"{{'uid':{m3.TEST_UID},'gid':{m3.TEST_UID},'permissions':rec}}}}}})\n"
        )
        added = m5.sudo("/usr/bin/python3", "-I", "-c", code)
        check(f"{distro}: throwaway host user added to the space",
              done.returncode == 0 and done2.returncode == 0 and added.returncode == 0, added.stdout.strip()[-100:])
        argv = ["/usr/bin/spaces.priv", "enter-as-user", m3.TEST_USER, distro, "--", "sh", "-c", "sudo -S -p '' id -u"]
        right = m5.sudo(*argv, input=password + "\n")
        wrong = m5.sudo(*argv, input="definitely-wrong\n")
        check(f"{distro}: guest sudo accepts the HOST password (pam_spaces.so, spaces-pam)",
              right.returncode == 0 and right.stdout.strip().splitlines()[-1:] == ["0"],
              right.stdout.strip().replace("\n", " ")[-70:])
        check(f"{distro}: a wrong password is rejected",
              wrong.returncode != 0 and "0" not in wrong.stdout.split(),
              f"exit {wrong.returncode}")
    finally:
        m3.stop_space()
        m5.sudo("userdel", "-f", "-r", m3.TEST_USER)
        if m5.sudo("test", "-d", str(backup)).returncode == 0:
            m5.sudo("cp", "-a", str(backup / "info.json"), str(info_path))
            for name in files:
                m5.sudo("cp", "-a", str(backup / name), str(state / "rootfs/etc" / name))
        m5.sudo("rm", "-rf", str(state / "home" / m3.TEST_USER), str(backup))
        m5.sudo("rmdir", f"/run/media/{m3.TEST_USER}")
        gone = m5.run(["getent", "passwd", m3.TEST_USER]).stdout.strip() == ""
        guest_gone = m5.sudo("grep", "-c", m3.TEST_USER, str(state / "rootfs/etc/passwd")).stdout.strip() == "0"
        restored = m3.TEST_USER not in m5.sudo("cat", str(info_path)).stdout
        check(f"{distro}: cleanup: throwaway user, guest account and info.json restored", gone and guest_gone and restored)
    m5.start_space()


def install_extras(distro: str, spec: dict) -> bool:
    marker = f"/var/lib/spaces/{distro}/rootfs/var/lib/spaces-m6-extras"
    if m5.sudo("test", "-f", marker).returncode == 0:
        skip(f"{distro}: install GTK app and Vulkan/GL tools", "already installed")
        return True
    done = guest_root(f"{spec['env']} {spec['install']} 2>&1 | tail -5", timeout=3600)
    ok = guest_user("command -v gnome-calculator vulkaninfo glxinfo >/dev/null").returncode == 0
    check(f"{distro}: guest package manager installs gnome-calculator, vulkan-tools, GL tools", ok,
          done.stdout.strip().replace("\n", " | ")[-120:])
    if ok:
        m5.sudo("sh", "-c", f"touch {marker}")
    return ok


def check_window(distro: str, extras: bool) -> None:
    if not m5.graphical():
        skip(f"{distro}: GUI app window on niri", "no graphical session")
        return
    if not extras:
        skip(f"{distro}: GUI app window on niri", "extras not installed")
        return
    proc = subprocess.Popen(
        ["spaces", "enter", "--graphical", distro, "--", "gnome-calculator"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        def calc() -> bool:
            return any("calculator" in ((w.get("app_id") or "") + (w.get("title") or "")).lower()
                       for w in m5.niri_windows())

        shown = m5.wait_for(calc, 45)
        windows = [(w.get("app_id"), w.get("title")) for w in m5.niri_windows()
                   if "calculator" in ((w.get("app_id") or "") + (w.get("title") or "")).lower()]
        check(f"{distro}: gnome-calculator window appears in niri", shown, str(windows)[:80])
    finally:
        proc.terminate()  # stops the guest unit too (see LxcBackend.spawn_in_guest)
        m5.guest("pkill -f gnome-calculator || kill $(pidof gnome-calculator) 2>/dev/null; true")
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    check(f"{distro}: the window closes", m5.wait_for(lambda: not any(
        "calculator" in ((w.get("app_id") or "") + (w.get("title") or "")).lower() for w in m5.niri_windows()), 10))


def glibc_of(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\s*$", text.strip())
    return (int(match[1]), int(match[2])) if match else None


def check_nvidia(distro: str, spec: dict, extras: bool) -> None:
    if not Path("/dev/nvidiactl").exists() or not Path("/var/lib/spaces/.host/nvidia/current").exists():
        skip(f"{distro}: NVIDIA userspace", "no NVIDIA driver or farm on this host")
        return
    done = m5.guest("nvidia-smi -L")
    check(f"{distro}: nvidia-smi lists the GPU through the NVIDIA farm",
          "NVIDIA" in done.stdout and "GPU 0" in done.stdout, done.stdout.strip()[:70])
    libdir = spec["libdir"]
    names = ("libcuda.so.1", "libGLX_nvidia.so.0", "libnvidia-ml.so.1", "libEGL_nvidia.so.0")
    done = m5.guest(" ; ".join(f"test -s {libdir}/{n} && echo ok:{n}" for n in names))
    check(f"{distro}: NVIDIA libraries are present in {libdir}", all(f"ok:{n}" in done.stdout for n in names),
          done.stdout.strip().replace("\n", " ")[-70:])
    done = m5.guest("test -s /usr/share/vulkan/icd.d/nvidia_icd.json && test -s /usr/share/glvnd/egl_vendor.d/10_nvidia.json && echo ok")
    check(f"{distro}: Vulkan and EGL vendor files are visible", done.stdout.strip() == "ok", done.stdout.strip()[:60])
    glibc_text = m5.guest("ldd --version | head -1").stdout.strip()
    guest_glibc = glibc_of(glibc_text)
    report(f"{distro}: guest glibc", "PASS" if guest_glibc else "FAIL", glibc_text[-40:])
    done = m5.guest(f"for f in {libdir}/libnvidia-egl-wayland2.so* {libdir}/libGLX_nvidia.so.0 {libdir}/libcuda.so.1 "
                    f"{libdir}/libnvidia-glcore.so*; do [ -s \"$f\" ] && ldd \"$f\" 2>&1 | grep -E 'not found|version .GLIBC' ; done; echo END")
    missing = [l.strip() for l in done.stdout.splitlines() if l.strip() and l.strip() != "END"]
    check(f"{distro}: NVIDIA libraries resolve their dependencies in the guest (ldd)", not missing and "END" in done.stdout,
          "; ".join(missing)[:140])
    if not extras:
        skip(f"{distro}: vulkaninfo / EGL hardware rendering", "extras not installed")
        return
    out = m5.guest("vulkaninfo --summary 2>&1").stdout
    check(f"{distro}: vulkaninfo lists the NVIDIA GPU", "driverName         = NVIDIA" in out,
          (re.findall(r"deviceName\s*=\s*(.*)", out) or ["?"])[0][:50])
    check(f"{distro}: vulkaninfo lists the AMD (radv) GPU", "radv" in out.lower())
    for node, expected in (("/dev/dri/renderD129", None), ("/dev/dri/renderD128", "NVIDIA")):
        if not Path(node).exists():
            continue
        if m5.guest("command -v python3 >/dev/null").returncode != 0:
            skip(f"{distro}: EGL on {node}", "no python3 in the guest")
            continue
        done = m5.guest_python(m5.GBM_EGL, node)
        text = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stdout
        hardware = done.returncode == 0 and "llvmpipe" not in text and "softpipe" not in text
        check(f"{distro}: EGL on {node} renders in hardware", hardware and (expected is None or expected in text), text[:70])
    if m5.graphical():
        out = m5.guest_graphical("glxinfo -B 2>&1").stdout
        check(f"{distro}: glxinfo (XWayland) direct rendering, not llvmpipe",
              "direct rendering: Yes" in out and "llvmpipe" not in out,
              (re.findall(r"OpenGL renderer string: (.*)", out) or [out[:50]])[0][:60])
        out = m5.guest_graphical("__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia glxinfo -B 2>&1").stdout
        check(f"{distro}: PRIME offload selects the NVIDIA GL driver", "OpenGL vendor string: NVIDIA" in out)


def forwarding_warnings(distro: str) -> int:
    done = m5.sudo("grep", "-c", "Could not enable host session forwarding", f"/var/log/spaces/{distro}/current")
    return int(done.stdout.strip() or 0) if done.stdout.strip().isdigit() else 0


def check_clean(distro: str) -> None:
    m5.sudo("sv", "-w", "90", "down", m5.SERVICE)
    time.sleep(1)
    mounts = Path("/proc/mounts").read_text()
    leftovers = [l.split()[1] for l in mounts.splitlines() if f"/var/lib/spaces/{distro}/" in l or f"/run/spaces/lxc/{distro}" in l]
    check(f"{distro}: stopping leaves no mounts, cgroup or container",
          not leftovers and not Path(f"/sys/fs/cgroup/spaces/{distro}").exists() and m5.cgroup_clean(),
          str(leftovers[:2]))


def run_distro(distro: str, args: argparse.Namespace) -> None:
    spec = DISTROS[distro]
    print(f"\n== {distro} ==", flush=True)
    use(distro)
    if not exists(distro):
        if args.no_create:
            report(f"{distro}: space exists", "FAIL", "not created (--no-create)")
            return
        create(distro)
        if not exists(distro):
            return
    else:
        skip(f"{distro}: spaces create", "space already exists")
    report(f"{distro}: disk used (state, cache)", "PASS", disk_used(distro))
    baseline = forwarding_warnings(distro)
    if not check_basics(distro, spec):
        return
    check_sudo_bridge(distro)
    extras = False
    if not args.no_extras:
        extras = install_extras(distro, spec)
    check_window(distro, extras)
    check_nvidia(distro, spec, extras)
    new = forwarding_warnings(distro) - baseline
    check(f"{distro}: desktop session forwarding worked during this run (launcher log)", new == 0, f"{new} warnings")
    check_clean(distro)


# ---------------------------------------------------------------- watchdog

PROBE = r'''
import errno, os, stat
def attempt(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        return errno.errorcode[e.errno]
    try:
        os.write(fd, b"V")   # magic close, in case it ever was a watchdog
    except OSError:
        pass
    os.close(fd)
    return "OPEN"
for name, major, minor in (("wd-misc", 10, 130), ("wd0", 246, 0), ("tty0", 4, 0), ("ttyS0", 4, 64),
                           ("vcs", 7, 0), ("ttyprintk", 5, 3), ("kmsg", 1, 11)):
    node = "/var/tmp/m6-" + name
    try:
        os.mknod(node, stat.S_IFCHR | 0o600, os.makedev(major, minor))
    except OSError as e:
        print("mknod", name, errno.errorcode[e.errno]); continue
    print("open", name, attempt(node)); os.unlink(node)
'''


def check_watchdog() -> None:
    """Level `full`: watchdog and VT/console majors stay denied (boot and live)."""

    print("\n== full device level keeps watchdogs denied ==", flush=True)
    use("ubuntu")
    if not exists("ubuntu"):
        skip("watchdog: full level", "no ubuntu space")
        return
    if not WATCHDOG_STATE.exists():
        skip("watchdog: full level", "no watchdog on this host")
        return
    armed: list[float] = []
    stop = threading.Event()

    def guard() -> None:
        while not stop.is_set():
            try:
                if WATCHDOG_STATE.read_text().strip() != "inactive":
                    armed.append(time.time())
                    m5.sudo("sh", "-c", "echo V > /dev/watchdog")
            except OSError:
                pass
            time.sleep(0.1)

    threading.Thread(target=guard, daemon=True).start()

    def probe() -> dict[str, str]:
        out = m5.guest_python(PROBE, root=True).stdout
        return {" ".join(l.split()[:2]): l.split()[2] for l in out.splitlines() if len(l.split()) == 3}

    def denied(res: dict[str, str]) -> bool:
        names = ("wd-misc", "wd0", "tty0", "ttyS0", "vcs", "ttyprintk")
        return all(res.get(f"mknod {n}") == "EPERM" or res.get(f"open {n}") == "EPERM" for n in names)

    try:
        m5.set_level("full")
        m5.start_space()
        conf = m5.sudo("cat", "/run/spaces/lxc/ubuntu/devices.conf").stdout
        check("watchdog: boot-time devices.conf denies 10:130 and the registered watchdog majors",
              "deny = c 10:130 rwm" in conf and "devices.allow = a" in conf, "")
        res = probe()
        check("watchdog: at full (boot) guest root cannot mknod/open watchdog, tty, vcs, ttyprintk", denied(res), str(res)[:100])
        check("watchdog: at full /dev/kmsg is still reachable (allow-all is otherwise intact)", res.get("open kmsg") == "OPEN")
        m5.set_level("basic")
        m5.start_space()
        code = "from spaces.host import lxc\nlxc.LxcBackend().set_device_policy('ubuntu','full',[])\n"
        done = m5.sudo("/usr/bin/python3", "-I", "-c", code)
        res = probe()
        check("watchdog: live update to full applies the same denies", done.returncode == 0 and denied(res), str(res)[:100])
        m5.set_level("admin")
        m5.start_space()
        res = probe()
        check("watchdog: admin level cannot open watchdog or VT devices", denied(res), str(res)[:100])
    finally:
        m5.set_level("basic")
        m5.start_space()
        stop.set()
        time.sleep(0.3)
        check("watchdog: the host watchdog was never armed", not armed and WATCHDOG_STATE.read_text().strip() == "inactive",
              f"state {WATCHDOG_STATE.read_text().strip()}, events {len(armed)}")
        info = json.loads(m5.sudo("cat", m5.INFO).stdout)
        check("watchdog: ubuntu is back at the basic device level", info["permissions"]["system"].get("devices") == "basic")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--distro", choices=sorted(DISTROS), action="append")
    parser.add_argument("--no-create", action="store_true")
    parser.add_argument("--no-extras", action="store_true")
    parser.add_argument("--watchdog", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0:
        print("run as the normal user", file=sys.stderr)
        return 2
    if m5.sudo("true").returncode != 0:
        print("passwordless sudo is required", file=sys.stderr)
        return 2
    for distro in args.distro or ["kali", "arch", "fedora"]:
        try:
            run_distro(distro, args)
        except Exception as error:  # keep going with the other distros
            report(f"{distro}: check run", "FAIL", repr(error)[:150])
    if args.watchdog:
        check_watchdog()
    failed = [label for label, status, _ in results if status == "FAIL"]
    passed = sum(status == "PASS" for _, status, _ in results)
    skipped = sum(status == "SKIP" for _, status, _ in results)
    print(f"\n{passed} passed, {len(failed)} failed, {skipped} skipped")
    for label in failed:
        print("FAILED:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
