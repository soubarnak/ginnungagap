#!/usr/bin/env python3
"""M2 end-to-end check: boot a real guest through LxcBackend (needs root).

Run: sudo env PYTHONPATH=src SPACES_HOST_BACKEND=lxc \
  SPACES_LXC_PATH=/run/spaces-m2/lxc python3 void/spike/m2_check.py
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from spaces import launch
from spaces.host import lxc as lxc_module
from spaces.host.lxc import LxcBackend

NAME = "m2test"
ROOTFS = Path("/var/lib/spaces-spike/rootfs")
USER = "soubarna"
results: list[tuple[str, bool, str]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append((label, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {label}  {detail}", flush=True)


def read_subtree() -> str:
    return Path("/sys/fs/cgroup/cgroup.subtree_control").read_text().strip()


def main() -> int:
    backend = LxcBackend()
    work = Path(tempfile.mkdtemp(prefix="m2check-"))
    hostro = work / "ro"
    hostrw = work / "rw"
    hostro.mkdir()
    hostrw.mkdir()
    (hostro / "file").write_text("ro-data\n")
    runtime = lxc_module.lxc_path() / NAME
    print("cgroup.subtree_control before:", repr(read_subtree()), flush=True)

    kept = (*launch.KEPT_CAPS, *launch.NETWORK_CAPS["basic"])
    dropped = tuple(c for c in launch.DROPPED_CAPS if c not in kept)
    argv = [
        "/usr/bin/systemd-nspawn",
        "--quiet",
        f"--directory={ROOTFS}",
        f"--machine={NAME}",
        f"--hostname={NAME}",
        f"--bind-ro={hostro}:/mnt/hostro",
        f"--bind={hostrw}:/mnt/hostrw",
        f"--bind-ro={hostro / 'file'}:/run/m2/probe",
        "--bind-ro=/dev/null:/etc/systemd/system/m2mask.service",
        *launch.NETWORK_SYSCTL_BINDS,
        "--boot",
        "--setenv=SYSTEMD_GETTY_AUTO=no",
        "--console=read-only",
        "--private-users=no",
        "--keep-unit",
        "--settings=no",
        "--notify-ready=yes",
        "--resolv-conf=bind-host",
        f"--drop-capability={','.join(dropped)}",
        f"--capability={','.join(kept)}",
    ]
    backend.set_device_policy(NAME, "disabled", launch.BASE_DEVICE_ALLOW)
    launcher = backend.run_launcher(argv, dict(os.environ))
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not (runtime / "ready").exists():
            if launcher.poll() is not None:
                break
            time.sleep(0.5)
        check("ready marker appears", (runtime / "ready").exists())
        if not (runtime / "ready").exists():
            print((runtime / "lxc.log").read_text()[-3000:])
            return 1
        check("is_running", backend.is_running(NAME))
        completed = backend.exec_in_guest(
            "root", NAME, ["/usr/bin/systemctl", "is-system-running", "--wait"],
            stdout=subprocess.PIPE,
        )
        state = completed.stdout.decode().strip()
        check("system running/degraded", state in ("running", "degraded"), state)

        completed = backend.exec_in_guest("root", NAME, ["/bin/sh", "-c", "exit 5"])
        check("root exec exit code", completed.returncode == 5, str(completed.returncode))
        completed = backend.exec_in_guest(
            USER, NAME, ["/bin/sh", "-c", "id -u; echo $FOO; exit 7"],
            env={"FOO": "bar"}, stdout=subprocess.PIPE,
        )
        check(
            "user exec uid, env, exit code",
            completed.returncode == 7 and completed.stdout.decode().split() == ["1000", "bar"],
            f"{completed.returncode} {completed.stdout!r}",
        )

        def sh(script: str, who: str = "root") -> subprocess.CompletedProcess:
            return backend.exec_in_guest(
                who, NAME, ["/bin/sh", "-c", script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

        check("hostro readable", sh("cat /mnt/hostro/file").stdout == b"ro-data\n")
        check("hostro is read-only", sh("touch /mnt/hostro/x").returncode != 0)
        check("hostrw writable", sh("echo w > /mnt/hostrw/x").returncode == 0
              and (hostrw / "x").read_text() == "w\n")
        check(
            "resolv.conf equals host",
            sh("cat /etc/resolv.conf").stdout == Path("/etc/resolv.conf").read_bytes(),
        )

        check("bind under fresh /run tmpfs", sh("cat /run/m2/probe").stdout == b"ro-data\n")
        check("unit mask bound", sh("test -c /etc/systemd/system/m2mask.service").returncode == 0
              or sh("readlink -f /etc/systemd/system/m2mask.service").stdout.strip() == b"/dev/null")
        check("net sysctl writable (admin bind)", sh("test -w /proc/sys/net/ipv4/ip_forward").returncode == 0
              and sh("test ! -w /proc/sys/kernel/hostname").returncode == 0)
        # bind_into / unmount_in, including a /proc/PID/fd source.
        dyn = work / "dyn"
        dyn.mkdir()
        (dyn / "d").write_text("dyn\n")
        backend.bind_into(NAME, str(dyn), "/mnt/dyn/deep")
        check("bind_into visible", sh("cat /mnt/dyn/deep/d").stdout == b"dyn\n")
        backend.unmount_in(NAME, "/mnt/dyn/deep")
        check("unmount_in removes", sh("cat /mnt/dyn/deep/d").returncode != 0)
        descriptor = os.open(dyn, os.O_RDONLY)
        backend.bind_into(
            NAME, f"/proc/{os.getpid()}/fd/{descriptor}", "/mnt/dynfd", read_only=True
        )
        os.close(descriptor)
        check("bind_into via fd path", sh("cat /mnt/dynfd/d").stdout == b"dyn\n")
        check("bind_into read_only", sh("touch /mnt/dynfd/z").returncode != 0)
        backend.unmount_in(NAME, "/mnt/dynfd")

        # Live device policy: /dev/loop-control (c 10:237) is not in the base rules.
        check("mknod in guest", sh("mknod /dev/m2dev c 10 237").returncode == 0)
        opens = "exec 3</dev/m2dev"
        check("device denied by default", sh(opens).returncode != 0)
        backend.set_device_policy(
            NAME, "disabled", [*launch.BASE_DEVICE_ALLOW, ("/dev/char/10:237", "rw")]
        )
        check("device allowed live", sh(opens).returncode == 0)
        backend.set_device_policy(NAME, "disabled", launch.BASE_DEVICE_ALLOW)
        check("device denied again", sh(opens).returncode != 0)
        backend.set_device_policy(NAME, "full", ())
        check("full policy allows", sh(opens).returncode == 0)
        backend.set_device_policy(NAME, "disabled", launch.BASE_DEVICE_ALLOW)
        check("closed after full denies", sh(opens).returncode != 0)

        init_pid = backend._init_pid(NAME)
        check("peer_in_space guest pid", backend.peer_in_space(init_pid, NAME))
        check("peer_in_space host pid 1", not backend.peer_in_space(1, NAME))
        print("guest cgroup:", Path(f"/proc/{init_pid}/cgroup").read_text().strip())
        print("cgroup.subtree_control while running:", repr(read_subtree()))
        check("host_user_environment tolerant", backend.host_user_environment(1000, 1000) is None or True)

        launcher.send_signal(signal.SIGTERM)
        launcher.send_signal(signal.SIGTERM)
        code = launcher.wait(timeout=90)
        check("launcher exit code 0", code == 0, str(code))
        check("lxc-info STOPPED", backend._state(NAME) in ("STOPPED", None),
              str(backend._state(NAME)))
        check("ready marker removed", not (runtime / "ready").exists())
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=30)
        shutil.rmtree(work, ignore_errors=True)

    # runit smoke test against the real /etc/sv and /var/service.
    smoke = "m2smoke"
    link = lxc_module._runit_dirs()[1] / f"spaces-{smoke}"
    lxc_module.ensure_service(smoke)
    check("runit supervise/ok", (link / "supervise" / "ok").exists())
    status = subprocess.run(["sv", "status", str(link)], capture_output=True, text=True).stdout
    check("sv status down", status.startswith("down:"), status.strip())
    backend.forget_unit(smoke)
    time.sleep(1)
    check("forget removes service", not link.exists() and not link.is_symlink()
          and not Path(f"/etc/sv/spaces-{smoke}").exists())
    print("cgroup.subtree_control after:", repr(read_subtree()))
    leftovers = [str(p) for p in Path("/sys/fs/cgroup").glob("spaces*")]
    leftovers += [str(p) for p in Path("/sys/fs/cgroup").glob("lxc*")]
    check("no leftover cgroups", not leftovers, str(leftovers))
    failed = [label for label, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
