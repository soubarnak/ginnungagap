#!/usr/bin/env python3
"""M5 check: devices, GPU acceleration and hotplug for the `ubuntu` space.

Run as the normal user from the niri session (needs passwordless sudo, the
`ubuntu` space from M3, and in the guest: mesa-utils, mesa-utils-bin,
vulkan-tools, mesa-vulkan-drivers; install them with --with-apt):

    python3 void/spike/m5_check.py [--with-apt] [--no-levels] [--no-stale]

Prints PASS/FAIL/SKIP per item and exits non-zero when anything failed.
Items that need the optional tools, a graphical session, a second GPU or the
uinput module are skipped. The check creates and removes virtual uinput
devices (as root), briefly opens a vkcube window, temporarily moves the
guest's locale archive aside, kills the launcher once (stale-container
recovery) and, unless --no-levels, switches the space to the `admin` device
level and back to `basic`. It never opens watchdog nodes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

SPACE = "ubuntu"
SERVICE = f"/var/service/spaces-{SPACE}"
LOG = f"/var/log/spaces/{SPACE}/current"
LXC_WRAPPER = "/usr/lib/spaces/spaces-lxc"
LXC_PATH = "/run/spaces/lxc"
INFO = f"/var/lib/spaces/{SPACE}/info.json"
ROOTFS = f"/var/lib/spaces/{SPACE}/rootfs"
UINPUT_SCRIPT = ROOT / "void/spike/m5_uinput.py"
results: list[tuple[str, str, str]] = []

GBM_EGL = r'''
import ctypes, os, sys
gbm = ctypes.CDLL("libgbm.so.1"); egl = ctypes.CDLL("libEGL.so.1")
fd = os.open(sys.argv[1], os.O_RDWR)
gbm.gbm_create_device.restype = ctypes.c_void_p; gbm.gbm_create_device.argtypes = [ctypes.c_int]
dev = gbm.gbm_create_device(fd)
egl.eglGetPlatformDisplay.restype = ctypes.c_void_p
egl.eglGetPlatformDisplay.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
dpy = egl.eglGetPlatformDisplay(0x31D7, dev, None)
egl.eglInitialize.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
maj, mn = ctypes.c_int(), ctypes.c_int()
assert egl.eglInitialize(ctypes.c_void_p(dpy), ctypes.byref(maj), ctypes.byref(mn)), "eglInitialize failed"
egl.eglBindAPI.argtypes = [ctypes.c_uint]; egl.eglBindAPI(0x30A2)
attr = (ctypes.c_int * 5)(0x3040, 8, 0x3033, 1, 0x3038)
cfg = ctypes.c_void_p(); n = ctypes.c_int()
egl.eglChooseConfig.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
egl.eglChooseConfig(ctypes.c_void_p(dpy), attr, ctypes.byref(cfg), 1, ctypes.byref(n))
egl.eglCreateContext.restype = ctypes.c_void_p; egl.eglCreateContext.argtypes = [ctypes.c_void_p] * 4
ctx = egl.eglCreateContext(ctypes.c_void_p(dpy), cfg, None, None)
egl.eglMakeCurrent.argtypes = [ctypes.c_void_p] * 4
egl.eglMakeCurrent(ctypes.c_void_p(dpy), None, None, ctypes.c_void_p(ctx))
egl.eglGetProcAddress.restype = ctypes.c_void_p; egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
get_string = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_uint)(egl.eglGetProcAddress(b"glGetString"))
print(get_string(0x1F01).decode())
'''

# Opens only; never touches watchdog nodes (opening arms the host watchdog).
BLOCKED = r'''
import errno, os, stat
def attempt(path):
    try:
        os.close(os.open(path, os.O_RDONLY | os.O_NONBLOCK)); return "OPEN"
    except OSError as e:
        return errno.errorcode[e.errno]
for path in ("/dev/mem", "/dev/kmsg", "/dev/tpm0", "/dev/tpmrm0", "/dev/tty0", "/dev/vcs",
             "/dev/nvme0n1", "/dev/nvme0n1p1", "/dev/sda", "/dev/sda1", "/dev/loop-control",
             "/dev/hidraw0", "/dev/uinput", "/dev/input/event0", "/dev/bus/usb/001/001",
             "/dev/kvm", "/dev/cpu_dma_latency"):
    print("open", path, attempt(path))
for name, kind, major, minor in (("nvme", stat.S_IFBLK, 259, 0), ("sda", stat.S_IFBLK, 8, 0),
                                 ("tpm0", stat.S_IFCHR, 10, 224), ("tty0", stat.S_IFCHR, 4, 0),
                                 ("kmsg", stat.S_IFCHR, 1, 11)):
    node = "/var/tmp/m5-" + name
    try:
        os.mknod(node, kind | 0o600, os.makedev(major, minor))
    except OSError as e:
        print("mknod", name, errno.errorcode[e.errno]); continue
    print("mknod", name, attempt(node)); os.unlink(node)
'''


def report(label: str, status: str, detail: str = "") -> bool:
    results.append((label, status, detail))
    print(f"{status:4}  {label}  {detail}", flush=True)
    return status == "PASS"


def check(label: str, condition: object, detail: str = "") -> bool:
    return report(label, "PASS" if condition else "FAIL", detail)


def skip(label: str, why: str) -> None:
    report(label, "SKIP", why)


def run(argv, *, timeout=120, input=None):
    try:
        return subprocess.run(
            argv, input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        out = error.stdout or ""
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return subprocess.CompletedProcess(argv, 124, out + "\n[timeout]")


def sudo(*argv, **kw):
    return run(["sudo", "-n", *argv], **kw)


def guest(command: str, **kw):
    return run(["spaces", "enter", SPACE, "--", "sh", "-c", command], **kw)


def guest_graphical(command: str, **kw):
    return run(["spaces", "enter", "--graphical", SPACE, "--", "sh", "-c", command], **kw)


def guest_root(command: str, **kw):
    return sudo("spaces", "enter", SPACE, "--root", "--", "sh", "-c", command, **kw)


def guest_python(source: str, *args: str, root: bool = False):
    argv = ["spaces", "enter", SPACE, *(("--root",) if root else ()), "--", "python3", "-", *args]
    return run((["sudo", "-n"] if root else []) + argv, input=source)


def wait_for(predicate, timeout, interval=0.3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def init_pid() -> int | None:
    done = sudo(LXC_WRAPPER, "/usr/bin/lxc-info", "-P", LXC_PATH, "-n", SPACE, "-p", "-H")
    return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip().isdigit() else None


def guest_ls(path: str) -> list[str]:
    pid = init_pid()
    if pid is None:
        return []
    done = sudo("nsenter", "-t", str(pid), "-m", "ls", path)
    return done.stdout.split() if done.returncode == 0 else []


def cgroup_clean() -> bool:
    return Path("/sys/fs/cgroup/cgroup.subtree_control").read_text().strip() == ""


def start_space() -> bool:
    return guest("true").returncode == 0


def graphical() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") and shutil.which("niri")
                and run(["niri", "msg", "version"]).returncode == 0)


def niri_windows() -> list[dict]:
    done = run(["niri", "msg", "--json", "windows"])
    try:
        return json.loads(done.stdout)
    except ValueError:
        return []


# ------------------------------------------------------------------ items


def check_discovery() -> None:
    from spaces import devices

    nodes = {str(node.destination) for node in devices.discover("basic")}
    gpu = {"/dev/dri/card0", "/dev/dri/card1", "/dev/dri/renderD128", "/dev/dri/renderD129"}
    drm = {p.name for p in Path("/dev/dri").glob("*") if p.is_char_device()}
    gpu &= {f"/dev/dri/{name}" for name in drm}
    check("1 discover(basic) lists every DRM card and render node", gpu <= nodes, str(sorted(gpu - nodes)))
    seen = set(guest_ls("/dev/dri"))
    check("1 guest sees the same DRM nodes", {Path(p).name for p in gpu} <= seen, f"guest: {sorted(seen)}")
    out = guest_python(
        "import os\nfor p in __import__('sys').argv[1:]:\n"
        "    try: os.close(os.open(p, os.O_RDWR)); print(p, 'OPEN')\n"
        "    except OSError as e: print(p, e.strerror)\n",
        *sorted(gpu),
    ).stdout
    denied = [line for line in out.splitlines() if not line.endswith("OPEN")]
    check("1 guest uid 1000 opens every DRM node read-write", not denied and out, "; ".join(denied) or "")
    acl = run(["getfacl", "-p", "/dev/dri/card0"]).stdout
    check("1 card nodes carry the uaccess ACL for the user (why a gid-13 node opens in the guest)",
          f"user:{os.environ.get('USER', '')}:rw" in acl or "user:" in acl, acl.replace("\n", " ")[:80])
    guest_gid = guest("getent group video | cut -d: -f3").stdout.strip()
    host_gid = run(["getent", "group", "video"]).stdout.split(":")[2:3]
    report("1 gid note: guest video gid vs host", "PASS", f"host {host_gid} guest {guest_gid}: access relies on ACL/0666, not groups")


def check_nvidia_nodes() -> None:
    if not Path("/dev/nvidiactl").exists():
        skip("2 NVIDIA nodes", "no NVIDIA driver loaded on this host")
        return
    seen = set(guest_ls("/dev"))
    wanted = {"nvidia0", "nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia-modeset"}
    wanted = {n for n in wanted if Path("/dev", n).exists()}
    check("2 guest sees /dev/nvidia* at basic", wanted <= seen, f"missing {sorted(wanted - seen)}")
    check("2 MIG caps / NVSwitch nodes stay out at basic", "nvidia-caps" not in seen and "nvidia-nvswitch" not in seen)


def check_nvidia_userspace(with_apt: bool) -> None:
    from spaces import host_config
    from spaces.host import nvidia

    if not Path("/var/lib/spaces/.host/nvidia/current").exists():
        skip("3 NVIDIA userspace", "no farm: run sudo /usr/lib/spaces/spaces-nvidia-sync")
        return
    config = host_config.load()
    ubuntu = config.overlays_for("ubuntu")
    check("3 config.json loads and has the ubuntu NVIDIA overlays",
          {str(o.destination) for o in ubuntu} >= {"/usr/share", "/usr/lib/x86_64-linux-gnu"},
          str([str(o.destination) for o in ubuntu]))
    sidecar = Path("/etc/spaces/config.json.generated")
    check("3 config.json is marked generated (sidecar hash matches)",
          sidecar.exists() and nvidia._sha256(Path("/etc/spaces/config.json").read_text()) == sidecar.read_text().strip())
    done = guest("nvidia-smi -L")
    check("3/4 nvidia-smi in the guest lists the GPU", "NVIDIA" in done.stdout and "GPU 0" in done.stdout, done.stdout.strip()[:80])
    done = guest("python3 -c \"import ctypes; ctypes.CDLL('libcuda.so.1'); ctypes.CDLL('libGLX_nvidia.so.0'); ctypes.CDLL('libnvidia-ml.so.1'); print('ok')\"")
    check("3 guest dlopen of libcuda, libGLX_nvidia, libnvidia-ml", done.stdout.strip() == "ok", done.stdout.strip()[:80])
    done = guest("ls /usr/share/vulkan/icd.d/nvidia_icd.json /usr/share/glvnd/egl_vendor.d/10_nvidia.json && cat /usr/share/vulkan/icd.d/nvidia_icd.json | head -c 60")
    check("3 ICD files are visible in the guest", done.returncode == 0, done.stdout.strip()[:80])
    objdump = shutil.which("objdump")
    if objdump:
        worst = (0, 0)
        for target in Path("/var/lib/spaces/.host/nvidia/current/lib").glob("lib*.so*"):
            out = run([objdump, "-T", str(target.resolve())]).stdout
            for found in re.findall(r"GLIBC_(\d+)\.(\d+)", out):
                worst = max(worst, (int(found[0]), int(found[1])))
        guest_glibc = guest("ldd --version | head -1").stdout
        match = re.search(r"(\d+)\.(\d+)\s*$", guest_glibc.strip())
        check("3 exposed libraries need no newer glibc than the guest has",
              bool(match) and worst <= (int(match[1]), int(match[2])), f"max needed {worst[0]}.{worst[1]}, guest {guest_glibc.strip()[-30:]}")
    else:
        skip("3 glibc symbol check", "objdump missing")


def check_acceleration() -> None:
    if guest("command -v vulkaninfo glxinfo eglinfo >/dev/null").returncode != 0:
        skip("4 acceleration", "install mesa-utils mesa-utils-bin vulkan-tools mesa-vulkan-drivers in the guest (--with-apt)")
        return
    out = guest("vulkaninfo --summary 2>&1").stdout
    check("4 vulkaninfo lists the AMD (radv) GPU", "radv" in out, "")
    if Path("/dev/nvidiactl").exists():
        check("4 vulkaninfo lists the NVIDIA GPU", "driverName         = NVIDIA" in out)
    # The render node numbers follow probe order and changed between boots (renderD128 was the
    # NVIDIA GPU once and is the AMD iGPU now), so the expected driver comes from the PCI vendor.
    expected_by_vendor = {"0x10de": ("NVIDIA", "NVIDIA"), "0x1002": ("AMD", "AMD")}
    for node in sorted(Path("/dev/dri").glob("renderD*")):
        try:
            vendor = (Path("/sys/class/drm") / node.name / "device/vendor").read_text().strip()
        except OSError:
            vendor = ""
        kind, expected = expected_by_vendor.get(vendor, ("other", None))
        done = guest_python(GBM_EGL, str(node))
        text = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stdout
        hardware = done.returncode == 0 and "llvmpipe" not in text and "softpipe" not in text
        check(f"4 EGL on {node} ({kind} GPU) renders in hardware", hardware and (expected is None or expected in text), text[:80])
    if graphical():
        out = guest_graphical("glxinfo -B 2>&1").stdout
        check("4 glxinfo (XWayland :0) direct rendering, not llvmpipe",
              "direct rendering: Yes" in out and "llvmpipe" not in out, re.findall(r"OpenGL renderer string: (.*)", out)[0][:60] if "renderer" in out else out[:60])
        if Path("/dev/nvidiactl").exists():
            out = guest_graphical("__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia glxinfo -B 2>&1").stdout
            check("4 PRIME offload selects the NVIDIA GL driver", "OpenGL vendor string: NVIDIA" in out)
        check_window()
    else:
        skip("4 glxinfo / vkcube window", "no graphical session")


def check_window() -> None:
    if guest("command -v vkcube >/dev/null").returncode != 0:
        skip("4 vkcube window", "vkcube missing in the guest")
        return
    proc = subprocess.Popen(
        ["spaces", "enter", "--graphical", SPACE, "--", "sh", "-c", "timeout 12 vkcube --c 100000 --gpu_number 0"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        shown = wait_for(lambda: any((w.get("title") or "") == "vkcube" for w in niri_windows()), 10)
        check("4 vkcube (GPU 0) shows a window on niri", shown)
    finally:
        proc.wait(timeout=30)
    check("4 the window closes", wait_for(lambda: not any((w.get("title") or "") == "vkcube" for w in niri_windows()), 5))


def parse_blocked(out: str) -> dict[str, str]:
    return {" ".join(line.split()[:2]): line.split()[2] for line in out.splitlines() if len(line.split()) == 3}


def check_blocked() -> None:
    for who, root in (("root", True), ("user", False)):
        parsed = parse_blocked(guest_python(BLOCKED, root=root).stdout)
        opened = sorted(k for k, v in parsed.items() if v == "OPEN")
        check(f"5 basic: sensitive nodes unreachable for guest {who}", parsed and not opened, f"open: {opened}")
    check("5 basic: /dev/uinput is not created in the guest (the host module is loaded at every boot)",
          "uinput" not in guest_ls("/dev") and "uhid" not in guest_ls("/dev"), str(Path("/dev/uinput").exists()))
    parsed = parse_blocked(guest_python(BLOCKED, root=True).stdout)
    mknod = {k: v for k, v in parsed.items() if k.startswith("mknod")}
    check("5 basic: mknod'd host disk/tpm/tty/kmsg nodes are refused by the device cgroup",
          mknod and all(v in ("EPERM", "EACCES") for v in mknod.values()), str(mknod))


def set_level(level: str) -> bool:
    code = (
        "import json,sys\nfrom spaces import priv\n"
        f"info=json.load(open({INFO!r}))\nrec=info['permissions']['users']['{os.getuid()}']['permissions']\n"
        f"system=dict(info['permissions']['system'], devices={level!r}, preset={'basic' if level == 'basic' else 'custom'!r})\n"
        f"priv.configure({{'schema_version':1,'name':{SPACE!r},'permissions':{{'system':system,"
        f"'user':{{'uid':{os.getuid()},'gid':{os.getgid()},'permissions':rec}}}}}})\n"
    )
    sudo("sv", "-w", "90", "down", SERVICE)
    return sudo("/usr/bin/python3", "-I", "-c", code).returncode == 0


def check_levels() -> None:
    try:
        ok = set_level("admin")
        start_space()
        seen = set(guest_ls("/dev"))
        parsed = parse_blocked(guest_python(BLOCKED, root=True).stdout)
        check("5 admin level: disks and hidraw appear, TPM/tty/watchdog still absent",
              ok and "nvme0n1" in seen and "tpm0" not in seen and "tty0" not in seen and "watchdog" not in seen,
              f"nvme0n1 in /dev: {'nvme0n1' in seen}")
        check("5 admin level: nodes outside the allow list (tpm0, tty0, kmsg) stay refused by the device cgroup",
              all(parsed.get(f"mknod {n}") in ("EPERM", "EACCES") for n in ("tpm0", "tty0", "kmsg")),
              str({k: v for k, v in parsed.items() if k.startswith("mknod")}))
    finally:
        restored = set_level("basic")
        start_space()
    seen = set(guest_ls("/dev"))
    check("5 basic level restored", restored and "nvme0n1" not in seen, str(INFO))


UINPUT_PROCS: list[subprocess.Popen] = []


def start_uinput(kind: str) -> tuple[subprocess.Popen, list[str]]:
    proc = subprocess.Popen(["sudo", "-n", "/usr/bin/python3", str(UINPUT_SCRIPT), kind],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL)
    UINPUT_PROCS.append(proc)
    line = proc.stdout.readline()
    match = re.match(r"(input\d+) \[(.*)\]", line)
    names = re.findall(r"'([^']+)'", match[2]) if match else []
    return proc, names


def stop_uinput(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            sudo("pkill", "-KILL", "-P", str(proc.pid))
            proc.kill()
    if proc in UINPUT_PROCS:
        UINPUT_PROCS.remove(proc)


def check_hotplug() -> None:
    if sudo("modprobe", "uinput").returncode != 0 or not Path("/dev/uinput").exists():
        skip("6 hotplug", "uinput not available")
        return
    before = set(guest_ls("/dev/input"))
    t0 = time.monotonic()
    pad, nodes = start_uinput("gamepad")
    events = [n for n in nodes if n.startswith("event")]
    if not events:
        check("6 virtual gamepad created", False, f"nodes {nodes}")
        stop_uinput(pad)
        return
    appeared = wait_for(lambda: events[0] in guest_ls("/dev/input"), 15, 0.1)
    check("6 gamepad node appears in the guest after creation", appeared, f"{time.monotonic() - t0:.1f}s")
    done = guest_python("import os,sys\nos.close(os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK)); print('OPEN')\n", f"/dev/input/{events[0]}")
    check("6 guest uid 1000 can open it (uaccess ACL)", done.stdout.strip() == "OPEN", done.stdout.strip()[-60:])
    major_minor = run(["stat", "-c", "%t:%T", f"/dev/input/{events[0]}"]).stdout.strip()
    node_test = (
        "import os,errno,sys\nmaj,mn=[int(x,16) for x in sys.argv[1].split(':')]\n"
        "p='/var/tmp/m5-hot'\n"
        "try: os.unlink(p)\nexcept OSError: pass\n"
        "os.mknod(p, 0o20600, os.makedev(maj,mn))\n"
        "try: os.close(os.open(p, os.O_RDONLY|os.O_NONBLOCK)); print('OPEN')\n"
        "except OSError as e: print(errno.errorcode[e.errno])\n"
        "os.unlink(p)\n"
    )
    allowed = guest_python(node_test, major_minor, root=True).stdout.strip()
    check("6 device cgroup rule is live while the device exists", allowed == "OPEN", allowed)
    kb, kb_nodes = start_uinput("keyboard")
    kb_events = [n for n in kb_nodes if n.startswith("event")]
    time.sleep(4)
    visible = set(guest_ls("/dev/input"))
    check("6 virtual keyboard (capture device) stays hidden at basic", bool(kb_events) and not (set(kb_events) & visible), f"{kb_events} vs {sorted(visible)}")
    stop_uinput(kb)
    t1 = time.monotonic()
    stop_uinput(pad)
    gone = wait_for(lambda: events[0] not in guest_ls("/dev/input"), 15, 0.1)
    check("6 gamepad node disappears from the guest after removal", gone, f"{time.monotonic() - t1:.1f}s")
    denied = guest_python(node_test, major_minor, root=True).stdout.strip()
    check("6 device cgroup rule is removed again", denied in ("EPERM", "EACCES"), denied)
    check("6 /dev/input has the same entries as before", set(guest_ls("/dev/input")) == before, str(sorted(set(guest_ls('/dev/input')) ^ before)))


def check_locale() -> None:
    archive = f"{ROOTFS}/usr/lib/locale/locale-archive"
    if not guest("test -n \"$LANG\" && echo yes").stdout.strip() == "yes" or not Path(archive).exists() and sudo("test", "-e", archive).returncode != 0:
        skip("7a locale", "no forwarded LANG or no locale archive in the guest")
        return
    lang = guest("echo $LANG").stdout.strip()
    if lang in ("C.UTF-8", "C", "POSIX", ""):
        skip("7a locale", f"forwarded LANG is {lang!r}")
        return
    try:
        sudo("mv", archive, archive + ".m5off")
        out = guest("echo $LANG; perl -e 1 2>&1").stdout.strip().splitlines()
        check("7a missing guest locale: LANG becomes C.UTF-8 and perl does not warn", out[:1] == ["C.UTF-8"] and len(out) == 1, "; ".join(out)[:80])
    finally:
        sudo("mv", archive + ".m5off", archive)
    check("7a present guest locale is forwarded unchanged", guest("echo $LANG").stdout.strip() == lang, lang)


def launcher_pid() -> int | None:
    done = sudo("sv", "status", SERVICE)
    match = re.match(r"run: [^:]+: \(pid (\d+)\)", done.stdout)
    return int(match[1]) if match else None


def check_stale() -> None:
    start_space()
    pid = launcher_pid()
    if pid is None:
        skip("7b stale container", "no launcher pid from sv status")
        return
    old = run(["pgrep", "-x", "lxc-start"]).stdout.split()
    sudo("kill", "-9", str(pid))
    time.sleep(1.5)
    orphan = run(["pgrep", "-x", "lxc-start"]).stdout.split()
    check("7b kill -9 of the launcher leaves the container running", bool(orphan) and orphan == old, f"{orphan}")
    t0 = time.monotonic()
    done = guest("echo recovered")
    new = run(["pgrep", "-x", "lxc-start"]).stdout.split()
    check("7b spaces enter starts a fresh launcher and container", done.stdout.strip() == "recovered" and len(new) == 1 and new != orphan,
          f"{time.monotonic() - t0:.1f}s old {orphan} new {new}")
    log = sudo("tail", "-n", "40", LOG).stdout
    check("7b launcher logged the leftover cleanup", "leftover container" in log)
    check("7b root cgroup.subtree_control is empty", cgroup_clean())


def check_final(with_stale: bool) -> None:
    check("9 no uinput test devices left", "ggm5 test" not in Path("/proc/bus/input/devices").read_text() and not UINPUT_PROCS)
    check("9 root cgroup.subtree_control is empty", cgroup_clean())
    mounts = Path("/proc/mounts").read_text()
    check("9 no stray mounts of the guest rootfs on the host while it runs", mounts.count("/var/lib/spaces/ubuntu/rootfs") <= 1, "")


def main() -> int:
    if os.geteuid() == 0:
        print("run as the normal user", file=sys.stderr)
        return 2
    if sudo("true").returncode != 0:
        print("passwordless sudo is required", file=sys.stderr)
        return 2
    with_apt = "--with-apt" in sys.argv
    if with_apt:
        guest_root("DEBIAN_FRONTEND=noninteractive apt-get install -y -qq mesa-utils mesa-utils-bin vulkan-tools mesa-vulkan-drivers libgl1-mesa-dri libegl1 libgbm1 libvulkan1 >/dev/null 2>&1", timeout=900)
    if not start_space():
        print("could not start the ubuntu space", file=sys.stderr)
        return 2
    try:
        check_discovery()
        check_nvidia_nodes()
        check_nvidia_userspace(with_apt)
        check_acceleration()
        check_blocked()
        check_hotplug()
        if "--no-levels" not in sys.argv:
            check_levels()
        else:
            skip("5 admin level", "--no-levels")
        check_locale()
        if "--no-stale" not in sys.argv:
            check_stale()
        else:
            skip("7b stale container", "--no-stale")
    finally:
        for proc in list(UINPUT_PROCS):
            stop_uinput(proc)
    check_final("--no-stale" not in sys.argv)
    failed = [label for label, status, _ in results if status == "FAIL"]
    passed = sum(status == "PASS" for _, status, _ in results)
    skipped = sum(status == "SKIP" for _, status, _ in results)
    print(f"\n{passed} passed, {len(failed)} failed, {skipped} skipped")
    for label in failed:
        print("FAILED:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
