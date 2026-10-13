# SPDX-License-Identifier: Apache-2.0
"""`ginn [--root] exec <realm> -- <cmd> [args...]`: run one command in a realm (FR-049, IR-011).

Every word after REALM belongs to the command, so `ginn exec arch ls --root` runs `ls --root`, and
`ginn exec arch cmd --help` runs `cmd --help`. The `--` separator, the form IR-010 shows, is taken
off the front of the command and is optional. The ginn option `--root` must come before REALM, as
in `ginn exec --root arch -- id`, and `--json` before the command name. The command's exit status
is the exit status of `<cmd>` (IR-011). A ginn-side failure exits 125 with its GINN-E code on
stderr (see _enter). A command with no words is a usage error (exit 2).
"""

from __future__ import annotations

import argparse

from ginn.cli._enter import enter_or_exec
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import make


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root", action="store_true", help="run as container root inside the realm (FR-042)"
    )
    parser.add_argument("realm", metavar="REALM", help="realm to run the command in")
    parser.add_argument(
        "cmd",
        metavar="CMD",
        nargs=argparse.REMAINDER,
        help="command and its arguments; every word after REALM belongs to the command",
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    realm = str(args.realm)
    RealmName(realm)  # a bad name is an argument error (exit 2), before the realm is touched
    cmd = [str(word) for word in args.cmd]
    if not cmd:
        raise make("GINN-E-USAGE", detail="exec needs a command after REALM, as in '-- CMD'")
    return enter_or_exec(ctx.output, realm, root=bool(args.root), argv=cmd)


SPEC = CommandSpec(
    name="exec",
    help="run one command in a realm and return its exit status (FR-049)",
    example="ginn exec debian -- uname -a",
    configure=_configure,
    run=_run,
)
