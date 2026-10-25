# SPDX-License-Identifier: Apache-2.0
"""`ginn host-setup --user NAME` and `--undo`: Host Setup for a user (FR-001..003, IR-010).

The command is a thin wrapper over `ginn.hostsetup.setup.run_setup` and
`ginn.hostsetup.undo.run_undo`.
Undo exits 0 when it completed, even when it left paths in place: those paths are printed with
their reasons (CL-11).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ginn.cli.commands import CommandSpec, Context
from ginn.hostsetup.setup import SetupResult, run_setup
from ginn.hostsetup.undo import UndoResult, run_undo


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--user",
        default=None,
        metavar="NAME",
        help="account that gets rootless realms; must already exist; a root run needs it",
    )
    parser.add_argument(
        "--root-dir",
        default="/",
        metavar="DIR",
        help="directory that every host path is placed under (default /; tests use a temporary "
        "directory)",
    )
    parser.add_argument(
        "--undo",
        action="store_true",
        help="undo Host Setup for --user: restore or remove what setup wrote; refused while the "
        "user still has realms",
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    root = Path(str(args.root_dir))
    result: SetupResult | UndoResult
    if args.undo:
        result = run_undo(args.user, root_dir=root)
    else:
        result = run_setup(args.user, root_dir=root)
    ctx.output.emit(result.as_dict(), render=result.render)
    return 0


SPEC = CommandSpec(
    name="host-setup",
    help="one-time Host Setup for a user, or its --undo; must run as root",
    example="sudo ginn host-setup --user alice",
    configure=_configure,
    run=_run,
)
