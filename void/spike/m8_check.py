#!/usr/bin/env python3
"""M8 check: the xbps package install (never calls dev-install).

Run as the normal user from the niri session (passwordless sudo) after

    void/tools/xbps-build.sh && sudo xbps-install -y -R <printed repo> spaces

    python3 void/spike/m8_check.py [--lifecycle] [--skip-sudo-bridge] [--skip-autostart]

Sections: package (files owned, modes, no setuid, no unowned files, doctor), enter each
distro, the sudo bridge (m3's throwaway-user check), a GUI app on niri (m4), NVIDIA (m5),
autostart smoke (m7), install-flavor dry run, the orphaned-broker fix (kill -9 of a
launcher), and a final cleanliness check. `--lifecycle` also builds revision 2, upgrades
while a space is RUNNING, removes the package (a space still running), and reinstalls
(the machine ends with that revision installed; reinstall revision 1 with
`sudo xbps-install -fy -R <repo> spaces-0.0.1_1`). Prints PASS/FAIL/SKIP, exits non-zero
on FAIL. All spaces are stopped at the end.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import m3_check as m3  # noqa: E402
import m4_check as m4  # noqa: E402
import m5_check as m5  # noqa: E402
import m7_check as m7  # noqa: E402

SPACES = ("ubuntu", "arch", "kali", "fedora")
VP = Path(os.environ.get("VOID_PACKAGES", Path.home() / ".local/share/ginnungagap/void-packages"))
REPO = VP / "hostdir/binpkgs"
PACKAGES = ("spaces", "spaces-arch-install-scripts", "spaces-archlinux-keyring", "spaces-rankmirrors", "ubuntu-keyring")
mine = {"PASS": 0, "FAIL": 0, "SKIP": 0}
failed: list[str] = []


def check(label: str, condition: object, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    mine[status] += 1
    if not condition:
        failed.append(label)
    print(f"{status}  {label}" + (f"  [{detail}]" if detail and not condition else ""), flush=True)
    return bool(condition)


def skip(label: str, why: str) -> None:
    mine["SKIP"] += 1
    print(f"SKIP  {label}  [{why}]", flush=True)


run = m7.run
sudo = m7.sudo
wait_for = m7.wait_for
service_state = m7.service_state
stop_all = m7.stop_all


def stray() -> list[str]:
    """Helpers, containers and supervisors that a stopped machine must not have."""

    out = run(["pgrep", "-af", r"lxc-start|spaces-system-broker|spaces-broker|xdg-dbus-proxy .*/run/spaces"]).stdout
    return [line for line in out.splitlines() if "pgrep" not in line and "m8_check" not in line]


def installed_version() -> str:
    return run(["xbps-query", "-p", "pkgver", "spaces"]).stdout.strip()


# ----------------------------------------------------------------- package


def check_package() -> None:
    print("\n== package ==", flush=True)
    version = installed_version()
    check("spaces is installed from xbps", version.startswith("spaces-"), version)
    check("no dev-install leftovers (state file, backup keyring)",
          not Path("/etc/spaces/dev-install.state").exists()
          and not Path("/usr/share/keyrings/ubuntu-archive-keyring.gpg.dev-install-backup").exists())
    for name in PACKAGES:
        check(f"{name} is installed", run(["xbps-query", name]).returncode == 0)
    owned: set[str] = set()
    for name in PACKAGES:
        owned |= {line.split(" ->")[0] for line in run(["xbps-query", "-f", name]).stdout.splitlines()}
    on_disk = set()
    for top in ("/usr/lib/spaces", "/usr/share/spaces", "/etc/spaces", "/etc/sv/spaces-autostart",
                *(str(p) for p in Path("/usr/lib").glob("python3*/site-packages/spaces*"))):
        for base, _dirs, files in os.walk(top):
            if "__pycache__" in base:
                continue
            on_disk |= {os.path.join(base, f) for f in files}
    unowned = sorted(on_disk - owned)
    check("every file under /usr/lib/spaces, /usr/share/spaces, /etc/spaces, the autostart service and the python package is owned by a package"
          " (except the generated config.json and its hash)",
          set(unowned) <= {"/etc/spaces/config.json", "/etc/spaces/config.json.generated", "/etc/spaces/config.json.new"}, str(unowned))
    pkgdb = sudo("xbps-pkgdb", *PACKAGES, timeout=300)
    check("xbps-pkgdb reports nothing for the five packages", pkgdb.returncode == 0 and not pkgdb.stdout.strip(), pkgdb.stdout + pkgdb.stderr)
    for path in ("/usr/bin/spaces.priv", "/usr/lib/spaces/spaces-pam", "/usr/lib/spaces/spaces-broker",
                 "/usr/lib/spaces/spaces-system-broker", "/etc/pam.d/spaces", "/etc/apparmor.d/spaces-container",
                 "/usr/share/polkit-1/actions/org.anatase.spaces.policy", "/var/lib/spaces", "/var/cache/spaces", "/var/log/spaces"):
        info = os.stat(path)
        check(f"{path}: root-owned, not group/world writable, no setuid/setgid",
              info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID), oct(info.st_mode))
    priv = Path("/usr/bin/spaces.priv").read_text()
    check("spaces.priv is the sh wrapper with the shim directory first on PATH",
          priv.startswith("#!/bin/sh\n") and "PATH=/usr/lib/spaces/void/bin:" in priv)
    check("the polkit policy binds to /usr/bin/spaces.priv",
          '"org.freedesktop.policykit.exec.path">/usr/bin/spaces.priv<' in Path("/usr/share/polkit-1/actions/org.anatase.spaces.policy").read_text())
    check("no systemd units were installed", not Path("/usr/lib/systemd/system/spaces@.service").exists()
          and not Path("/usr/lib/systemd/user/spaces@.service").exists())
    check("autostart service is installed with vsv-style supervise links and is not linked",
          Path("/etc/sv/spaces-autostart/run").exists()
          and os.readlink("/etc/sv/spaces-autostart/supervise") == "/run/runit/supervise.spaces-autostart"
          and os.readlink("/etc/sv/spaces-autostart/log/supervise") == "/run/runit/supervise.spaces-autostart-log"
          and not Path("/var/service/spaces-autostart").exists())
    check("the Arch keyring marker and the pacman keyring exist",
          Path("/var/lib/spaces/.host/arch/keyring-version").exists() and sudo("test", "-f", "/var/lib/spaces/.host/arch/gnupg/trustdb.gpg").returncode == 0)
    check("ubuntu archive keyring has the signing key (debootstrap path)",
          "F6ECB3762474EDA9D21B7022871920D1991BC93C" in run(["gpg", "--show-keys", "--with-colons", "/usr/share/keyrings/ubuntu-archive-keyring.gpg"]).stdout)
    doctor = sudo("spaces-void", "doctor")
    check("spaces-void doctor: no FAIL", doctor.returncode == 0 and "0 FAIL" in doctor.stdout, doctor.stdout[-400:])
    for name in SPACES:
        check(f"space {name} survived (info.json present)", sudo("test", "-f", f"/var/lib/spaces/{name}/info.json").returncode == 0)


# ------------------------------------------------------------------- enter


def check_enter() -> None:
    print("\n== enter each distro ==", flush=True)
    for command, os_id in (("ubuntu", "ubuntu"), ("arch-linux", "arch"), ("kali", "kali"), ("fedora", "fedora")):
        out = run([command, "--", "cat", "/etc/os-release"], timeout=150)
        check(f"`{command}` enters the {os_id} space", re.search(rf'^ID="?{os_id}"?$', out.stdout, re.M) is not None, out.stdout + out.stderr)
    out = run(["spaces", "enter", "ubuntu", "--", "id", "-un"])
    check("`spaces enter ubuntu -- id` runs as the user", out.stdout.strip() == os.environ.get("USER", "soubarna"), out.stdout)
    stop_all()
    check("all spaces stop cleanly", all(m7.stopped(n) for n in SPACES) and not stray(), str(stray()))


# --------------------------------------------------------------- the rest


def check_flavor_dry_run() -> None:
    print("\n== install-flavor dry run ==", flush=True)
    out = run(["spaces-void", "install-flavor", "ubuntu", "--flavor", "kde"], timeout=120)
    check("install-flavor ubuntu --flavor kde adds nothing", "adds nothing" in out.stdout, out.stdout + out.stderr)


def launcher_of(name: str) -> int | None:
    out = run(["pgrep", "-f", rf"spaces\.priv import main.* launch {name}$"]).stdout.split()
    if not out:
        out = run(["pgrep", "-f", rf"from spaces\.priv import main.*launch {name}"]).stdout.split()
    return int(out[0]) if out else None


def check_orphans() -> None:
    print("\n== orphaned system broker after kill -9 of the launcher ==", flush=True)
    stop_all()
    run(["ubuntu", "--", "true"], timeout=150)
    pid = launcher_of("ubuntu")
    brokers = run(["pgrep", "-f", "spaces-system-broker --broker /run/spaces/ubuntu/"]).stdout.split()
    check("a launcher and one system broker are running", pid is not None and len(brokers) == 1, f"launcher={pid} brokers={brokers}")
    if pid is None:
        return
    sudo("kill", "-9", str(pid))
    gone = wait_for(lambda: run(["pgrep", "-f", "spaces-system-broker --broker /run/spaces/ubuntu/"]).returncode != 0, 3, 0.1)
    check("the system broker dies with the launcher (PR_SET_PDEATHSIG)", gone)
    run(["ubuntu", "--", "true"], timeout=150)
    brokers = run(["pgrep", "-f", "spaces-system-broker --broker /run/spaces/ubuntu/"]).stdout.split()
    lxc = run(["pgrep", "-x", "lxc-start"]).stdout.split()
    check("after the next start there is exactly one broker and one container", len(brokers) == 1 and len(lxc) == 1, f"{brokers} {lxc}")
    stop_all()
    check("after stopping: no helper, container or cgroup is left", not stray() and not Path("/sys/fs/cgroup/spaces").exists(), str(stray()))


# --------------------------------------------------------------- lifecycle


def check_lifecycle() -> None:
    print("\n== upgrade, remove, reinstall ==", flush=True)
    out = run([str(ROOT / "void/tools/xbps-build.sh"), "--revision", "2", "spaces"], timeout=1800)
    check("revision 2 builds", out.returncode == 0, out.stdout[-300:] + out.stderr[-300:])
    stop_all()
    run(["ubuntu", "--", "true"], timeout=150)
    container = run(["pgrep", "-x", "lxc-start"]).stdout.split()
    before = installed_version()
    up = sudo("xbps-install", "-y", "-S", "-R", str(REPO), "-u", "spaces", timeout=600)
    after = installed_version()
    check("xbps-install -u upgrades to revision 2 while ubuntu runs", up.returncode == 0 and before != after and after.endswith("_2"), f"{before} -> {after} {up.stdout[-200:]}")
    check("the running space kept running (same lxc-start)", run(["pgrep", "-x", "lxc-start"]).stdout.split() == container, str(container))
    check("ubuntu still answers", run(["ubuntu", "--", "id", "-un"]).stdout.strip() != "")
    profiles = sudo("cat", "/sys/kernel/security/apparmor/profiles").stdout
    check("the AppArmor profile is loaded after the upgrade", "spaces-container" in profiles)
    if Path("/dev/nvidiactl").exists():
        smi = run(["ubuntu", "--", "nvidia-smi", "-L"])
        check("nvidia-smi still works in the running space", "GPU 0" in smi.stdout, smi.stdout + smi.stderr)
    check("config.json is still generated and valid", sudo("/usr/bin/spaces-void", "doctor").stdout.count("FAIL  ") == 0)
    # remove with a space running
    removed = sudo("xbps-remove", "-y", "spaces", timeout=600)
    text = removed.stdout + removed.stderr
    check("xbps-remove succeeds with a space running", removed.returncode == 0, text[-300:])
    check("REMOVE says the spaces are kept", "Your spaces are kept in /var/lib/spaces" in text, text[-300:])
    check("spaces data is intact (info.json, .host)",
          all(sudo("test", "-f", f"/var/lib/spaces/{n}/info.json").returncode == 0 for n in SPACES)
          and sudo("test", "-d", "/var/lib/spaces/.host/arch/gnupg").returncode == 0
          and sudo("test", "-d", "/var/lib/spaces/.host/fedora").returncode == 0)
    check("no container, helper, runsv or svlogd is left", not stray()
          and run(["pgrep", "-f", r"^runsv spaces-|^svlogd -tt /var/log/spaces"]).returncode != 0, str(stray()))
    check("no spaces service links or directories are left",
          not list(Path("/var/service").glob("spaces-*")) and not list(Path("/etc/sv").glob("spaces-*")))
    check("the AppArmor profile is unloaded", "spaces-container" not in sudo("cat", "/sys/kernel/security/apparmor/profiles").stdout)
    check("the files are gone (spaces, spaces.priv, config.json, python package)",
          not Path("/usr/bin/spaces").exists() and not Path("/usr/bin/spaces.priv").exists()
          and not Path("/etc/spaces/config.json").exists() and not list(Path("/usr/lib").glob("python3*/site-packages/spaces")))
    check("/var/lib/spaces and the cgroup root are clean (subtree_control empty)",
          Path("/var/lib/spaces").is_dir() and Path("/sys/fs/cgroup/cgroup.subtree_control").read_text().strip() == "")
    again = sudo("xbps-install", "-y", "-R", str(REPO), "spaces", timeout=600)
    check("reinstall succeeds", again.returncode == 0 and installed_version() == after, again.stdout[-300:])
    out = run(["spaces", "enter", "ubuntu", "--", "id", "-un"], timeout=150)
    check("`spaces enter ubuntu` works again after the reinstall", out.stdout.strip() != "" and out.returncode == 0, out.stdout + out.stderr)
    stop_all()


# -------------------------------------------------------------------- main


def merge(module: object, name: str) -> None:
    """Fold the counts of an imported older check module into this run."""

    for entry in getattr(module, "results", []) if not isinstance(getattr(module, "results", None), dict) else []:
        label, status, _detail = entry
        status = ("PASS" if status else "FAIL") if isinstance(status, bool) else status
        mine[status] += 1
        if status == "FAIL":
            failed.append(f"{name}: {label}")
    if isinstance(getattr(module, "results", None), dict):
        for key, value in module.results.items():  # type: ignore[attr-defined]
            mine[key] += value
        module.results.update({"PASS": 0, "FAIL": 0, "SKIP": 0})  # type: ignore[attr-defined]
    elif hasattr(module, "results"):
        module.results.clear()  # type: ignore[attr-defined]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lifecycle", action="store_true")
    parser.add_argument("--lifecycle-only", action="store_true", help="only the upgrade/remove/reinstall section")
    parser.add_argument("--skip-sudo-bridge", action="store_true")
    parser.add_argument("--skip-autostart", action="store_true")
    arguments = parser.parse_args()
    if os.geteuid() == 0 or sudo("true").returncode != 0:
        print("run as the normal user with passwordless sudo", file=sys.stderr)
        return 2
    if arguments.lifecycle_only:
        check_lifecycle()
        arguments.skip_sudo_bridge = arguments.skip_autostart = True
        return finish()
    check_package()
    check_enter()
    if arguments.skip_sudo_bridge:
        skip("sudo bridge (host-PAM, throwaway user)", "--skip-sudo-bridge")
    else:
        m3.check_auth_and_mounts()
        merge(m3, "m3")
    m4.check_gui()
    merge(m4, "m4")
    if m5.start_space():
        m5.check_nvidia_nodes()
        m5.check_nvidia_userspace(False)
        merge(m5, "m5")
    else:
        check("ubuntu starts for the NVIDIA checks", False)
    if arguments.skip_autostart:
        skip("autostart smoke", "--skip-autostart")
    else:
        m7.check_autostart()
        merge(m7, "m7")
    check_flavor_dry_run()
    check_orphans()
    stop_all()
    if arguments.lifecycle:
        check_lifecycle()
    return finish()


def finish() -> int:
    print("\n== final state ==", flush=True)
    stop_all()
    check("all spaces stopped, no helpers, containers or cgroups", not stray() and not Path("/sys/fs/cgroup/spaces").exists(), str(stray()))
    check("cgroup.subtree_control is empty", Path("/sys/fs/cgroup/cgroup.subtree_control").read_text().strip() == "")
    state = Path("/sys/class/watchdog/watchdog0/state")
    check("the host watchdog is inactive", not state.exists() or state.read_text().strip() == "inactive", state.read_text() if state.exists() else "")
    check("no mounts below /var/lib/spaces or /run/spaces",
          not re.search(r" /(var/lib|run)/spaces", Path("/proc/mounts").read_text()))
    print(f"\n{mine['PASS']} passed, {mine['FAIL']} failed, {mine['SKIP']} skipped")
    for label in failed:
        print("FAILED:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
