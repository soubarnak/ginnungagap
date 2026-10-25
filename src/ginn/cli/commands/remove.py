# SPDX-License-Identifier: Apache-2.0
"""`ginn remove <realm> <pkg>...`: remove packages from a realm (FR-041).

The work is `ops.packages.remove_packages`. A removal that would also take a package the user did
not name is refused before it runs (GINN-E-PKG-DEPENDENTS, ADR-011). The names leave the Install
Manifest. Removing the exported desktop entry is not built yet (FR-045).
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import Warning as GinnWarning
from ginn.errors import make
from ginn.ops.packages import remove_packages


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm to remove from")
    parser.add_argument(
        "packages", metavar="PKG", nargs="*", help="package names; every word after REALM"
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    names = [str(word) for word in args.packages]
    if not names:
        raise make("GINN-E-USAGE", detail="remove needs at least one PKG after REALM")
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    notices: list[GinnWarning] = []
    record = remove_packages(
        realms.env,
        realms.podman,
        name,
        names,
        session=realms.host,
        hooks=_context.package_hooks(realms.paths),
        warnings=notices,
    )
    _context.finish(realms, ctx.output)
    for notice in notices:
        ctx.output.warn(notice)
    payload: dict[str, object] = {
        "realm": record.name.value,
        "packages": names,
        "manifest": [entry.to_dict() for entry in record.manifest],
    }
    text = f"removed from realm {record.name.value}: {' '.join(names)}"
    ctx.output.emit(payload, render=lambda: text)
    return 0


SPEC = CommandSpec(
    name="remove",
    help="remove packages from a realm with its native manager (FR-041)",
    example="ginn remove debian htop --json",
    configure=_configure,
    run=_run,
)
