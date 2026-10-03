#!/usr/bin/env python3
"""M3 end-to-end check: dev install, create and enter on this Void machine.

Run as the normal user (not root) after void/tools/dev-install.sh and after
`sudo spaces create ubuntu --preset basic`:

    python3 void/spike/m3_check.py

It needs passwordless sudo. It stops and starts the `ubuntu` space, creates a
throwaway host user (uid 59990, "spacestest") with a random password to test
the host-PAM bridge and login-scoped mounts, and removes it again. Prints
PASS/FAIL per item and exits non-zero when anything failed. Every command
output goes through pipes: lxc-attach changes the ownership of regular-file
stdio, so never run `spaces enter` with stdout redirected to a file you care
about without the backend's restore logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPACE = "ubuntu"
STATE = Path("/var/lib/spaces") / SPACE
SERVICE = f"/var/service/spaces-{SPACE}"
RUNTIME = Path("/run/spaces/lxc") / SPACE
WRAPPER = "/usr/lib/spaces/spaces-lxc"
GUEST_KEYRING = Path("/usr/share/keyrings/ubuntu-archive-keyring.gpg")
KEYRING_FPR = "F6ECB3762474EDA9D21B7022871920D1991BC93C"
TEST_USER = "spacestest"
TEST_UID = 59990
BACKUP = Path("/run/spaces-m3check")
ME = pwd.getpwuid(os.getuid()).pw_name
results: list[tuple[str, bool, str]] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    results.append((label, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {label}  {detail}", flush=True)
    return bool(condition)


def run(
    argv: list[str],
    *,
    input: str | None = None,
    timeout: float = 120,
    detach: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command with every stream on a pipe."""

    try:
        return subprocess.run(
            argv,
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            start_new_session=detach,
        )
    except subprocess.TimeoutExpired as error:
        out = error.stdout or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return subprocess.CompletedProcess(argv, 124, out + "\n[timeout]")


def sudo(*argv: str, **kw: object) -> subprocess.CompletedProcess[str]:
    return run(["sudo", "-n", *argv], **kw)  # type: ignore[arg-type]


def sudo_python(code: str, **kw: object) -> subprocess.CompletedProcess[str]:
    return sudo("/usr/bin/python3", "-I", "-c", code, **kw)


def lxc_state() -> str | None:
    done = sudo(WRAPPER, "lxc-info", "-P", "/run/spaces/lxc", "-n", SPACE, "-s", "-H")
    return done.stdout.strip() if done.returncode == 0 else None


def wait_for(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def marker_present() -> bool:
    return sudo("test", "-f", str(RUNTIME / "ready")).returncode == 0


def stop_space() -> bool:
    sudo("sv", "down", SERVICE)
    return wait_for(lambda: lxc_state() in (None, "STOPPED"), 90)


def guest_root(command: str, *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run a root shell command in the guest (starts the space if needed)."""

    return sudo(
        "/usr/bin/spaces.priv",
        "enter-as-user",
        "root",
        SPACE,
        "--",
        "sh",
        "-c",
        command,
        timeout=timeout,
    )


def subtree_control() -> str:
    return Path("/sys/fs/cgroup/cgroup.subtree_control").read_text().strip()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owner_ok(path: str, *, mode_max: int = 0o755) -> bool:
    status = os.stat(path)
    return status.st_uid == 0 and status.st_gid == 0 and not status.st_mode & 0o022 and (
        status.st_mode & 0o7777
    ) <= mode_max


# ------------------------------------------------------------------ sections


def check_install() -> None:
    print("\n== install ==", flush=True)
    files = [
        "/usr/bin/spaces",
        "/usr/bin/spaces.priv",
        "/usr/lib/spaces/spaces-lxc",
        "/usr/lib/spaces/spaces-pam",
        "/usr/lib/spaces/spaces-broker",
        "/usr/lib/spaces/spaces-system-broker",
        *(
            f"/usr/lib/spaces/guest/{n}"
            for n in (
                "pam_spaces.so",
                "spaces",
                "spaces-portal",
                "spaces-secret-helper",
                "spaces-open",
                "spaces-system-broker",
            )
        ),
        "/etc/apparmor.d/spaces-container",
        "/etc/pam.d/spaces",
        "/etc/spaces/config.json",
        "/usr/share/polkit-1/actions/org.anatase.spaces.policy",
        "/usr/share/spaces/systemd/run-spaces-proc.mount",
        "/usr/share/spaces/systemd/local-fs-spaces-proc.conf",
        "/usr/share/spaces/pam/spaces.ubuntu",
        str(GUEST_KEYRING),
    ]
    missing = [f for f in files if not os.path.exists(f)]
    check("install: all expected files exist", not missing, ",".join(missing))
    bad = [f for f in files if os.path.exists(f) and not owner_ok(f)]
    check("install: files are root-owned and not group/world-writable", not bad, ",".join(bad))
    check(
        "install: /usr/lib/spaces/void/bin shim directory exists",
        os.path.isdir("/usr/lib/spaces/void/bin") and owner_ok("/usr/lib/spaces/void/bin"),
    )
    wrapper = Path("/usr/bin/spaces.priv").read_text()
    check(
        "install: spaces.priv is an isolated-python wrapper with the shim PATH",
        "python3 -I -c" in wrapper
        and "from spaces.priv import main" in wrapper
        and "PATH=/usr/lib/spaces/void/bin:" in wrapper
        and wrapper.startswith("#!/bin/sh"),
    )
    policy = Path("/usr/share/polkit-1/actions/org.anatase.spaces.policy").read_text()
    paths = set(re.findall(r'exec\.path">([^<]+)<', policy))
    check(
        "install: polkit exec.path matches the installed wrapper",
        paths == {"/usr/bin/spaces.priv"} and os.access("/usr/bin/spaces.priv", os.X_OK),
        str(paths),
    )
    # Source freshness: the installed package equals the checkout's.
    site = Path(
        run(["/usr/bin/python3", "-I", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"]).stdout.strip()
    )
    stale = [
        str(p.relative_to(ROOT / "src"))
        for p in (ROOT / "src" / "spaces").rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and (not (site / p.relative_to(ROOT / "src")).is_file() or sha(p) != sha(site / p.relative_to(ROOT / "src")))
    ]
    check("install: site-packages/spaces matches the checkout", not stale, ",".join(stale[:5]))
    owned = run(["find", str(site / "spaces"), "!", "-user", "root"]).stdout.strip()
    check("install: site-packages/spaces is entirely root-owned", owned == "", owned[:200])
    refs = run(
        [
            "grep",
            "-rIl",
            "--exclude=*.pyc",
            str(ROOT),
            "/usr/bin/spaces",
            "/usr/bin/spaces.priv",
            "/usr/lib/spaces",
            "/usr/share/spaces",
            str(site / "spaces"),
            "/etc/apparmor.d/spaces-container",
            "/etc/pam.d/spaces",
            "/etc/spaces",
            *sorted(str(p) for p in Path("/etc/sv").glob("spaces-*")),
        ]
    ).stdout.strip()
    check("install: nothing installed points back into the checkout", refs == "", refs[:200])
    check(
        "install: root imports spaces from site-packages",
        sudo_python("import spaces,sys;print(spaces.__file__)").stdout.strip().startswith(str(site)),
    )
    elf_ok = all(
        Path(f"/usr/lib/spaces/guest/{n}").read_bytes()[:4] == b"\x7fELF"
        for n in ("pam_spaces.so", "spaces", "spaces-portal")
    )
    check("install: guest native binaries are ELF", elf_ok)
    done = run(
        [
            "python3",
            str(ROOT / "native" / "check_guest_abi.py"),
            "--target",
            "x86_64",
            "--glibc-max",
            "2.17",
            "--guest",
            *(f"/usr/lib/spaces/guest/{n}" for n in ("pam_spaces.so", "spaces", "spaces-portal", "spaces-secret-helper", "spaces-open", "spaces-system-broker")),
            "--host",
            "/usr/lib/spaces/spaces-pam",
            "/usr/lib/spaces/spaces-broker",
        ]
    )
    check("install: installed binaries pass check_guest_abi (glibc <= 2.17)", done.returncode == 0, done.stdout.strip()[-120:])
    fingerprints = run(["gpg", "--batch", "--show-keys", "--with-colons", str(GUEST_KEYRING)]).stdout
    found = [line.split(":")[9] for line in fingerprints.splitlines() if line.startswith("fpr:")]
    check("keyring: Ubuntu archive key (2018) fingerprint present", KEYRING_FPR in found, " ".join(found))
    mount_unit = Path("/usr/share/spaces/systemd/run-spaces-proc.mount").read_text()
    check("install: run-spaces-proc.mount conditional on container", "ConditionVirtualization=container" in mount_unit)
    config = run(
        [
            "python3",
            "-I",
            "-c",
            "import logging;from spaces import host_config;r=[];"
            "h=type('H',(logging.Handler,),{'emit':lambda s,x:r.append(x.getMessage())})();"
            "logging.getLogger().addHandler(h);c=host_config.load();"
            "print(c.version,sorted(c.distros),c.packages_for('ubuntu'),r)",
        ]
    ).stdout.strip()
    check("install: /etc/spaces/config.json accepted without warnings", config.endswith("[]") and config.startswith("1 "), config)
    nvidia = Path("/etc/spaces/config.json").read_text()
    check("install: config has no nvidia mounts or overlays", "nvidia" not in nvidia.lower() and "mounts" not in nvidia)


def check_polkit() -> None:
    print("\n== polkit ==", flush=True)
    expected = {
        "org.anatase.spaces.enter": 0,
        "org.anatase.spaces.start": 0,
        "org.anatase.spaces.create": 2,
        "org.anatase.spaces.configure": 2,
        "org.anatase.spaces.delete": 2,
        "org.anatase.spaces.cp": 2,
        "org.anatase.spaces.enter-as-user": 2,
    }
    # pkcheck exit codes: 0 authorized, 2 challenge (authentication needed).
    outcome = {a: run(["pkcheck", "--action-id", a, "--process", str(os.getpid())]).returncode for a in expected}
    check("polkit: enter/start allowed without a password", outcome["org.anatase.spaces.enter"] == 0 and outcome["org.anatase.spaces.start"] == 0, str(outcome))
    admin = all(outcome[a] == 2 for a in expected if expected[a] == 2)
    check("polkit: create/configure/delete/cp/enter-as-user require admin authentication", admin, str(outcome))
    info = json.loads(sudo("cat", str(STATE / "info.json")).stdout)
    check("space: info.json lists the invoking user", str(os.getuid()) in info["permissions"]["users"], str(info["distribution"]))
    os_release = sudo("cat", str(STATE / "rootfs/etc/os-release")).stdout
    check("space: rootfs is Ubuntu", "ID=ubuntu" in os_release, re.search(r'VERSION_ID="?([^"\n]+)', os_release).group(1) if "VERSION_ID" in os_release else "")


def check_enter() -> None:
    print("\n== enter ==", flush=True)
    check("cgroup: root subtree_control empty before", subtree_control() == "", repr(subtree_control()))
    check("stop: space stops via sv down", stop_space() and not marker_present(), str(lxc_state()))
    started = time.monotonic()
    # No controlling terminal and no stdin: a password prompt cannot be answered.
    done = run(["spaces", "enter", SPACE, "--", "id"], detach=True)
    elapsed = time.monotonic() - started
    check(
        "enter: `spaces enter ubuntu -- id` autostarts, no prompt, runs as the invoking user",
        done.returncode == 0 and f"uid={os.getuid()}({ME})" in done.stdout,
        f"rc={done.returncode} {elapsed:.1f}s {done.stdout.strip()[:100]}",
    )
    check("enter: ready marker written and guest running", lxc_state() == "RUNNING" and marker_present())
    done = run(["spaces", "enter", SPACE, "--", "sh", "-c", "exit 7"], detach=True)
    check("enter: exit code propagates", done.returncode == 7, f"rc={done.returncode}")
    out = Path(tempfile.mkdtemp(prefix="m3out.")) / "out.txt"
    with open(out, "w") as handle:
        subprocess.run(["spaces", "enter", SPACE, "--", "id", "-un"], stdout=handle, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    status = out.stat()
    check(
        "enter: stdout redirected to a file stays owned by the user (mode 0644)",
        status.st_uid == os.getuid() and (status.st_mode & 0o777) == 0o644 and out.read_text().strip() == ME,
        f"uid={status.st_uid} mode={oct(status.st_mode & 0o777)}",
    )
    out.unlink()
    out.parent.rmdir()
    check("enter: guest environment is clean of host session variables", "DBUS_SESSION_BUS_ADDRESS=" not in run(["spaces", "enter", SPACE, "--", "env"], detach=True).stdout.replace("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user", "X"))
    # Interactive login shell on a pty.
    import pty

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("spaces", ["spaces", "enter", SPACE])
    collected = b""

    def drain(seconds: float) -> None:
        nonlocal collected
        import select

        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    return
                if not data:
                    return
                collected += data

    drain(4)
    os.write(fd, b"echo USER=$(id -un) SHELL=$0 TTY=$(tty) PWD=$PWD; exit 3\n")
    drain(3)
    try:
        _, wait_status = os.waitpid(pid, 0)
        code = os.WEXITSTATUS(wait_status)
    except ChildProcessError:
        code = -1
    text = collected.decode(errors="replace")
    check(
        "enter: interactive login shell on a pty, exit status propagates",
        f"USER={ME}" in text and "SHELL=-bash" in text and "TTY=/dev/pts/" in text and code == 3,
        f"exit={code} " + (re.search(r"USER=\S+ SHELL=\S+ TTY=\S+ PWD=\S+", text) or [""])[0],
    )


def check_runit() -> None:
    print("\n== runit service ==", flush=True)
    status = sudo("sv", "status", SERVICE).stdout
    check("runit: service is up, 'normally down'", status.startswith("run:") and "normally down" in status, status.strip()[:90])
    check("runit: permanent down file keeps runsv from restarting it", sudo("test", "-e", f"/etc/sv/spaces-{SPACE}/down").returncode == 0)
    match = re.search(r"run: [^:]+: \(pid (\d+)\)", status)
    environ = {}
    if match:
        raw = sudo("cat", f"/proc/{match.group(1)}/environ").stdout
        environ = dict(item.partition("=")[::2] for item in raw.split("\0") if "=" in item)
    check(
        "runit: launch environment (PATH shim first, HOME, LANG)",
        environ.get("PATH", "").startswith("/usr/lib/spaces/void/bin:") and environ.get("HOME") == "/root" and environ.get("LANG") == "C.UTF-8",
        json.dumps({k: environ.get(k) for k in ("PATH", "HOME", "LANG")}),
    )
    log = sudo("cat", f"/var/log/spaces/{SPACE}/current").stdout
    check("runit: launcher output reaches /var/log/spaces/NAME/current via svlogd", "mounted at space launch" in log, f"{len(log.splitlines())} lines")
    check("runit: ready marker present while running", marker_present())
    check("selinux: storage relabel code is inert (no /sys/fs/selinux)", not os.path.exists("/sys/fs/selinux/enforce"))
    modes = sudo("stat", "-c", "%U:%G %a", "/var/lib/spaces", str(STATE), "/var/cache/spaces", "/run/spaces").stdout.split("\n")
    check("state: /var/lib/spaces, space dir, cache and /run/spaces are root-owned 755", all(line.startswith("root:root") for line in modes if line), " | ".join(modes))
    guest = guest_root("systemctl is-system-running; /usr/bin/test -x /run/spaces-host/bin/pam_spaces.so && echo native-bound; ls /run/spaces-host")
    check("native: guest binaries bound at /run/spaces-host/bin", "native-bound" in guest.stdout, guest.stdout.replace("\n", " ")[:120])
    check("guest: systemd reaches 'running' (no AppArmor mount denials)", guest.stdout.splitlines()[0:1] == ["running"], guest.stdout.splitlines()[0] if guest.stdout else "")


def check_caller_exit() -> None:
    print("\n== caller exit terminates the guest unit ==", flush=True)

    def guest_sleep(marker: str) -> bool:
        return marker in guest_root("pgrep -a -x sleep || true").stdout

    # (1) Through pkexec exactly as `spaces enter --graphical` with a caller pid.
    shell = subprocess.Popen(
        [
            "sh",
            "-c",
            "pkexec /usr/bin/spaces.priv enter --caller-pid=$$ "
            f"{ME}@{SPACE} -- sleep 311; rc=$?; exit $rc",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # spaces.priv insists that --caller-pid is its parent: the sh, not pkexec.
    started = wait_for(lambda: guest_sleep("sleep 311"), 20, 1)
    shell.kill()
    shell.wait()
    gone = wait_for(lambda: not guest_sleep("sleep 311"), 20, 1)
    check("caller exit: pkexec enter --caller-pid stops the guest command", started and gone, f"started={started} gone={gone}")
    # (2) The plain (non-launcher) path with a real pidfd.
    code = (
        "import os,subprocess,threading,time\n"
        "from spaces import priv\n"
        "c=subprocess.Popen(['sleep','60']);fd=os.pidfd_open(c.pid)\n"
        "threading.Timer(3,c.kill).start()\n"
        f"rc=priv._machine_shell({ME!r},{SPACE!r},['sleep','312'],caller_pidfd=fd)\n"
        "print('rc',rc)\n"
    )
    done = sudo_python(code, timeout=60)
    still = guest_sleep("sleep 312")
    check("caller exit: _machine_shell(caller_pidfd) stops the guest unit", done.returncode == 0 and "rc 143" in done.stdout and not still, done.stdout.strip()[-60:] + f" still={still}")


def check_auth_and_mounts() -> None:
    print("\n== host-PAM bridge and login-scoped mounts (throwaway user) ==", flush=True)
    info_path = STATE / "info.json"
    password = secrets.token_urlsafe(14)
    holder: subprocess.Popen[str] | None = None
    state_files = ("passwd", "group", "shadow", "gshadow", "passwd-", "group-", "shadow-", "gshadow-")
    try:
        stop_space()
        sudo("rm", "-rf", str(BACKUP))
        sudo("install", "-d", "-m", "0700", str(BACKUP))
        for name in state_files:
            sudo("cp", "-a", str(STATE / "rootfs/etc" / name), str(BACKUP / name))
        sudo("cp", "-a", str(info_path), str(BACKUP / "info.json"))
        done = sudo("useradd", "-m", "-u", str(TEST_UID), "-U", "-s", "/bin/bash", TEST_USER)
        hashed = run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n").stdout.strip()
        done2 = sudo("usermod", "-p", hashed, TEST_USER)
        downloads = f"/home/{TEST_USER}/Downloads"
        sudo("-u", TEST_USER, "mkdir", "-p", downloads)
        sudo("-u", TEST_USER, "sh", "-c", f"echo hostfile > {downloads}/marker.txt")
        check("auth: throwaway host user created", done.returncode == 0 and done2.returncode == 0, done.stdout.strip())
        code = (
            "import json\n"
            "from spaces import priv\n"
            f"info=json.load(open({str(info_path)!r}))\n"
            f"rec=info['permissions']['users'][{str(os.getuid())!r}]['permissions']\n"
            "priv.configure({'schema_version':1,'name':'ubuntu','permissions':{'user':"
            f"{{'uid':{TEST_UID},'gid':{TEST_UID},'permissions':rec}}}}}})\n"
        )
        done = sudo_python(code)
        check("auth: priv.configure adds the user to the space", done.returncode == 0, done.stdout.strip()[-120:])
        stop_space()
        run(["spaces", "enter", SPACE, "--", "true"], detach=True)
        before = guest_root(f"ls {downloads}").stdout.strip()
        check("mounts: Downloads absent in the guest while the user has no session", before == "", repr(before))
        right = sudo(
            "/usr/bin/spaces.priv", "enter-as-user", TEST_USER, SPACE, "--", "sh", "-c", "sudo -S -p '' id -u",
            input=password + "\n",
        )
        wrong = sudo(
            "/usr/bin/spaces.priv", "enter-as-user", TEST_USER, SPACE, "--", "sh", "-c", "sudo -S -p '' id -u",
            input="definitely-wrong\n",
        )
        check(
            "sudo bridge: guest sudo accepts the HOST password via spaces-pam/pam_spaces.so",
            right.returncode == 0 and right.stdout.strip().splitlines()[-1:] == ["0"],
            right.stdout.strip().replace("\n", " ")[-60:],
        )
        check(
            "sudo bridge: a wrong password is rejected",
            wrong.returncode != 0 and "Authentication failed" in wrong.stdout,
            wrong.stdout.strip().replace("\n", " ")[-60:],
        )
        # Open a real elogind session for the throwaway user.
        helper = (
            "import ctypes,ctypes.util,os,sys\n"
            "open('/sys/fs/cgroup/cgroup.procs','w').write(str(os.getpid()))\n"
            "pam=ctypes.CDLL(ctypes.util.find_library('pam'))\n"
            "class M(ctypes.Structure):_fields_=[('s',ctypes.c_int),('m',ctypes.c_char_p)]\n"
            "class R(ctypes.Structure):_fields_=[('r',ctypes.c_char_p),('c',ctypes.c_int)]\n"
            "CV=ctypes.CFUNCTYPE(ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.POINTER(M)),ctypes.POINTER(ctypes.POINTER(R)),ctypes.c_void_p)\n"
            "class C(ctypes.Structure):_fields_=[('f',CV),('a',ctypes.c_void_p)]\n"
            "cv=C(CV(lambda n,m,r,d:19),None);h=ctypes.c_void_p()\n"
            "pam.pam_start.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.POINTER(C),ctypes.POINTER(ctypes.c_void_p)]\n"
            f"assert pam.pam_start(b'system-local-login',b'{TEST_USER}',ctypes.byref(cv),ctypes.byref(h))==0\n"
            "pam.pam_set_item(h,3,b'tty9')\n"
            "assert pam.pam_open_session(h,0)==0\n"
            "print('READY',flush=True)\n"
            "sys.stdin.read()\n"
            "pam.pam_close_session(h,0);pam.pam_end(h,0)\n"
        )
        holder = subprocess.Popen(
            ["sudo", "-n", "/usr/bin/python3", "-I", "-c", helper],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        ready = holder.stdout.readline().strip() if holder.stdout else ""
        sessions = run(["loginctl", "list-sessions", "--no-legend"]).stdout
        check("mounts: elogind session opened for the throwaway user", ready == "READY" and TEST_USER in sessions, sessions.replace("\n", " | ")[:100])
        appeared = wait_for(lambda: "marker.txt" in guest_root(f"ls {downloads}").stdout, 30, 1)
        check("mounts: Downloads appears in the guest once the user has an active elogind session", appeared)
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=30)
        holder = None
        vanished = wait_for(lambda: "marker.txt" not in guest_root(f"ls {downloads}").stdout, 30, 1)
        check("mounts: Downloads disappears when the session ends", vanished)
        log = sudo("grep", "-E", f"{TEST_USER} .*(logged|mounted)", f"/var/log/spaces/{SPACE}/current").stdout
        check("mounts: launcher log records the login and logout transitions", "logged in; mounted" in log and "logged out; unmounted" in log, log.strip().splitlines()[-1][-100:] if log.strip() else "")
    finally:
        if holder is not None and holder.stdin is not None:
            holder.stdin.close()
            holder.wait(timeout=30)
        stop_space()
        sudo("userdel", "-f", "-r", TEST_USER)
        if (BACKUP / "info.json").exists() or sudo("test", "-d", str(BACKUP)).returncode == 0:
            sudo("cp", "-a", str(BACKUP / "info.json"), str(info_path))
            for name in state_files:
                sudo("cp", "-a", str(BACKUP / name), str(STATE / "rootfs/etc" / name))
        sudo("rm", "-rf", str(STATE / "home" / TEST_USER), str(BACKUP))
        sudo("rmdir", f"/run/media/{TEST_USER}")
        gone = run(["getent", "passwd", TEST_USER]).stdout.strip() == ""
        guest_gone = sudo("grep", "-c", TEST_USER, str(STATE / "rootfs/etc/passwd")).stdout.strip() == "0"
        restored = TEST_USER not in sudo("cat", str(info_path)).stdout
        check("cleanup: throwaway user, guest account and info.json restored", gone and guest_gone and restored)


def check_real_user_mount() -> None:
    print("\n== real user mount ==", flush=True)
    downloads = Path.home() / "Downloads"
    downloads.mkdir(exist_ok=True)
    guest = run(["spaces", "enter", SPACE, "--", "stat", "-c", "%d:%i", str(downloads)], detach=True)
    host = os.stat(downloads)
    sessions = run(["loginctl", "list-sessions", "--no-legend"]).stdout
    check(
        "mounts: the user's ~/Downloads is mounted in the guest during their active session",
        guest.returncode == 0 and guest.stdout.strip().endswith(f":{host.st_ino}") and ME in sessions,
        guest.stdout.strip(),
    )


def check_stop_start() -> None:
    print("\n== stop / start ==", flush=True)
    stopped = stop_space()
    leftovers = sudo("cat", "/proc/mounts").stdout
    state = sudo("sv", "status", SERVICE).stdout
    check("stop: `sudo sv down` stops the space", stopped and state.startswith("down:"), f"{lxc_state()} {state.strip()[:40]}")
    check("stop: ready marker removed", not marker_present())
    check(
        "stop: no leftover mounts, cgroups, lxc processes or subtree controllers",
        f"/var/lib/spaces/{SPACE}" not in leftovers
        and not Path("/sys/fs/cgroup/spaces").exists()
        and subtree_control() == ""
        and run(["pgrep", "-x", "lxc-start"]).returncode == 1,
        f"subtree={subtree_control()!r} cgroup={Path('/sys/fs/cgroup/spaces').exists()}",
    )
    # `sv once` readiness (what start_unit uses): the marker appears.
    sudo("sv", "once", SERVICE)
    ready = wait_for(marker_present, 90, 1)
    check("start: `sudo sv once` boots the space and writes the ready marker", ready and lxc_state() == "RUNNING")
    done = run(["spaces", "enter", SPACE, "--", "true"], detach=True)
    check("start: enter works on the already running space", done.returncode == 0)
    stop_space()
    start = run(["spaces", "start", SPACE], detach=True, timeout=150)
    check("start: `spaces start ubuntu` (pkexec, no password) starts and waits for readiness", start.returncode == 0 and marker_present() and lxc_state() == "RUNNING", start.stdout.strip()[:80])
    check("cgroup: root subtree_control still empty while running", subtree_control() == "", repr(subtree_control()))
    stop_space()
    check(
        "final: space stopped, nothing left behind",
        lxc_state() in (None, "STOPPED") and subtree_control() == "" and not Path("/sys/fs/cgroup/spaces").exists() and f"/var/lib/spaces/{SPACE}" not in sudo("cat", "/proc/mounts").stdout,
    )


def main() -> int:
    if os.geteuid() == 0:
        print("run as the normal user (it uses sudo -n itself)", file=sys.stderr)
        return 2
    if sudo("true").returncode != 0:
        print("passwordless sudo is required", file=sys.stderr)
        return 2
    check_install()
    check_polkit()
    check_enter()
    check_runit()
    check_real_user_mount()
    check_caller_exit()
    check_auth_and_mounts()
    check_stop_start()
    failed = [label for label, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for label in failed:
        print("FAILED:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
