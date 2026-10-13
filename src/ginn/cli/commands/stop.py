# SPDX-License-Identifier: Apache-2.0
"""`ginn stop <realm>`: stop a running realm (FR-016).

The work is `ops.lifecycle.stop_realm`, which waits up to 30 s before podman kills the realm
(AS-023). A stopped realm stays stopped, so the command is safe to repeat.
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli._views import record_json, record_text
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import Warning as GinnWarning
from ginn.ops.lifecycle import stop_realm


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm to stop")


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    notices: list[GinnWarning] = []
    record = stop_realm(
        realms.env, realms.podman, name, hooks=_context.lifecycle_hooks(), warnings=notices
    )
    _context.finish(realms, ctx.output)
    for notice in notices:
        ctx.output.warn(notice)
    ctx.output.emit(record_json(record), render=lambda: record_text(record))
    return 0


SPEC = CommandSpec(
    name="stop",
    help="stop a running realm; its processes end within 30 s (FR-016)",
    example="ginn stop debian --json",
    configure=_configure,
    run=_run,
)
