# SPDX-License-Identifier: Apache-2.0
"""`ginn _service-finish <realm> <exit> <signal>`: record one end of a services-on realm's run.

Hidden from the top-level help: the realm's runit `finish` script runs it, with the two arguments
runsv passes (runsv(8)). The work is ops.service.finish_service: a stop request leaves the realm
stopped, and a crash waits the restart delay, or gives the realm up after five crashes in 60 s
(AS-028, FR-081). It prints nothing on stdout.
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import Warning as GinnWarning
from ginn.ops.service import finish_service


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="services-on realm whose run ended")
    parser.add_argument(
        "exit_code",
        metavar="EXIT",
        type=int,
        help="the exit code of run, or -1 when run did not exit normally",
    )
    parser.add_argument(
        "signal_no",
        metavar="SIGNAL",
        type=int,
        help="the signal that ended run, or 0 when none did",
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    notices: list[GinnWarning] = []
    outcome = finish_service(
        realms.env,
        name,
        exit_code=int(args.exit_code),
        signal_no=int(args.signal_no),
        hooks=_context.service_hooks(),
        warnings=notices,
    )
    _context.finish(realms, ctx.output)
    for notice in notices:
        ctx.output.warn(notice)
    ctx.output.verbose_line(f"service {name.value}: {outcome.value}")
    return 0


SPEC = CommandSpec(
    name="_service-finish",
    help="record one end of a services-on realm's run (internal, FR-081)",
    example="ginn _service-finish debian 1 0",
    configure=_configure,
    run=_run,
    hidden=True,
)
