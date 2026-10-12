# SPDX-License-Identifier: Apache-2.0
"""Ginnungagap (ginn): isolated per-distribution Realms on rootless Podman for Void Linux."""

from importlib import metadata

__all__ = ["__version__"]

_FALLBACK_VERSION = "0.1.0.dev0"


def _read_version() -> str:
    try:
        return metadata.version("ginn")
    except metadata.PackageNotFoundError:
        return _FALLBACK_VERSION


__version__: str = _read_version()
