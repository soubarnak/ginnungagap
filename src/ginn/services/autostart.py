# SPDX-License-Identifier: Apache-2.0
"""Autostart of a services-on realm: the `down` file of its runit service (FR-084, DR-012).

Autostart on means runit starts the realm when its runsv starts, which is when the user's
supervisor starts (AS-016, FR-084). runsv reads the `down` file only when it starts (runsv(8)), so
the file is what autostart toggles: on removes it, off creates it. The record keeps the flag
(`settings.autostart`). The supervised start makes the file match the flag again (ops/lifecycle.py),
so the give-up of a realm, which writes `down`, is undone by the next supervised start.

`apply` is the file operation. ops/service.py owns the record write and the realm lock.
"""

from __future__ import annotations

import os
from pathlib import Path

from ginn.domain import RealmName
from ginn.errors import make
from ginn.services.runit_user import DOWN, mark_down, service_dir
from ginn.store.paths import OwnedPaths


def down_file(paths: OwnedPaths, name: RealmName) -> Path:
    """The `down` file of the realm's runit service (runit_user.DOWN)."""
    return service_dir(paths, name) / DOWN


def is_down(paths: OwnedPaths, name: RealmName) -> bool:
    """True when the `down` file exists, so runit does not start the realm (DR-012)."""
    return os.path.lexists(down_file(paths, name))


def apply(paths: OwnedPaths, name: RealmName, autostart: bool) -> None:
    """Make the `down` file match `autostart` (DR-012). Idempotent.

    A missing service directory is GINN-E-SERVICE-MISSING, and nothing is written. A services-on
    realm has one from its create, so a missing one is a state the user must see, not a bug.
    """
    if not service_dir(paths, name).is_dir():
        raise make("GINN-E-SERVICE-MISSING", realm=name.value, detail="no service directory")
    if autostart:
        down_file(paths, name).unlink(missing_ok=True)
    else:
        mark_down(paths, name)
