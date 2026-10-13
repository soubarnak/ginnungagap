# SPDX-License-Identifier: Apache-2.0
"""`ginn status <realm> [--json]`: one realm's state, disk use and skips (FR-020, AS-025).

The keys of the JSON object are `RealmStatus.to_json()`, pinned by a golden test. The command takes
no realm lock and writes no record. Recovery runs first, as in every realm command (FR-122), and
a recovery that raises becomes a warning, so the status is still shown (see _context).
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli._views import status_text
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.ops.lifecycle import realm_status


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm to show")


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output, read_only=True)
    status = realm_status(realms.env, realms.podman, name, hooks=_context.lifecycle_hooks())
    _context.finish(realms, ctx.output)
    ctx.output.emit(status.to_json(), render=lambda: status_text(status))
    return 0


SPEC = CommandSpec(
    name="status",
    help="show one realm: state, container, tier, disk use and skipped shares (FR-020)",
    example="ginn status debian --json",
    configure=_configure,
    run=_run,
)
