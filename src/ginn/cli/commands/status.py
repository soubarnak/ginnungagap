# SPDX-License-Identifier: Apache-2.0
"""`ginn status <realm> [--json]`: one realm's state, disk use and skips (FR-020, AS-025).

The keys of the JSON object are `RealmStatus.to_json()`, pinned by a golden test. The command takes
no realm lock and writes no record. Recovery runs first, as in every realm command (FR-122), and
a recovery that raises becomes a warning, so the status is still shown (see _context).

An orphan marker of the realm (FR-124, ADR-014) is shown as GINN-W-RELAY-ORPHAN, one warning per
record, and as GINN-W-ORPHAN-UNKNOWN for a record with processes the kill could not read. It is read
only: the marker is audited and removed by the realm's next mutating operation, not by status. A
marker that cannot be read is GINN-W-ORPHAN-UNSETTLED, and status still succeeds.
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli._views import status_text
from ginn.cli.commands import CommandSpec, Context
from ginn.cli.output import Output
from ginn.domain import RealmName
from ginn.errors import GinnError, warn
from ginn.ops._mutating import orphan_warnings, read_orphans
from ginn.ops.lifecycle import realm_status
from ginn.services._exec_lock import exec_lock_busy
from ginn.store.paths import OwnedPaths


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm to show")
    parser.add_argument(
        "--sizes",
        action="store_true",
        help="also report the container, image and base image sizes from podman (NFR-008); slow",
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output, read_only=True)
    status = realm_status(
        realms.env, realms.podman, name, hooks=_context.lifecycle_hooks(), sizes=bool(args.sizes)
    )
    _context.finish(realms, ctx.output)
    _orphan_notices(realms.env.paths, name, ctx.output)
    ctx.output.emit(status.to_json(), render=lambda: status_text(status))
    return 0


def _orphan_notices(paths: OwnedPaths, name: RealmName, output: Output) -> None:
    """Show the realm's orphan records as warnings, read only (FR-124, ADR-014).

    The listing is skipped while the exec lock is busy, because a busy lock's exec may still be
    writing its marker. It is also skipped when the lock path is faulty: that is not ginn's marker
    to read, and the operations that use the lock report the fault. A marker that cannot be read is
    a GINN-W-ORPHAN-UNSETTLED warning, not an error, so the status is still shown.
    """
    try:
        if exec_lock_busy(paths, name):
            return
    except GinnError:
        return
    try:
        records = read_orphans(paths, name)
    except OSError as exc:
        detail = f"{type(exc).__name__}: {exc}"
        output.warn(warn("GINN-W-ORPHAN-UNSETTLED", realm=name.value, detail=detail))
        return
    for record in records:
        for notice in orphan_warnings(name, record):
            output.warn(notice)


SPEC = CommandSpec(
    name="status",
    help="show one realm: state, container, tier, disk use and skipped shares (FR-020)",
    example="ginn status debian --json",
    configure=_configure,
    run=_run,
)
