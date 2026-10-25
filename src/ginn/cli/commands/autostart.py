# SPDX-License-Identifier: Apache-2.0
"""`ginn autostart <realm> on|off`: start a services-on realm when the user's runit starts (FR-084).

The work is `ops.service.set_autostart`. Autostart needs services on (DR-012), so a services-off
realm is refused before anything is written. On removes the realm's `down` file and off creates it
(services/autostart.py). The flag is stored on the realm record. The change applies at the next
start that runit makes, and a running realm keeps running.
"""

from __future__ import annotations

import argparse
from typing import Final

from ginn.cli import _context
from ginn.cli._views import autostart_json, autostart_text
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.ops.service import set_autostart

MODES: Final = ("on", "off")


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="services-on realm whose autostart changes")
    parser.add_argument(
        "mode",
        metavar="MODE",
        choices=MODES,
        help="autostart: on starts the realm when the user's runit supervisor starts, off does not",
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    record = set_autostart(realms.env, name, on=args.mode == "on")
    _context.finish(realms, ctx.output)
    ctx.output.emit(autostart_json(record), render=lambda: autostart_text(record))
    return 0


SPEC = CommandSpec(
    name="autostart",
    help="start a services-on realm when the user's runit starts (FR-084)",
    example="ginn autostart debian-dev on --json",
    configure=_configure,
    run=_run,
)
