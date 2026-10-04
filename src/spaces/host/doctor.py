"""`spaces-void doctor`: sanity checks of the Void installation.

Every check yields (status, name, detail) with status PASS, WARN or FAIL.
Checks that need root to look (AppArmor profile list, runsv state) report
WARN when run unprivileged; run `sudo spaces-void doctor` for the full set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

from . import autostart

Result = tuple[str, str, str]

CGROUP = Path("/sys/fs/cgroup")
APPARMOR_PROFILES = Path("/sys/kernel/security/apparmor/profiles")
APPARMOR_ENABLED = Path("/sys/module/apparmor/parameters/enabled")
PROFILE = "spaces-container"
SVDIR = Path("/etc/sv")
SERVICE_DIR = Path("/var/service")
CONFIG = Path("/etc/spaces/config.json")


def _run(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return None


def check_lxc() -> Iterator[Result]:
    wrapper = Path("/usr/lib/spaces/spaces-lxc")
    if shutil.which("lxc-start") is None:
        yield "FAIL", "lxc", "lxc-start is not installed (xbps-install lxc)"
        return
    result = _run(["lxc-start", "--version"])
    yield "PASS", "lxc", f"lxc {result.stdout.strip() if result else '?'}"
    if wrapper.exists() and os.access(wrapper, os.X_OK):
        yield "PASS", "lxc wrapper", str(wrapper)
    else:
        yield "FAIL", "lxc wrapper", f"{wrapper} is missing: run dev-install.sh"


def check_apparmor() -> Iterator[Result]:
    try:
        enabled = APPARMOR_ENABLED.read_text().strip() == "Y"
    except OSError:
        enabled = False
    if not enabled:
        yield "FAIL", "apparmor", "AppArmor is not enabled in the kernel (apparmor=1 security=apparmor)"
        return
    yield "PASS", "apparmor", "enabled"
    if shutil.which("apparmor_parser") is None:
        yield "FAIL", "apparmor_parser", "not installed (xbps-install apparmor)"
    if not Path(f"/etc/apparmor.d/{PROFILE}").exists():
        yield "FAIL", "apparmor profile file", f"/etc/apparmor.d/{PROFILE} is missing"
    try:
        loaded = any(
            line.startswith(f"{PROFILE} ")
            for line in APPARMOR_PROFILES.read_text().splitlines()
        )
    except PermissionError:
        yield "WARN", "apparmor profile loaded", "cannot read the profile list as this user; use sudo"
    except OSError as error:
        yield "WARN", "apparmor profile loaded", str(error)
    else:
        if loaded:
            yield "PASS", "apparmor profile loaded", PROFILE
        else:
            yield "WARN", "apparmor profile loaded", (
                f"{PROFILE} is not loaded; the launcher loads it on demand, or: "
                f"sudo apparmor_parser -r /etc/apparmor.d/{PROFILE}"
            )


def check_cgroups() -> Iterator[Result]:
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        mounts = []
    unified = any(line.split()[1:3] == [str(CGROUP), "cgroup2"] for line in mounts if line)
    yield ("PASS", "cgroup layout", "cgroup2 at /sys/fs/cgroup") if unified else (
        "FAIL", "cgroup layout", "/sys/fs/cgroup is not a cgroup2 mount"
    )
    try:
        delegated = (CGROUP / "cgroup.subtree_control").read_text().strip()
    except OSError:
        delegated = None
    if delegated is None:
        yield "WARN", "cgroup.subtree_control", "unreadable"
    elif delegated:
        yield "WARN", "cgroup.subtree_control", (
            f"root delegates '{delegated}'; elogind and LXC expect it empty"
        )
    else:
        yield "PASS", "cgroup.subtree_control", "empty"
    hybrid = any(" cgroup " in line and "name=elogind" in line for line in mounts)
    if hybrid:
        yield "PASS", "elogind v1 hierarchy", "present; hidden from LXC by spaces-lxc"


def check_elogind() -> Iterator[Result]:
    try:
        autostart.ElogindLogins()
    except OSError as error:
        yield "FAIL", "libelogind", str(error)
        return
    yield "PASS", "libelogind", "loads, login monitor works"
    result = _run(["pgrep", "-x", "elogind"])
    if result is not None and result.returncode == 0:
        yield "PASS", "elogind", "running"
    else:
        yield "WARN", "elogind", "no elogind process found"


def check_polkit() -> Iterator[Result]:
    result = _run(["pgrep", "-x", "polkitd"])
    if result is not None and result.returncode == 0:
        yield "PASS", "polkitd", "running"
    else:
        yield "FAIL", "polkitd", "not running (ln -s /etc/sv/polkitd /var/service/)"
    missing = [
        path
        for path in (
            "/usr/share/polkit-1/actions/org.anatase.spaces.policy",
            "/usr/bin/spaces.priv",
            "/usr/bin/spaces",
        )
        if not Path(path).exists()
    ]
    if missing:
        yield "FAIL", "installed files", f"missing: {', '.join(missing)}"
    else:
        yield "PASS", "installed files", "policy, spaces, spaces.priv present"


def _service_status(link: Path) -> str:
    result = _run(["sv", "status", str(link)])
    if result is None:
        return "unknown"
    return (result.stdout or result.stderr).strip()


def check_services(
    root: Path = autostart.STATE_ROOT,
    svdir: Path = SVDIR,
    service_dir: Path = SERVICE_DIR,
) -> Iterator[Result]:
    names = autostart.space_names(root)
    if not names:
        yield "WARN", "spaces", "no space exists yet (sudo spaces create ubuntu --preset basic)"
    for name in names:
        service = svdir / f"spaces-{name}"
        link = service_dir / f"spaces-{name}"
        if not service.is_dir():
            yield "WARN", f"service {name}", "no runit service yet (created on the first start)"
            continue
        problems = []
        for relative in ("run", "finish", "log/run"):
            path = service / relative
            if not (path.is_file() and os.access(path, os.X_OK)):
                problems.append(f"{relative} missing or not executable")
        if not (service / "down").exists():
            problems.append("no 'down' file (it would start at boot; run 'spaces enter' once to rewrite)")
        if not link.exists():
            problems.append("not linked into /var/service")
        if problems:
            yield "FAIL", f"service {name}", "; ".join(problems)
        else:
            yield "PASS", f"service {name}", "ok"
    for stale in autostart.gc_services(root=root, svdir=svdir, dry_run=True):
        yield "WARN", f"service {stale}", "space is gone, service remains: spaces-void gc"
    auto = service_dir / autostart.SERVICE_NAME
    wanted = [
        n for n in names if autostart.boot_enabled(n, root) or autostart.read_users(n, root)
    ]
    if auto.exists():
        yield "PASS", "autostart service", f"linked ({_service_status(auto)})"
    elif wanted:
        yield "WARN", "autostart service", (
            "spaces enable autostart (" + ", ".join(wanted) + ") but the service is not linked: "
            "sudo ln -s /etc/sv/spaces-autostart /var/service/"
        )
    else:
        yield "PASS", "autostart service", "not linked, nothing enabled"
    if "autostart" in names:
        yield "FAIL", "space name", "a space called 'autostart' collides with the spaces-autostart service"


def check_nvidia() -> Iterator[Result]:
    from . import nvidia

    loaded = nvidia.driver_version()
    if loaded is None:
        yield "PASS", "nvidia sync", "no NVIDIA driver loaded, nothing to do"
        return
    package = nvidia.xbps_version()
    farm = nvidia.FARM_ROOT / "current"
    if package is None:
        yield "WARN", "nvidia sync", f"driver {loaded} loaded but no nvidia-libs xbps package"
    elif not farm.exists():
        yield "FAIL", "nvidia sync", f"{farm} missing: sudo /usr/lib/spaces/spaces-nvidia-sync"
    elif not package.startswith(loaded):
        yield "WARN", "nvidia sync", f"module {loaded} differs from userspace {package}: reboot"
    else:
        yield "PASS", "nvidia sync", f"driver {loaded}, farm {farm.resolve().name}"


def check_config() -> Iterator[Result]:
    from . import nvidia

    try:
        text = CONFIG.read_text(encoding="utf-8")
    except OSError:
        yield "FAIL", "config.json", f"{CONFIG} is missing: sudo spaces-void sync-config"
        return
    problems = nvidia.validate(text)
    if problems:
        yield "FAIL", "config.json", "; ".join(problems)
    else:
        yield "PASS", "config.json", "valid"
    if CONFIG.with_name(CONFIG.name + ".new").exists():
        yield "WARN", "config.json.new", "a newer generated file exists (config.json was edited by hand)"


CHECKS: tuple[Callable[[], Iterator[Result]], ...] = (
    check_lxc,
    check_apparmor,
    check_cgroups,
    check_elogind,
    check_polkit,
    check_services,
    check_config,
    check_nvidia,
)


def run(checks: tuple[Callable[[], Iterator[Result]], ...] = CHECKS) -> tuple[list[Result], int]:
    """Run every check; return the results and the number of FAILs."""

    results: list[Result] = []
    for check in checks:
        try:
            results.extend(check())
        except Exception as error:  # noqa: BLE001 - a broken check is a finding
            results.append(("FAIL", check.__name__, f"check crashed: {error}"))
    return results, sum(1 for status, _n, _d in results if status == "FAIL")
