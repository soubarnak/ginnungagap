# SPDX-License-Identifier: Apache-2.0
"""`ginn enter [--root] <realm>`: open an interactive login shell in a realm (FR-048, FR-042).

The shell runs as the realm user with HOME at the host home path (FR-048). `--root` gives a shell
as container root, with HOME at /root (FR-042). A realm that is stopped starts first (FR-017).
With no terminal on stdin and stdout, the shell gets no TTY (`-t` is not passed). The command's
exit status is the shell's status (IR-011). A ginn-side failure exits 125 (see _enter).
"""

from __future__ import annotations

import argparse

from ginn.cli._enter import enter_or_exec
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root", action="store_true", help="shell as container root inside the realm (FR-042)"
    )
    parser.add_argument("realm", metavar="REALM", help="realm to enter")


def _run(args: argparse.Namespace, ctx: Context) -> int:
    realm = str(args.realm)
    RealmName(realm)  # a bad name is an argument error (exit 2), before the realm is touched
    return enter_or_exec(ctx.output, realm, root=bool(args.root), argv=None)


SPEC = CommandSpec(
    name="enter",
    help="open a login shell in a realm as your user; --root gives container root (FR-048)",
    example="ginn enter debian",
    configure=_configure,
    run=_run,
)
