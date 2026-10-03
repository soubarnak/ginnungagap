"""LXC host backend (stub; implemented in milestone M2)."""

from __future__ import annotations

from typing import Any

from .base import HostBackend


def _unimplemented(self: Any, *args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError("M2")


class LxcBackend(HostBackend):
    """Every operation raises NotImplementedError until M2."""

    is_running = _unimplemented
    start_unit = _unimplemented
    stop_unit = _unimplemented
    try_restart_unit = _unimplemented
    enable_user_autostart = _unimplemented
    run_launcher = _unimplemented
    probe_registered = _unimplemented
    probe_guest_shell = _unimplemented
    exec_in_guest = _unimplemented
    spawn_in_guest = _unimplemented
    bind_into = _unimplemented
    unmount_in = _unimplemented
    set_device_policy = _unimplemented
    login_library_names = _unimplemented
    host_user_environment = _unimplemented
    session_bus_address = _unimplemented
    spawn_user_scope = _unimplemented
    peer_in_space = _unimplemented
