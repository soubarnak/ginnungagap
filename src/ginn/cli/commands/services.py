# SPDX-License-Identifier: Apache-2.0
"""`ginn services <realm> on|off`: set a realm's services mode (FR-083, IR-010).

The work is `ops.service.set_services_mode`. The mode is fixed in the container (ADR-003), so a
change needs the next container generation, and it is refused with GINN-E-NOT-SUPPORTED-YET,
naming T8.3, before anything is written. Setting the mode the realm already has is a no-op that
prints it. The change takes effect at the next start (FR-083), once T8.3 lands.
"""

from __future__ import annotations

import argparse
from typing import Final

from ginn.cli import _context
from ginn.cli._views import services_json, services_text
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
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
    record = set_services_mode(realms.env, name, on=args.mode == "on")
    _context.finish(realms, ctx.output)
    ctx.output.emit(services_json(record), render=lambda: services_text(record))
    return 0


SPEC = CommandSpec(
    name="services",
    help="turn a realm's services mode on or off (FR-083)",
    example="ginn services debian-dev on --json",
    configure=_configure,
    run=_run,
)
