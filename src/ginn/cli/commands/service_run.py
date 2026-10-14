# SPDX-License-Identifier: Apache-2.0
"""`ginn _service-run <realm>`: start a services-on realm under its runit service (FR-081).

Hidden from the top-level help: the realm's runit `run` script runs it, not the user. The start is
ops.service.start_service, which starts the container and attaches its grants (FR-084, ADR-008).
On success this process execs ginn-wait, which supervises the conmon of the container (NFR-033),
so the runit service is this process from then on. Nothing is written to stdout after the exec.
"""

from __future__ import annotations

import argparse
import os
import sys

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import Warning as GinnWarning
from ginn.ops.service import start_service


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="services-on realm to start")


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    notices: list[GinnWarning] = []
    argv = start_service(
        realms.env,
        realms.podman,
        name,
        global_argv=_context.podman_global(realms.podman),
        session=realms.host,
        hooks=_context.service_hooks(),
        warnings=notices,
    )
    _context.finish(realms, ctx.output)
    for notice in notices:
        ctx.output.warn(notice)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(argv[0], argv)  # noqa: S606 - fixed argv list from start_service, no shell


SPEC = CommandSpec(
    name="_service-run",
    help="start a services-on realm under its runit service (internal, FR-081)",
    example="ginn _service-run debian",
    configure=_configure,
    run=_run,
    hidden=True,
)
