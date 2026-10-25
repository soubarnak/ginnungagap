# SPDX-License-Identifier: Apache-2.0
"""`ginn start <realm>`: start a stopped realm and attach its grants (FR-016, FR-064, FR-066).

The work is `ops.lifecycle.start_realm`. A realm that runs already is returned as it is, with its
pending restart flag. Each share that the start skipped is printed, as FR-064 AC-1 asks.
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli._views import record_json, record_text
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import Warning as GinnWarning
from ginn.ops.lifecycle import start_realm


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm to start")


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    notices: list[GinnWarning] = []
    record = start_realm(
        realms.env,
        realms.podman,
        name,
        session=realms.host,
        hooks=_context.lifecycle_hooks(),
        warnings=notices,
    )
    _context.finish(realms, ctx.output)
    for notice in notices:
        ctx.output.warn(notice)
    ctx.output.emit(record_json(record), render=lambda: record_text(record))
    return 0


SPEC = CommandSpec(
    name="start",
    help="start a stopped realm and attach its grants (FR-016)",
    example="ginn start debian --json",
    configure=_configure,
    run=_run,
)
