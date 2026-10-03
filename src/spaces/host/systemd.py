"""systemd host backend: the commands upstream issues directly."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .base import HostBackend

NSPAWN = "/usr/bin/systemd-nspawn"
MACHINECTL = "/usr/bin/machinectl"
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
BUSCTL = "/usr/bin/busctl"
UMOUNT = "/usr/bin/umount"


def _cgroup_components(pid: int) -> set[str]:
    value = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    return {
        component
        for line in value.splitlines()
        for component in line.partition(":")[2].split("/")
        if component
    }


def _shell_command(
    user_name: str,
    name: str,
    command: Sequence[str],
    env: Mapping[str, str] | None,
) -> list[str]:
    return [
        MACHINECTL,
        "--quiet",
        f"--uid={user_name}",
        *(
            f"--setenv={key}={value}"
            for key, value in sorted((env or {}).items())
        ),
        "--",
        "shell",
        name,
        *command,
    ]


class SystemdBackend(HostBackend):
    def is_running(self, name: str) -> bool:
        return subprocess.run(
            [MACHINECTL, "--quiet", "show", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0

    def start_unit(self, name: str) -> int:
        return subprocess.run(
            [SYSTEMCTL, "start", f"spaces@{name}.service"],
            check=False,
        ).returncode

    def stop_unit(self, name: str) -> int:
        return subprocess.run(
            [SYSTEMCTL, "stop", f"spaces@{name}.service"],
            check=True,
        ).returncode

    def try_restart_unit(self, name: str) -> int:
        return subprocess.run(
            [SYSTEMCTL, "try-restart", f"spaces@{name}.service"],
            check=True,
        ).returncode

    def enable_user_autostart(self, user_name: str, name: str) -> None:
        # Reenable also removes symlinks left under the former default.target.
        subprocess.run(
            [
                SYSTEMCTL,
                f"--machine={user_name}@.host",
                "--user",
                "--no-reload",
                "reenable",
                f"spaces@{name}.service",
            ],
            check=True,
        )

    def run_launcher(
        self, argv: Sequence[str], env: Mapping[str, str] | None
    ) -> subprocess.Popen[Any]:
        return subprocess.Popen(argv, env=env)

    def probe_registered(self, name: str) -> bool:
        return subprocess.run(
            [
                MACHINECTL,
                "--quiet",
                "--no-ask-password",
                "show",
                "--property=Leader",
                "--value",
                name,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0

    def probe_guest_shell(self, name: str) -> bool:
        return subprocess.run(
            [
                MACHINECTL,
                "--quiet",
                "--no-ask-password",
                "--uid=root",
                "--",
                "shell",
                name,
                "/usr/bin/true",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0

    def exec_in_guest(
        self,
        user_name: str,
        name: str,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = False,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.CompletedProcess[Any]:
        options: dict[str, Any] = {}
        if stdout is not None:
            options["stdout"] = stdout
        if stderr is not None:
            options["stderr"] = stderr
        return subprocess.run(
            _shell_command(user_name, name, command, env),
            check=check,
            **options,
        )

    def spawn_in_guest(
        self,
        user_name: str,
        name: str,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[Any]:
        return subprocess.Popen(_shell_command(user_name, name, command, env))

    def bind_into(
        self,
        name: str,
        source: str,
        destination: str,
        *,
        read_only: bool = False,
        mkdir: bool = True,
    ) -> None:
        subprocess.run(
            [
                MACHINECTL,
                "--quiet",
                "--no-ask-password",
                *(("--mkdir",) if mkdir else ()),
                *(("--read-only",) if read_only else ()),
                "bind",
                name,
                source,
                destination,
            ],
            check=True,
        )

    def unmount_in(self, name: str, destination: str) -> None:
        # LazyUnmount= is a mount-unit setting, but systemd does not expose
        # it through systemctl set-property. Run the stable umount(8)
        # interface in the guest manager instead; every supported systemd
        # has these systemd-run options (--pipe is the newest, systemd 235).
        subprocess.run(
            [
                SYSTEMD_RUN,
                f"--machine={name}",
                "--no-ask-password",
                "--quiet",
                "--wait",
                "--pipe",
                "--collect",
                "--service-type=exec",
                "--",
                UMOUNT,
                "--lazy",
                "--",
                destination,
            ],
            check=True,
        )

    def set_device_policy(
        self,
        name: str,
        level: str,
        allow: Sequence[tuple[str, str]],
    ) -> None:
        if level == "full":
            policy = "auto"
            allow = ()
        else:
            policy = "closed"
        subprocess.run(
            [
                BUSCTL,
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "SetUnitProperties",
                "sba(sv)",
                f"spaces@{name}.service",
                "true",
                "2",
                "DevicePolicy",
                "s",
                policy,
                "DeviceAllow",
                "a(ss)",
                str(len(allow)),
                *(value for pair in allow for value in pair),
            ],
            check=True,
        )

    def login_library_names(self) -> tuple[str, ...]:
        return ("systemd", "libsystemd.so.0")

    def session_bus_address(self, uid: int) -> str:
        return f"unix:path=/run/user/{uid}/bus"

    def host_user_environment(self, uid: int, gid: int) -> str | None:
        # Do not use ``--machine=<user>@.host`` here. That transport starts a
        # systemd-stdio-bridge PAM session; logind then wakes the monitor
        # again, turning one environment read into an unbounded
        # reconciliation loop.
        completed = subprocess.run(
            [
                SYSTEMCTL,
                "--user",
                "--no-ask-password",
                "show-environment",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                "DBUS_SESSION_BUS_ADDRESS": self.session_bus_address(uid),
                "XDG_RUNTIME_DIR": f"/run/user/{uid}",
            },
            user=uid,
            group=gid,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout

    def spawn_user_scope(
        self,
        unit: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        description: str,
        uid: int,
        gid: int,
        pass_fds: Sequence[int] = (),
    ) -> subprocess.Popen[Any]:
        return subprocess.Popen(
            [
                SYSTEMD_RUN,
                "--user",
                "--scope",
                "--quiet",
                f"--unit={unit}",
                f"--description={description}",
                "--",
                *argv,
            ],
            env=env,
            user=uid,
            group=gid,
            pass_fds=tuple(pass_fds),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def peer_in_space(self, pid: int, name: str) -> bool:
        try:
            components = _cgroup_components(pid)
        except OSError:
            return False
        return (
            f"spaces@{name}.service" in components
            or f"machine-{name}.scope" in components
        )
