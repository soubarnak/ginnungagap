"""Host backend interface.

Every host-side call that upstream makes through systemd (machinectl,
systemctl, systemd-run, busctl, cgroup inspection) goes through this
interface so that another init system can provide an equivalent.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any


class HostBackend(ABC):
    """Host-side service, guest-exec and mount operations for spaces."""

    @abstractmethod
    def is_running(self, name: str) -> bool:
        """Return whether the space is registered and running."""

    @abstractmethod
    def start_unit(self, name: str) -> int:
        """Start the space's service and return its exit status."""

    @abstractmethod
    def stop_unit(self, name: str) -> int:
        """Stop the space's service.

        Raises CalledProcessError on failure; returns 0 otherwise.
        """

    @abstractmethod
    def try_restart_unit(self, name: str) -> int:
        """Restart the space's service if it runs.

        Raises CalledProcessError on failure; returns 0 otherwise.
        """

    @abstractmethod
    def forget_unit(self, name: str) -> None:
        """Remove any init-system service definition for a deleted space."""

    @abstractmethod
    def enable_user_autostart(self, user_name: str, name: str) -> None:
        """Enable the space's per-user autostart.

        Raises CalledProcessError on failure.
        """

    @abstractmethod
    def run_launcher(
        self, argv: Sequence[str], env: Mapping[str, str] | None
    ) -> subprocess.Popen[Any]:
        """Start the container launcher process (the nspawn equivalent)."""

    @abstractmethod
    def probe_registered(self, name: str) -> bool:
        """Return whether the running space is registered with the host."""

    @abstractmethod
    def probe_guest_shell(self, name: str) -> bool:
        """Return whether a root command can already run in the guest."""

    @abstractmethod
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
        """Run a command in the guest as user_name and wait for it.

        The environment is applied to the command only. stdout and stderr
        are forwarded to subprocess.run only when not None.
        """

    @abstractmethod
    def spawn_in_guest(
        self,
        user_name: str,
        name: str,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[Any]:
        """Like exec_in_guest, but start the command without waiting."""

    @abstractmethod
    def bind_into(
        self,
        name: str,
        source: str,
        destination: str,
        *,
        read_only: bool = False,
        mkdir: bool = True,
    ) -> None:
        """Bind a host path into the running guest.

        Raises CalledProcessError on failure.
        """

    @abstractmethod
    def unmount_in(self, name: str, destination: str) -> None:
        """Lazily unmount a guest path. Raises CalledProcessError."""

    @abstractmethod
    def set_device_policy(
        self,
        name: str,
        level: str,
        allow: Sequence[tuple[str, str]],
    ) -> None:
        """Replace the guest device policy.

        level "full" is unrestricted and ignores allow; any other level
        permits only the (device, permissions) pairs in allow. Raises
        CalledProcessError on failure.
        """

    @abstractmethod
    def login_library_names(self) -> tuple[str, ...]:
        """Return (name for ctypes.util.find_library, fallback soname)."""

    @abstractmethod
    def host_user_environment(self, uid: int, gid: int) -> str | None:
        """Return the host user manager's raw environment block.

        None means it could not be read.
        """

    @abstractmethod
    def session_bus_address(self, uid: int) -> str:
        """Return the host session bus address for a user."""

    @abstractmethod
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
        """Start argv in a transient per-user scope named unit."""

    @abstractmethod
    def peer_in_space(self, pid: int, name: str) -> bool:
        """Return whether a host process belongs to the space."""

    def space_created(self, name: str, isolate: bool = True) -> None:
        """Called by create once info.json is written, before the rootfs is bootstrapped.

        isolate is False when the user passed --no-userns. Backends with nothing to prepare keep this.
        """

    def guest_root_uid(self, name: str) -> int | None:
        """What root in the space is on the host when it has a user namespace, else None."""

        return None
