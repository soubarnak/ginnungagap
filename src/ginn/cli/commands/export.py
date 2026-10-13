# SPDX-License-Identifier: Apache-2.0
"""`ginn export --bin <realm> <binary>`: a launcher for a CLI binary of a realm (FR-047, AS-008).

The launcher is written to the ginn bin directory in Owned Paths. Running `<binary>` on the host
then runs it inside the realm through `ginn exec`. Nothing is exported unless this command is run.
ginn does not edit shell startup files, so when the bin directory is not on PATH the result says
so, and the user adds the directory.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import make
from ginn.export.bins import BinResult, bin_on_path, check_binary, export_bin


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bin",
        action="store_true",
        help="export a CLI binary as a launcher in the ginn bin directory (required, FR-047)",
    )
    parser.add_argument("realm", metavar="REALM", help="realm that holds the binary")
    parser.add_argument("binary", metavar="BINARY", nargs="?", help="binary name in the realm")


def _text(result: BinResult, bin_dir: Path, on_path: bool) -> str:
    verb = "exported" if result.changed else "already exported"
    lines = [f"{verb} {result.binary} from realm {result.realm}: {result.path}"]
    if not on_path:
        lines.append(
            f"note: {bin_dir} is not on PATH; add it to PATH in your shell startup file "
            "(ginn does not edit shell files, AS-008)"
        )
    return "\n".join(lines)


def _run(args: argparse.Namespace, ctx: Context) -> int:
    if not args.bin:
        raise make("GINN-E-USAGE", detail="export needs --bin: only CLI binaries are exported")
    if args.binary is None:
        raise make("GINN-E-USAGE", detail="export --bin needs a REALM and a BINARY")
    name = RealmName(str(args.realm))
    binary = check_binary(str(args.binary))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    result = export_bin(realms.env, name, binary, program=_context.ginn_program(realms.paths))
    _context.finish(realms, ctx.output)
    on_path = bin_on_path(realms.paths.bin, os.environ.get("PATH", ""))
    payload: dict[str, object] = {
        "realm": result.realm,
        "binary": result.binary,
        "launcher": str(result.path),
        "changed": result.changed,
        "on_path": on_path,
    }
    ctx.output.emit(payload, render=lambda: _text(result, realms.paths.bin, on_path))
    return 0


SPEC = CommandSpec(
    name="export",
    help="export a CLI binary of a realm as a launcher on the host (FR-047)",
    example="ginn export --bin debian rg",
    configure=_configure,
    run=_run,
)
