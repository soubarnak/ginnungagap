# SPDX-License-Identifier: Apache-2.0
"""Shared display helpers for the viewmodels. Pure Python, no GTK.

`Failure` is the one shape a view shows for an error. It comes from a CLI error object
(`CliResult.error`) or from a domain GinnError raised during input checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ginn.errors import GinnError
from ginn.gui.backend import CliResult, DiskUse

_UNITS: Final = ("KiB", "MiB", "GiB", "TiB")
NONE_TEXT: Final = "-"


@dataclass(frozen=True)
class Failure:
    """An error to show: its GINN-E-* code (None when the CLI gave none), a message, a remedy."""

    code: str | None
    message: str
    remedy: str | None


def failure_of(result: CliResult) -> Failure | None:
    """The failure of a CLI run, or None when it succeeded (IR-011).

    A non-zero exit without an error object is still a failure. Its message gives the status.
    """
    if result.ok:
        return None
    if result.error is not None:
        return Failure(
            code=result.error.code, message=result.error.message, remedy=result.error.remedy
        )
    return Failure(code=None, message=f"ginn exited with status {result.exit_code}", remedy=None)


def failure_of_error(err: GinnError) -> Failure:
    """The failure for a domain error raised by an input check (DR-002..004)."""
    return Failure(code=err.code, message=err.message, remedy=err.remedy)


def format_bytes(count: int) -> str:
    """Binary units with one decimal, for example `1.5 GiB`. Bytes are shown as `N B`."""
    if count < 1024:
        return f"{count} B"
    value = count / 1024
    index = 0
    while value >= 1024 and index < len(_UNITS) - 1:
        value /= 1024
        index += 1
    return f"{value:.1f} {_UNITS[index]}"


def disk_label(disk: DiskUse | None) -> str:
    """The disk use of a realm, with `(partial)` when the size is incomplete (FR-020)."""
    if disk is None:
        return NONE_TEXT
    text = format_bytes(disk.realm_home_bytes)
    return f"{text} (partial)" if disk.realm_home_partial else text


def services_label(services: bool | None) -> str:
    """`on`, `off`, or `-` when the CLI reports no value."""
    if services is None:
        return NONE_TEXT
    return "on" if services else "off"
