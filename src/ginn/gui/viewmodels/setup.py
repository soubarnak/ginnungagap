# SPDX-License-Identifier: Apache-2.0
"""The text of the status page that stands in for the Control Plane when it cannot start (FR-105).

GINN-E-HOST-NOT-SET-UP means Host Setup has not run for this user. The page then tells the user
to run it, with the remedy that the error carries. Pure Python, no GTK.
"""

from __future__ import annotations

from dataclasses import dataclass

from ginn.gui.viewmodels.labels import Failure

HOST_NOT_SET_UP: str = "GINN-E-HOST-NOT-SET-UP"
HOST_SETUP_TITLE = "Host Setup has not run"
HOST_SETUP_REMEDY = "Run 'sudo ginn host-setup --user <name>', then open the Control Plane again."


@dataclass(frozen=True)
class StatusText:
    title: str
    description: str


def status_text(failure: Failure) -> StatusText:
    """The page for a failure that stops the Control Plane before its first read."""
    if failure.code == HOST_NOT_SET_UP:
        return StatusText(
            title=HOST_SETUP_TITLE,
            description=failure.remedy or HOST_SETUP_REMEDY,
        )
    title = failure.code or "The Control Plane cannot start"
    return StatusText(title=title, description=failure.remedy or failure.message)
