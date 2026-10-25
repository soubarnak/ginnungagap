# SPDX-License-Identifier: Apache-2.0
"""`ginn unexport --bin <realm> <binary>`: remove a CLI launcher made by `ginn export` (FR-047).

Only a launcher that names this realm and binary is removed. A file that ginn did not write is
never touched, and neither is a launcher of another realm. The realm record is not read, so the
launcher of a deleted realm can still be removed.
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import make
from ginn.export.bins import BinResult, check_binary, unexport_bin


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bin",
        action="store_true",
        help="remove the launcher of a CLI binary made by 'ginn export --bin' (required)",
    )
    parser.add_argument("realm", metavar="REALM", help="realm that the launcher runs in")
    parser.add_argument("binary", metavar="BINARY", nargs="?", help="binary name in the realm")


def _text(result: BinResult) -> str:
    return f"removed launcher of {result.binary} from realm {result.realm}: {result.path}"


def _run(args: argparse.Namespace, ctx: Context) -> int:
    if not args.bin:
        raise make("GINN-E-USAGE", detail="unexport needs --bin: only CLI binaries are unexported")
    if args.binary is None:
        raise make("GINN-E-USAGE", detail="unexport --bin needs a REALM and a BINARY")
    name = RealmName(str(args.realm))
    binary = check_binary(str(args.binary))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    result = unexport_bin(realms.env, name, binary)
    _context.finish(realms, ctx.output)
    payload: dict[str, object] = {
        "realm": result.realm,
        "binary": result.binary,
        "launcher": str(result.path),
        "removed": result.changed,
    }
    ctx.output.emit(payload, render=lambda: _text(result))
    return 0


SPEC = CommandSpec(
    name="unexport",
    help="remove a CLI launcher that 'ginn export --bin' made (FR-047)",
    example="ginn unexport --bin debian rg",
    configure=_configure,
    run=_run,
)
