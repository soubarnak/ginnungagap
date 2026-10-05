#!/usr/bin/env python3
"""M4 check: desktop session integration on this Void machine.

Run as the normal user from the niri session (needs passwordless sudo and the
`ubuntu` space from M3):

    python3 void/spike/m4_check.py [--with-apt]

Prints PASS/FAIL/SKIP per item and exits non-zero when anything failed.
GUI items use `niri msg`; without a graphical session they are skipped.
--with-apt also installs/removes gnome-calculator in the guest to check the
application menu export and its removal (needs network, takes a minute).
The check only leaves a throwaway window open for a few seconds and uses the
primary selection (not the clipboard) for the clipboard probe.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_lock  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SPACE = "ubuntu"
SERVICE = f"/var/service/spaces-{SPACE}"
LOG = f"/var/log/spaces/{SPACE}/current"
UID = os.getuid()
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{UID}"))
APPS = Path("/usr/local/share/applications")
APP = "gnome-calculator"
APP_ID = "org.gnome.Calculator"
PROXY = rf"xdg-dbus-proxy .*/run/spaces/{SPACE}/desktop"
BROKER = rf"spaces-broker .*--space {SPACE}\b"
LXC = rf"lxc-start .*-n {SPACE}\b"
ALL = f"{PROXY}|{BROKER}|{LXC}"
NOISE = re.compile(r"Timed out|Could not enable host portals|WARNING|ERROR|Traceback")
results: list[tuple[str, str, str]] = []


def report(label: str, status: str, detail: str = "") -> bool:
    results.append((label, status, detail))
    print(f"{status:4}  {label}  {detail}", flush=True)
    return status == "PASS"


def check(label: str, condition: object, detail: str = "") -> bool:
    return report(label, "PASS" if condition else "FAIL", detail)


def skip(label: str, why: str) -> None:
    report(label, "SKIP", why)


def run(argv, *, timeout=120, input=None, env=None):
    try:
        return subprocess.run(
            argv,
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        out = error.stdout or ""
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return subprocess.CompletedProcess(argv, 124, out + "\n[timeout]")


def sudo(*argv, **kw):
    return run(["sudo", "-n", *argv], **kw)


def sudo_python(code: str):
    return sudo("/usr/bin/python3", "-I", "-c", code)


def guest(command: str, **kw):
    return run(["spaces", "enter", SPACE, "--", "sh", "-c", command], **kw)


def wait_for(predicate, timeout, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def log_size() -> int:
    done = sudo("stat", "-c", "%s", LOG)
    return int(done.stdout.strip()) if done.returncode == 0 else 0


def log_since(offset: int) -> str:
    return sudo("tail", "-c", f"+{offset + 1}", LOG).stdout


def helpers(pattern: str) -> list[str]:
    out = run(["ps", "-eo", "pid=,stat=,user=,args="]).stdout
    return [line for line in out.splitlines() if re.search(pattern, line)]


def graphical() -> bool:
    return bool(
        os.environ.get("WAYLAND_DISPLAY")
        and shutil.which("niri")
        and run(["niri", "msg", "version"]).returncode == 0
    )


def niri_windows() -> list[dict]:
    done = run(["niri", "msg", "--json", "windows"])
    try:
        return json.loads(done.stdout)
    except ValueError:
        return []


# ------------------------------------------------------------------- items


def check_session_env() -> None:
    published = RUNTIME / "spaces" / "environment"
    published.unlink(missing_ok=True)
    done = run(["spaces-session-env", "publish"])
    status = published.lstat() if published.exists() else None
    check(
        "1a publish: file written 0600, owned by the user",
        done.returncode == 0
        and status is not None
        and stat.S_IMODE(status.st_mode) == 0o600
        and status.st_uid == UID,
        done.stdout.strip()[:90],
    )
    text = published.read_text() if status else ""
    check(
        "1a publish: bus, display and session id are in the file",
        all(key in text for key in ("DBUS_SESSION_BUS_ADDRESS=", "XDG_SESSION_ID=", "WAYLAND_DISPLAY="))
        and "PATH=" not in text,
    )
    show = run(["spaces-session-env", "show"])
    check("1a show: published file reported current", "(current)" in show.stdout, show.stdout.splitlines()[0][:80])
    code = (
        "from spaces.host.lxc import LxcBackend; "
        f"b = LxcBackend(); print(b.host_user_environment({UID}, {UID}) or 'NONE')"
    )
    with_file = sudo_python(code).stdout
    check("1b root reads the published file", "WAYLAND_DISPLAY=" in with_file and "DBUS_SESSION_BUS_ADDRESS=" in with_file)
    published.unlink()
    fallback = sudo_python(code).stdout
    check(
        "1b fallback: no file, root scan of the elogind session finds the environment",
        "WAYLAND_DISPLAY=" in fallback
        and "DBUS_SESSION_BUS_ADDRESS=" in fallback
        and re.search(r"^XDG_SESSION_ID=\S+$", fallback, re.M) is not None
        and "SSH_" not in fallback.replace("SSH_AUTH_SOCK", ""),
        f"{len(fallback.splitlines())} variables",
    )
    check(
        "1b fallback: a user without a graphical session yields NONE",
        sudo_python(
            "from spaces.host.lxc import LxcBackend; "
            "print(LxcBackend().host_user_environment(65534, 65534) or 'NONE')"
        ).stdout.strip()
        == "NONE",
    )


def check_bus_link() -> None:
    link = RUNTIME / "bus"
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    real = re.search(r"unix:path=([^,]+)", address)
    if real is None:
        skip("2 bus symlink", f"session bus is not a path socket: {address[:40]}")
        return
    sudo("rm", "-f", str(link))
    done = sudo_python(
        "from spaces.host.lxc import LxcBackend; "
        f"print(LxcBackend().session_bus_address({UID}))"
    )
    status = os.lstat(link) if os.path.lexists(link) else None
    check(
        "2 symlink: created owned by the user, 0777, to the real socket",
        status is not None
        and stat.S_ISLNK(status.st_mode)
        and status.st_uid == UID
        and os.readlink(link) == real.group(1)
        and done.stdout.strip() == f"unix:path={link}",
        done.stdout.strip()[-60:],
    )
    reply = run(
        ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
         "--object-path", "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.GetId"],
        env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={link}"},
    )
    check("2 symlink: connects to the session bus", reply.returncode == 0, reply.stdout.strip()[:60])
    sudo("rm", "-f", str(link))
    sudo("ln", "-s", "/tmp/dbus-does-not-exist", str(link))
    sudo_python(
        "from spaces.host.lxc import LxcBackend; "
        f"LxcBackend().session_bus_address({UID})"
    )
    check("2 symlink: dangling link is repointed", os.path.exists(link) and os.readlink(link) == real.group(1))


def start_space_and_measure() -> int:
    sudo("sv", "down", SERVICE)
    wait_for(lambda: not helpers(ALL), 60)
    offset = log_size()
    done = run(["spaces", "enter", SPACE, "--", "true"], timeout=150)
    check("5 terminal enter starts the space and works", done.returncode == 0, done.stdout.strip()[:80])
    return offset


def check_helpers(offset: int) -> None:
    time.sleep(16)  # three reconcile rounds
    check(
        "3 proxy and broker run as the user",
        len(helpers(rf"{UID_NAME}\s.*{PROXY}")) == 1
        and len(helpers(rf"{UID_NAME}\s.*{BROKER}")) == 1,
        f"{len(helpers(PROXY))} proxy, {len(helpers(BROKER))} broker",
    )
    noise = [line for line in log_since(offset).splitlines() if NOISE.search(line)]
    check("3 launcher log has no warnings or timeouts after start", not noise, noise[0][:100] if noise else "")
    check(
        "3 no zombie processes under the launcher",
        not [line for line in helpers(r"\sZ[a-z+]*\s") if "python" in line or "spaces" in line],
    )


def check_guest_helpers() -> None:
    tool = ROOT / "void" / "tools" / "check-guest-glib.py"
    done = sudo("/usr/bin/python3", str(tool))
    check("4 guest GLib provides every symbol the helpers use", done.returncode == 0, done.stdout.strip().replace("\n", "; ")[:110])
    done = guest("for f in /run/spaces-host/bin/*; do ldd $f | grep 'not found'; done; echo end")
    check("4 helpers link in the guest (ldd: nothing missing)", done.stdout.strip().endswith("end") and "not found" not in done.stdout)
    done = sudo_python(
        "from pathlib import Path; from spaces import auth; "
        f"auth.validate_native_runtime(Path('/var/lib/spaces/{SPACE}/rootfs')); print('ok')"
    )
    check("4 validate_native_runtime (check_guest_abi bundle) passes", done.stdout.strip() == "ok", done.stdout.strip()[-80:])


def check_desktop_environment() -> None:
    done = guest("echo $WAYLAND_DISPLAY $XDG_CURRENT_DESKTOP $GTK_USE_PORTAL $DBUS_SESSION_BUS_ADDRESS; test -S $WAYLAND_DISPLAY && echo sock")
    check(
        "5 guest env: Wayland socket bound, portals on, bus path",
        "/wayland/" in done.stdout and "Spaces:" in done.stdout and "sock" in done.stdout and "/run/user/" in done.stdout,
        done.stdout.split("\n")[0][:80],
    )
    done = guest("ls /run/spaces/desktop/*/pulse/native /run/spaces/desktop/*/pipewire/pipewire-0 /run/spaces/desktop/*/portal/bus")
    check("5 guest sees pulse, pipewire and the filtered portal bus", done.returncode == 0, done.stdout.strip().replace("\n", " ")[:90])


def check_gui() -> None:
    if not graphical():
        for item in ("6 window on niri", "6 audio", "6 clipboard", "6 notification", "6 portal"):
            skip(item, "no graphical niri session in this environment")
        return
    before = {w["id"] for w in niri_windows()}
    process = subprocess.Popen(
        ["spaces", "enter", "--graphical", SPACE, "--", APP],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    window = None
    for _ in range(30):
        time.sleep(1)
        window = next((w for w in niri_windows() if w["id"] not in before and w.get("app_id") == APP_ID), None)
        if window:
            break
    check("6 window: guest app opens on the host niri desktop", window is not None, f"app_id={window['app_id']} title={window['title']}" if window else "")
    if window:
        run(["niri", "msg", "action", "close-window", "--id", str(window["id"])])
        closed = wait_for(lambda: process.poll() is not None, 15)
        if not closed and session_lock.locked():  # a locked niri delivers no close-window
            skip("6 window: closes", session_lock.SKIP_REASON)
        else:
            check("6 window: closes", closed)
    if process.poll() is None:
        process.terminate()
    process.communicate()

    done = guest("pactl info | grep -E 'Server Name|Server String'; pactl list short sinks | wc -l")
    check("6 audio: guest pactl reaches host PipeWire", "PipeWire" in done.stdout, done.stdout.strip().replace("\n", " | ")[:100])

    token = f"m4-{os.getpid()}"
    guest(f"printf %s {token} | wl-copy --primary")
    pasted = run(["wl-paste", "--primary", "--no-newline"], timeout=10)
    check("6 clipboard: guest wl-copy (primary selection) is visible to the host", pasted.stdout == token, pasted.stdout[:30])
    run(["wl-copy", "--primary", "--clear"], timeout=10)

    log = tempfile.NamedTemporaryFile("w+", delete=False)
    monitor = subprocess.Popen(
        ["dbus-monitor", "--session", "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"],
        stdout=log, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    guest("notify-send -a spaces-m4 'Spaces M4 check' 'notification from the guest'")
    time.sleep(2)
    monitor.terminate()
    monitor.wait()
    seen = "spaces-m4" in Path(log.name).read_text()
    os.unlink(log.name)
    check("6 notification: guest notify-send reaches the host notification service", seen)

    done = guest(
        "gdbus call --session --dest org.freedesktop.portal.Desktop --object-path /org/freedesktop/portal/desktop "
        "--method org.freedesktop.portal.OpenURI.SchemeSupported https '{}'"
    )
    check("6 portal: OpenURI.SchemeSupported answered by the host portal", "(true,)" in done.stdout, done.stdout.strip()[-50:])


def check_shortcuts(with_apt: bool) -> None:
    entry = APPS / f"spaces-{SPACE}-v1-{APP_ID}.desktop"
    icon = APPS / "spaces-icons" / f"spaces-{SPACE}-v1-{APP_ID}.png"
    present = guest(f"dpkg -s {APP} >/dev/null 2>&1 && echo yes").stdout.strip().endswith("yes")
    if with_apt:
        sudo("spaces", "enter", SPACE, "--root", "--", "apt-get", "remove", "-y", APP, timeout=300)
        check("7 menu: entry removed after the app is uninstalled", wait_for(lambda: not entry.exists() and not icon.exists(), 40, 2))
        done = sudo("spaces", "enter", SPACE, "--root", "--", "sh", "-c",
                    f"DEBIAN_FRONTEND=noninteractive apt-get install -y {APP}", timeout=600)
        present = done.returncode == 0
        wait_for(entry.exists, 40, 2)
    if not present:
        skip("7 menu", f"{APP} is not installed in the guest")
        return
    text = entry.read_text() if entry.exists() else ""
    check("7 menu: .desktop entry exported with a distro-suffixed name", f"Name=Calculator ({SPACE})" in text, entry.name)
    check("7 menu: Exec goes through `spaces enter --graphical`", f"Exec=/usr/bin/spaces enter --graphical {SPACE} -- {APP}" in text)
    check("7 menu: badged icon exists (256x256 PNG)", icon.exists() and icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n")
    found = run([
        "python3", "-c",
        "import gi; gi.require_version('Gio','2.0'); from gi.repository import Gio; "
        f"print(Gio.DesktopAppInfo.new('{entry.name}') is not None)",
    ]).stdout
    check("7 menu: GLib application lookup (default XDG_DATA_DIRS) finds it", found.strip().endswith("True"), os.environ.get("XDG_DATA_DIRS") or "XDG_DATA_DIRS unset: defaults include /usr/local/share")


def check_final() -> None:
    sudo("sv", "down", SERVICE)
    ok = wait_for(lambda: not helpers(ALL), 90)
    left = helpers(ALL)
    check("9 stop: no proxy, broker or lxc-start left", ok, "; ".join(left)[:100])
    subtree = Path("/sys/fs/cgroup/cgroup.subtree_control").read_text().strip()
    mounts = sudo("cat", "/proc/mounts").stdout
    check("9 stop: cgroup subtree_control empty, no spaces cgroup", subtree == "" and not Path("/sys/fs/cgroup/spaces").exists(), repr(subtree))
    check("9 stop: no space mounts left", f"/var/lib/spaces/{SPACE}" not in mounts and "/run/spaces/desktop" not in mounts)
    zombies = [line for line in helpers(r"\sZ[a-z+]*\s") if "spaces" in line or "xdg-dbus" in line]
    check("9 stop: no zombies", not zombies, "; ".join(zombies)[:100])


UID_NAME = pwd.getpwuid(UID).pw_name


def main() -> int:
    if os.geteuid() == 0:
        print("run as the normal user", file=sys.stderr)
        return 2
    if sudo("true").returncode != 0:
        print("passwordless sudo is required", file=sys.stderr)
        return 2
    if not shutil.which("spaces-session-env"):
        print("spaces-session-env missing: run sudo void/tools/dev-install.sh", file=sys.stderr)
        return 2
    with_apt = "--with-apt" in sys.argv
    if graphical() or os.environ.get("XDG_SESSION_ID"):
        check_session_env()
        check_bus_link()
    else:
        skip("1/2 session environment and bus link", "no graphical session in this environment")
    offset = start_space_and_measure()
    check_helpers(offset)
    check_guest_helpers()
    check_desktop_environment()
    check_gui()
    check_shortcuts(with_apt)
    check_final()
    failed = [label for label, status, _ in results if status == "FAIL"]
    passed = sum(status == "PASS" for _, status, _ in results)
    skipped = sum(status == "SKIP" for _, status, _ in results)
    print(f"\n{passed} passed, {len(failed)} failed, {skipped} skipped")
    for label in failed:
        print("FAILED:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
