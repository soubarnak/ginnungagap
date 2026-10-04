"""`spaces-void`: administration commands of the Void port.

    spaces-void autostart list
    spaces-void autostart enable|disable NAME [--user USER] [--boot]
    spaces-void gc [--dry-run]
    spaces-void doctor
    spaces-void sync-config
    spaces-void install-flavor NAME [--flavor auto|kde|gtk]

Commands that change the system re-run themselves through sudo when started
as a normal user. Installed as /usr/bin/spaces-void (dev-install.sh).
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from . import autostart, flavor

NEEDS_ROOT = ("enable", "disable", "gc", "sync-config", "install-flavor")


def caller_uid() -> int:
    for variable in ("PKEXEC_UID", "SUDO_UID"):
        try:
            return int(os.environ[variable])
        except (KeyError, ValueError):
            continue
    return os.getuid()


def caller_name() -> str | None:
    try:
        return pwd.getpwuid(caller_uid()).pw_name
    except KeyError:
        return None


def _reexec_with_sudo(argv: Sequence[str]) -> int:
    program = shutil.which("spaces-void") or "/usr/bin/spaces-void"
    command = ["sudo", "--", program, *argv]
    print("spaces-void: this needs root, running through sudo", file=sys.stderr)
    return subprocess.call(command)


# ---------------------------------------------------------------- autostart


def cmd_autostart_list(root: Path = autostart.STATE_ROOT) -> int:
    names = autostart.space_names(root)
    if not names:
        print("no spaces")
        return 0
    service_linked = Path("/var/service", autostart.SERVICE_NAME).exists()
    print(f"{'SPACE':<16}{'BOOT':<6}USERS")
    for name in names:
        users = ",".join(autostart.read_users(name, root)) or "-"
        boot = "yes" if autostart.boot_enabled(name, root) else "no"
        print(f"{name:<16}{boot:<6}{users}")
    print(
        "autostart service: "
        + ("linked" if service_linked else "NOT linked (sudo ln -s /etc/sv/spaces-autostart /var/service/)")
    )
    return 0


def cmd_autostart_change(
    name: str,
    *,
    enable: bool,
    user: str | None,
    boot: bool,
    root: Path = autostart.STATE_ROOT,
) -> int:
    """enable/disable: the user switch (default) and/or the boot switch.

    `--boot` alone changes only the boot flag; with `--user` both change.
    Without `--boot` the user switch changes for --user or the calling user.
    """

    verb = "enabled" if enable else "disabled"
    try:
        if not boot or user:
            target = user or caller_name()
            if not target or caller_uid() == 0 and not user:
                print("spaces-void: pass --user USER", file=sys.stderr)
                return 2
            changed = autostart.set_user(name, target, enable, root)
            print(f"autostart for user {target} on {name}: {verb}" + ("" if changed else " (unchanged)"))
        if boot:
            changed = autostart.set_boot(name, enable, root)
            print(f"autostart at boot on {name}: {verb}" + ("" if changed else " (unchanged)"))
    except ValueError as error:
        print(f"spaces-void: {error}", file=sys.stderr)
        return 1
    if enable and not Path("/var/service", autostart.SERVICE_NAME).exists():
        print(
            "note: nothing happens until the service is linked: "
            "sudo ln -s /etc/sv/spaces-autostart /var/service/"
        )
    return 0


# --------------------------------------------------------------- the rest


def cmd_gc(dry_run: bool) -> int:
    removed = autostart.gc_services(dry_run=dry_run)
    for name in removed:
        print(("would remove" if dry_run else "removed") + f" service spaces-{name}")
    if not removed:
        print("nothing to remove")
    return 0


def cmd_doctor() -> int:
    from . import doctor

    results, failures = doctor.run()
    for status, name, detail in results:
        print(f"{status:<5} {name}: {detail}")
    warnings = sum(1 for status, _n, _d in results if status == "WARN")
    print(f"{len(results)} checks, {failures} FAIL, {warnings} WARN")
    if os.geteuid() != 0:
        print("(not root: some checks could not look closely; use sudo spaces-void doctor)")
    return 1 if failures else 0


def cmd_sync_config() -> int:
    from . import nvidia

    try:
        report = nvidia.sync(desktop_uid=caller_uid() or None)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"spaces-void: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    if report.get("config") == "kept":
        print(f"{nvidia.CONFIG_PATH} was edited by hand; the new version is {nvidia.CONFIG_PATH}.new")
    return 0


def cmd_install_flavor(name: str, choice: str | None, root: Path = autostart.STATE_ROOT) -> int:
    try:
        info = json.loads((root / name / "info.json").read_text(encoding="utf-8"))
        distro = info["distribution"]["id"]
    except (OSError, ValueError, KeyError, TypeError):
        print(f"spaces-void: no such space: {name}", file=sys.stderr)
        return 1
    setting = choice or flavor.configured_setting()
    try:
        chosen = flavor.resolve(setting, caller_uid() or None)
    except ValueError as error:
        print(f"spaces-void: {error}", file=sys.stderr)
        return 2
    packages = flavor.packages_for(chosen, distro)
    if not packages:
        print(f"flavour {chosen} adds nothing for {distro}")
        return 0
    command = flavor.install_command(distro, packages)
    if command is None:
        print(f"spaces-void: no install command for {distro}", file=sys.stderr)
        return 1
    print(f"installing the {chosen} flavour into {name} ({distro}): {' '.join(packages)}")
    # Through the normal entry path, which also starts the space.
    code = subprocess.call(["spaces", "enter", name, "--root", "--", *command])
    if code == 0:
        print(f"installed in {name}: {' '.join(packages)}")
    else:
        print(f"spaces-void: the package manager failed with status {code}", file=sys.stderr)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spaces-void", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    auto = sub.add_parser("autostart", help="manage automatic start of spaces")
    auto_sub = auto.add_subparsers(dest="action", required=True)
    auto_sub.add_parser("list", help="show what is enabled")
    for action in ("enable", "disable"):
        p = auto_sub.add_parser(action, help=f"{action} autostart for a space")
        p.add_argument("name")
        p.add_argument("--user", help="user (default: the caller)")
        p.add_argument("--boot", action="store_true", help="start at boot instead of at login")
    gc = sub.add_parser("gc", help="remove runit services of deleted spaces")
    gc.add_argument("--dry-run", "-n", action="store_true")
    sub.add_parser("doctor", help="check the installation")
    sub.add_parser("sync-config", help="regenerate /etc/spaces/config.json")
    flavor_parser = sub.add_parser("install-flavor", help="install the desktop flavour packages into a space")
    flavor_parser.add_argument("name")
    flavor_parser.add_argument("--flavor", choices=flavor.FLAVORS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    arguments = build_parser().parse_args(argv)
    command = arguments.command
    action = getattr(arguments, "action", None)
    changing = command in NEEDS_ROOT or (command == "autostart" and action in NEEDS_ROOT)
    if command == "gc" and arguments.dry_run:
        changing = False
    if changing and os.geteuid() != 0:
        return _reexec_with_sudo(argv)
    if command == "autostart":
        if action == "list":
            return cmd_autostart_list()
        return cmd_autostart_change(
            arguments.name,
            enable=action == "enable",
            user=arguments.user,
            boot=arguments.boot,
        )
    if command == "gc":
        return cmd_gc(arguments.dry_run)
    if command == "doctor":
        return cmd_doctor()
    if command == "sync-config":
        return cmd_sync_config()
    return cmd_install_flavor(arguments.name, arguments.flavor)


if __name__ == "__main__":
    sys.exit(main())
