"""Host backend selection."""

from __future__ import annotations

import os

from .base import HostBackend

_backend: HostBackend | None = None


def _select() -> HostBackend:
    choice = os.environ.get("SPACES_HOST_BACKEND")
    if choice is None:
        choice = (
            "systemd" if os.path.isdir("/run/systemd/system") else "lxc"
        )
    if choice == "systemd":
        from .systemd import SystemdBackend

        return SystemdBackend()
    if choice == "lxc":
        from .lxc import LxcBackend

        return LxcBackend()
    raise ValueError(f"Unknown SPACES_HOST_BACKEND: {choice!r}")


def get_backend() -> HostBackend:
    """Return the cached host backend, selecting it on first use."""

    global _backend
    if _backend is None:
        _backend = _select()
    return _backend


def set_backend(backend: HostBackend | None) -> None:
    """Install a backend, or None to reselect on the next get_backend()."""

    global _backend
    _backend = backend


__all__ = ["HostBackend", "get_backend", "set_backend"]
