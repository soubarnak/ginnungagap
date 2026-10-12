# SPDX-License-Identifier: Apache-2.0
"""`ginn host-setup --user NAME`: one-time Host Setup for a user (FR-001, FR-002, IR-010).

The command is a thin wrapper over `ginn.hostsetup.setup.run_setup`. `--undo` is T3.5 and is not
here yet.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ginn.cli.commands import CommandSpec, Context
from ginn.hostsetup.setup import SetupResult, run_setup


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--user",
        required=True,
        metavar="NAME",
        help="account that gets rootless realms; must already exist",
    )
    parser.add_argument(
        "--root-dir",
        default="/",
        metavar="DIR",
        help="directory that every host path is placed under (default /; tests use a temporary "
        "directory)",
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    result: SetupResult = run_setup(str(args.user), root_dir=Path(str(args.root_dir)))
    ctx.output.emit(result.as_dict(), render=result.render)
    return 0


SPEC = CommandSpec(
    name="host-setup",
    help="one-time Host Setup for a user; must run as root",
    example="sudo ginn host-setup --user alice",
    configure=_configure,
    run=_run,
)
