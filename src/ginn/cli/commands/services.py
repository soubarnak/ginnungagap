# SPDX-License-Identifier: Apache-2.0
"""`ginn services <realm> on|off`: set a realm's services mode (FR-083, IR-010).

The work is `ops.service.set_services_mode`. The mode is fixed in the container (ADR-003), so the
change is recorded now, and the next start rebuilds the realm into a container of the new mode, with
its packages and Realm Home kept (FR-083, ADR-003). Setting the mode the realm already has is a
no-op that prints it. A realm that is neither stopped nor running is refused before any write. A
change prints GINN-W-REBUILD-PENDING, which says that the next start rebuilds the realm (CL-19).
"""

from __future__ import annotations

import argparse
from typing import Final

from ginn.cli import _context
from ginn.cli._views import services_json, services_text
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import Warning as GinnWarning
from ginn.ops.service import set_services_mode

MODES: Final = ("on", "off")


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm whose services mode is set")
    parser.add_argument(
        "mode",
        metavar="MODE",
        choices=MODES,
        help="services mode: on runs systemd as the realm's init (FR-080), off runs no systemd",
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output, read_only=True)
    notices: list[GinnWarning] = []
    record = set_services_mode(realms.env, name, on=args.mode == "on", warnings=notices)
    _context.finish(realms, ctx.output)
    for notice in notices:
        ctx.output.warn(notice)
    ctx.output.emit(services_json(record), render=lambda: services_text(record))
    return 0


SPEC = CommandSpec(
    name="services",
    help="turn a realm's services mode on or off (FR-083)",
    example="ginn services debian-dev on --json",
    configure=_configure,
    run=_run,
)
