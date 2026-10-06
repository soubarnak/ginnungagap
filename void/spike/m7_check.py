#!/usr/bin/env python3
"""M7 check: entry commands, autostart, desktop flavour, housekeeping.

Run as the normal user from the niri session (needs passwordless sudo) after
`sudo void/tools/dev-install.sh`:

    python3 void/spike/m7_check.py [--skip-flavor-install] [--reinstall]

Sections: entry commands (+ the opt-in shell snippet), autostart (links the
spaces-autostart service temporarily, restores the autostart state files and
unlinks it again), desktop flavour (generated config, install-flavor on
ubuntu/arch/fedora, a GTK app on niri, the Qt gtk3 platform theme), housekeeping
(install state, dnf5 tuning, arch multilib, uninstall keeps /var/lib/spaces/.host;
--reinstall additionally runs dev-uninstall.sh and dev-install.sh), doctor/gc,
and a final cleanliness check. Prints PASS/FAIL/SKIP and exits non-zero on FAIL.
All spaces are stopped at the end.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPACES = ("ubuntu", "arch", "kali", "fedora")
STATE = Path("/var/lib/spaces")
AUTOSTART_LINK = Path("/var/service/spaces-autostart")
results = {"PASS": 0, "FAIL": 0, "SKIP": 0}


def check(label: str, condition: object, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    results[status] += 1
    print(f"{status}  {label}" + (f"  [{detail}]" if detail and status == "FAIL" else ""), flush=True)
    return bool(condition)


def skip(label: str, why: str) -> None:
    results["SKIP"] += 1
    print(f"SKIP  {label}  [{why}]", flush=True)


def run(argv: list[str], timeout: float = 120, **kw) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw
        )
    except subprocess.TimeoutExpired as error:
        out = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        return subprocess.CompletedProcess(argv, 124, out + "\n[timeout]", "")


def sudo(*argv: str, **kw) -> subprocess.CompletedProcess[str]:
    return run(["sudo", "-n", *argv], **kw)


def wait_for(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def service_state(name: str) -> str:
    return sudo("sv", "status", f"/var/service/spaces-{name}").stdout.partition(":")[0].strip()


def stopped(name: str) -> bool:
    """down, or no service at all (a reinstall removes them until the next start)."""

    return service_state(name) in ("down", "fail")


def ready(name: str) -> bool:
    return sudo("test", "-f", f"/run/spaces/lxc/{name}/ready").returncode == 0


def stop_all() -> None:
    for name in SPACES:
        if (Path("/var/service") / f"spaces-{name}").exists():
            sudo("sv", "-w", "90", "down", f"/var/service/spaces-{name}", timeout=120)


# ------------------------------------------------------------ entry commands


def check_entry() -> None:
    print("\n== entry commands ==", flush=True)
    wrapper = ROOT / "void/entry/enter-space"
    for command in ("ubuntu", "fedora", "kali", "arch-linux"):
        path = Path("/usr/bin") / command
        check(f"/usr/bin/{command} is a root-owned copy of the wrapper",
              path.is_file() and path.read_bytes() == wrapper.read_bytes()
              and path.stat().st_uid == 0 and os.access(path, os.X_OK))
        owner = run(["xbps-query", "-o", str(path)])
        check(f"/usr/bin/{command} is not owned by an xbps package", owner.stdout.strip() == "", owner.stdout)
    text = wrapper.read_text()
    check("wrapper is POSIX sh", text.startswith("#!/bin/sh\n") and "bash" not in text)
    check("/usr/bin/arch is still coreutils and is not ours",
          "coreutils" in run(["xbps-query", "-o", "/usr/bin/arch"]).stdout
          and "spaces" not in Path("/usr/bin/arch").read_bytes()[:200].decode(errors="ignore"))
    out = run(["ubuntu", "--", "id", "-un"], timeout=90)
    check("`ubuntu -- id` runs as the user in the space (auto-starts it)", out.stdout.strip() == "soubarna", out.stdout + out.stderr)
    out = run(["ubuntu", "cat", "/etc/os-release"])
    check("`ubuntu cat /etc/os-release` (no --) also works", "ID=ubuntu" in out.stdout, out.stdout + out.stderr)
    out = run(["arch-linux", "--", "cat", "/etc/os-release"], timeout=90)
    check("`arch-linux -- cat /etc/os-release` shows Arch", re.search(r"^ID=arch$", out.stdout, re.M) is not None, out.stdout + out.stderr)
    for command, os_id in (("kali", "kali"), ("fedora", "fedora")):
        out = run([command, "--", "cat", "/etc/os-release"], timeout=90)
        check(f"`{command} -- cat /etc/os-release`", re.search(rf"^ID=\"?{os_id}\"?$", out.stdout, re.M) is not None, out.stdout + out.stderr)
    out = run(["ubuntu", "--", "sh", "-c", "exit 7"])
    check("exit status propagates", out.returncode == 7, str(out.returncode))

    print("\n== opt-in shell snippet ==", flush=True)
    snippet = "/usr/share/spaces/void/shell/spaces.sh"
    check("snippet is installed", os.path.isfile(snippet))
    check("snippet is not enabled by default",
          not Path("/etc/bash/bashrc.d/spaces.sh").exists()
          and all("spaces.sh" not in Path.home().joinpath(f).read_text(errors="ignore")
                  for f in (".bashrc", ".bash_profile", ".profile") if Path.home().joinpath(f).exists()))
    script = (f". {snippet}; ubuntu -- id -un; arch -- sh -c 'echo $0; . /etc/os-release; echo $ID' x; "
              "type -t apt dnf; type -t pacman xbps-install; apt install foo; echo rc=$?")
    out = run(["bash", "--norc", "-c", script], timeout=90)
    lines = out.stdout.split()
    check("bash: sourcing works, `ubuntu -- id` and `arch` function enter the spaces",
          "soubarna" in lines and "arch" in lines, out.stdout + out.stderr)
    check("bash: hints for apt/dnf only (functions), pacman and xbps-* untouched",
          lines.count("function") == 2 and "file" in lines and "pacman" not in out.stderr, out.stdout)
    check("bash: apt hint points at the ubuntu space and returns 127",
          "ubuntu apt" in out.stderr and "rc=127" in out.stdout, out.stderr)
    out = run(["bash", "--norc", "-c", f"SPACES_NO_HINTS=1; . {snippet}; type -t apt || echo none"])
    check("SPACES_NO_HINTS=1 disables the hints", out.stdout.strip() == "none", out.stdout)
    stop_all()


# ---------------------------------------------------------------- autostart


def read_users(name: str) -> str | None:
    done = sudo("cat", str(STATE / name / "autostart-users"))
    return done.stdout if done.returncode == 0 else None


def check_autostart() -> None:
    print("\n== autostart ==", flush=True)
    stop_all()
    saved = {name: read_users(name) for name in SPACES}
    check("service files installed, not linked", Path("/etc/sv/spaces-autostart/run").exists() and not AUTOSTART_LINK.exists())
    try:
        out = run(["spaces-void", "autostart", "list"])
        check("autostart list shows every space", all(n in out.stdout for n in SPACES), out.stdout)
        # Only ubuntu enabled for the user.
        for name in SPACES[1:]:
            run(["spaces-void", "autostart", "disable", name])
        users = {name: (read_users(name) or "").split() for name in SPACES}
        check("only ubuntu enabled for soubarna", users["ubuntu"] == ["soubarna"] and not any(users[n] for n in SPACES[1:]), str(users))
        check("enable/disable of --boot flag round-trips",
              run(["spaces-void", "autostart", "enable", "kali", "--boot"]).returncode == 0
              and (STATE / "kali" / "autostart-boot").exists()
              and run(["spaces-void", "autostart", "disable", "kali", "--boot"]).returncode == 0
              and not (STATE / "kali" / "autostart-boot").exists())
        time.sleep(3)
        check("enabled but service not linked: nothing starts", service_state("ubuntu") == "down")
        sudo("rm", "-rf", "/run/spaces/autostart")
        began = time.monotonic()
        link = sudo("ln", "-s", "/etc/sv/spaces-autostart", "/var/service/")
        check("service linked", link.returncode == 0, link.stderr)
        up = wait_for(lambda: ready("ubuntu"), 40)
        elapsed = time.monotonic() - began
        check(f"ubuntu comes up within 30 s of the service start ({elapsed:.0f} s)", up and elapsed < 30, f"{elapsed:.0f}s")
        check("other spaces were left alone", all(service_state(n) == "down" for n in SPACES[1:]))
        log = sudo("cat", "/var/log/spaces/autostart/current").stdout
        check("daemon log names the start", "started ubuntu (login of uid" in log, log[-300:])
        sudo("sv", "-w", "90", "down", "/var/service/spaces-ubuntu", timeout=120)
        time.sleep(36)  # more than one periodic evaluation (30 s)
        check("a deliberate sv down is not undone", service_state("ubuntu") == "down" and not ready("ubuntu"))
        daemon = sudo("sv", "status", str(AUTOSTART_LINK)).stdout
        check("daemon still running", daemon.startswith("run:"), daemon)
        # Restarting the daemon does not undo it either (state on tmpfs).
        sudo("sv", "restart", str(AUTOSTART_LINK))
        time.sleep(6)
        check("a daemon restart does not undo it", service_state("ubuntu") == "down")
        out = sudo("cat", "/run/spaces/autostart/state.json")
        check("login state is kept on tmpfs", '"seen"' in out.stdout, out.stdout)
    finally:
        sudo("sv", "-w", "30", "down", str(AUTOSTART_LINK))
        sudo("rm", "-f", str(AUTOSTART_LINK))
        sudo("rm", "-rf", "/run/spaces/autostart")
        for name, content in saved.items():
            path = STATE / name / "autostart-users"
            if content is None:
                sudo("rm", "-f", str(path))
            else:
                subprocess.run(["sudo", "-n", "tee", str(path)], input=content, text=True,
                               stdout=subprocess.DEVNULL, check=False)
                sudo("chmod", "644", str(path))
        stop_all()
    check("autostart service unlinked, daemon gone",
          not AUTOSTART_LINK.exists() and wait_for(lambda: run(["pgrep", "-f", "spaces.host.autostart"]).returncode != 0, 15))
    check("autostart state files restored", {n: read_users(n) for n in SPACES} == saved)


# ------------------------------------------------------------------- flavour


def check_flavor(install: bool) -> None:
    print("\n== desktop flavour ==", flush=True)
    cfg = json.loads(Path("/etc/spaces/config.json").read_text())
    base = json.loads(Path("/usr/share/spaces/config.base.json").read_text())
    check("base config carries desktop_flavor=auto", base.get("desktop_flavor") == "auto")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    expected_gtk = "kde" not in desktop.lower() and "plasma" not in desktop.lower()
    packages = {d: cfg["distros"][d]["packages"] for d in SPACES}
    check(f"auto resolves for {desktop or '?'} -> {'gtk' if expected_gtk else 'kde'} in config.json",
          all(("xdg-desktop-portal-gtk" in p) == expected_gtk for p in packages.values()), str(packages))
    check("config.json adds no extra packages of its own (only the desktop flavour's)", all(not ({"fastfetch", "tmux", "zsh", "screen"} & set(p)) for p in packages.values()))
    check("no flavour package removes polkit-kde or the KDE portal",
          not any(re.search(r"kde|plasma", n) for p in packages.values() for n in p))
    check("arch has no /usr/lib32 overlay without multilib in its pacman.conf",
          "multilib" in Path(STATE / "arch/rootfs/etc/pacman.conf").read_text().replace("#[multilib", "")
          or "/usr/lib32" not in {o["destination"] for o in cfg["distros"]["arch"].get("overlays", [])})
    out = run(["spaces-void", "install-flavor", "ubuntu", "--flavor", "kde"])
    check("--flavor kde installs nothing", "adds nothing" in out.stdout, out.stdout + out.stderr)
    if not install:
        skip("install-flavor runs", "--skip-flavor-install")
    else:
        for name, query in (("ubuntu", ["dpkg", "-s", "xdg-desktop-portal-gtk", "gnome-themes-extra", "qt6-gtk-platformtheme"]),
                            ("arch", ["pacman", "-Q", "adw-gtk-theme", "xdg-desktop-portal-gtk", "gtk3"]),
                            ("fedora", ["rpm", "-q", "adw-gtk3-theme", "xdg-desktop-portal-gtk", "gtk3"])):
            out = run(["spaces-void", "install-flavor", name], timeout=600)
            check(f"install-flavor {name} succeeds and lists the packages", out.returncode == 0 and f"installed in {name}:" in out.stdout, out.stdout[-300:] + out.stderr[-300:])
            have = run(["spaces", "enter", name, "--root", "--", *query])
            check(f"{name}: the flavour packages are installed", have.returncode == 0, have.stdout[-200:] + have.stderr[-200:])
        kde = run(["spaces", "enter", "ubuntu", "--root", "--", "dpkg", "-s", "polkit-kde-agent-1", "xdg-desktop-portal-kde"])
        check("ubuntu still has polkit-kde-agent-1 and xdg-desktop-portal-kde", kde.returncode == 0, kde.stdout[-200:])
    # GTK app on niri
    for name, command in (("ubuntu", "ubuntu"), ("arch", "arch-linux")):
        have = run(["spaces", "enter", name, "--", "sh", "-c", "command -v gnome-calculator"])
        if have.returncode != 0:
            skip(f"gnome-calculator window ({name})", "gnome-calculator not installed")
            continue
        process = subprocess.Popen([command, "gnome-calculator"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        seen = wait_for(lambda: "org.gnome.Calculator" in run(["niri", "msg", "windows"]).stdout, 30)
        check(f"gnome-calculator from {name} opens a window on niri", seen)
        run(["spaces", "enter", name, "--", "sh", "-c", "kill $(pgrep -x gnome-calculato)"])
        process.wait(timeout=20)
    # Qt
    have = run(["spaces", "enter", "ubuntu", "--", "python3", "-c", "import PyQt6.QtWidgets"])
    if have.returncode != 0:
        skip("Qt app uses the gtk3 platform theme", "python3-pyqt6 not installed in ubuntu")
    else:
        script = ("from PyQt6.QtWidgets import QApplication, QLabel; import sys; a=QApplication(sys.argv); "
                  "l=QLabel('x'); l.show(); l.close(); print(a.platformName())")
        out = run(["spaces", "enter", "ubuntu", "--", "env", "QT_LOGGING_RULES=qt.qpa.theme*=true", "python3", "-c", script], timeout=60)
        text = out.stdout + out.stderr
        check("Qt app: platform theme gtk3 is created (QT_QPA_PLATFORMTHEME=gtk3 forwarded)",
              'Successfully created platform theme "gtk3"' in text, text[-300:])
        check("Qt app: no warning about a missing platform theme/plugin",
              not re.search(r"(?i)could not (load|find)|no such platform|unable to find", text), text[-300:])
    stop_all()


# --------------------------------------------------------------- housekeeping


def check_housekeeping(reinstall: bool) -> None:
    print("\n== housekeeping ==", flush=True)
    state = dict(line.split("=", 1) for line in Path("/etc/spaces/dev-install.state").read_text().split("\n") if "=" in line)
    packages = set(state.get("packages", "").split())
    check("dev-install.state records pam-devel, pacman and m4", {"pam-devel", "pacman", "m4"} <= packages, str(packages))
    manual = run(["xbps-query", "-m"]).stdout
    check("every recorded package is a manually installed xbps package",
          all(re.search(rf"^{re.escape(p)}-\d", manual, re.M) for p in packages))
    check("package record survives uninstalls", Path("/var/lib/spaces/.host/dev-install.packages").read_text().split() == sorted(packages))
    uninstall = (ROOT / "void/tools/dev-uninstall.sh").read_text()
    check("dev-uninstall.sh never deletes .host explicitly (only --purge removes /var/lib/spaces)",
          not re.search(r"^\s*rm .*\.host", uninstall, re.M))
    out = sudo("/usr/lib/spaces/void/bin/dnf5", "--releasever=44", "--version", timeout=120)
    check("dnf5 shim runs with the tuning options", out.returncode == 0 and "dnf5 version" in out.stdout, out.stdout[-200:] + out.stderr[-200:])
    out = sudo("/usr/lib/spaces/void/bin/dnf5", "--releasever=44", "--setopt=retries=zzz", "makecache")
    check("the tuning options are real dnf5 options (a bad value is rejected by dnf5 itself)", "Invalid number option value" in out.stdout + out.stderr)
    if not reinstall:
        skip("uninstall keeps .host, reinstall merges the package record", "--reinstall not given")
        return
    stop_all()
    before = sudo("find", "/var/lib/spaces/.host", "-maxdepth", "3").stdout
    un = sudo("/bin/bash", str(ROOT / "void/tools/dev-uninstall.sh"), timeout=300)
    after = sudo("find", "/var/lib/spaces/.host", "-maxdepth", "3").stdout
    check("dev-uninstall.sh succeeds", un.returncode == 0, un.stdout[-300:] + un.stderr[-300:])
    check("/var/lib/spaces/.host (keyring, fedora bootstrap, nvidia farm) is kept", before == after and "gnupg" in after and "fedora/44" in after, after[-300:])
    check("spaces and the package record are kept",
          all((STATE / n / "info.json").exists() for n in SPACES) and Path("/var/lib/spaces/.host/dev-install.packages").exists())
    ins = sudo("/bin/bash", str(ROOT / "void/tools/dev-install.sh"), timeout=900)
    check("dev-install.sh succeeds again", ins.returncode == 0, ins.stdout[-300:] + ins.stderr[-300:])
    state = Path("/etc/spaces/dev-install.state").read_text()
    check("state after reinstall still lists pam-devel, pacman, m4", all(p in state for p in ("pam-devel", "pacman", "m4")), state)
    check("autostart service dir reinstalled and not linked", Path("/etc/sv/spaces-autostart/run").exists() and not AUTOSTART_LINK.exists())


def check_doctor_gc() -> None:
    print("\n== doctor and gc ==", flush=True)
    out = sudo("spaces-void", "doctor")
    check("doctor: no FAIL", out.returncode == 0 and re.search(r"^PASS +libelogind", out.stdout, re.M) is not None, out.stdout[-400:])
    check("doctor: prints PASS lines for lxc, apparmor profile, cgroup, polkit, service sanity, nvidia sync",
          all(re.search(rf"^PASS +{k}", out.stdout, re.M) for k in ("lxc", "apparmor profile loaded", "cgroup layout", "polkitd", "service ubuntu", "nvidia sync")), out.stdout[-400:])
    # A service of a space that no longer exists.
    stray = "/etc/sv/spaces-m7stray"
    sudo("mkdir", "-p", stray + "/log")
    out = run(["spaces-void", "gc", "--dry-run"])
    check("gc --dry-run lists the stray service and removes nothing", "m7stray" in out.stdout and os.path.isdir(stray), out.stdout)
    sudo("sh", "-c", "ln -s /etc/sv/spaces-m7stray /var/service/spaces-m7stray")
    out = sudo("spaces-void", "gc")
    check("gc removes the stray service dir and link", "m7stray" in out.stdout and not os.path.exists(stray) and not os.path.lexists("/var/service/spaces-m7stray"), out.stdout + out.stderr)
    check("gc keeps real spaces and spaces-autostart", all(Path(f"/etc/sv/spaces-{n}").is_dir() for n in SPACES) and Path("/etc/sv/spaces-autostart").is_dir())
    sudo("rm", "-rf", stray, "/run/runit/supervise.spaces-m7stray", "/run/runit/supervise.spaces-m7stray.log")


# ------------------------------------------------------------------- cleanup


def check_clean() -> None:
    print("\n== clean machine ==", flush=True)
    stop_all()
    wait_for(lambda: run(["pgrep", "-x", "lxc-start"]).returncode != 0, 60)
    check("all spaces stopped", all(stopped(n) for n in SPACES) and run(["pgrep", "-x", "lxc-start"]).returncode != 0)
    check("autostart service unlinked", not AUTOSTART_LINK.exists())
    mounts = Path("/proc/mounts").read_text()
    check("no space mounts left", not re.search(r" /(var/lib|run)/spaces/", mounts), "")
    check("no stray cgroups", not sudo("sh", "-c", "ls -d /sys/fs/cgroup/spaces/* 2>/dev/null").stdout.strip())
    check("cgroup.subtree_control empty", Path("/sys/fs/cgroup/cgroup.subtree_control").read_text().strip() == "")
    watchdog = Path("/sys/class/watchdog/watchdog0/state")
    if watchdog.exists():
        check("watchdog inactive", watchdog.read_text().strip() == "inactive")
    else:
        skip("watchdog inactive", "no watchdog on this host")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skip-flavor-install", action="store_true")
    parser.add_argument("--reinstall", action="store_true", help="also run dev-uninstall.sh and dev-install.sh")
    arguments = parser.parse_args()
    if os.geteuid() == 0:
        print("run as the normal user", file=sys.stderr)
        return 2
    if sudo("true").returncode != 0:
        print("needs passwordless sudo", file=sys.stderr)
        return 2
    try:
        check_entry()
        check_autostart()
        check_flavor(not arguments.skip_flavor_install)
        check_doctor_gc()
        check_housekeeping(arguments.reinstall)  # last: a reinstall removes the spaces' services
    finally:
        check_clean()
    print(f"\n{results['PASS']} PASS, {results['FAIL']} FAIL, {results['SKIP']} SKIP")
    return 1 if results["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
