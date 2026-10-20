# SPDX-License-Identifier: Apache-2.0
"""`ginn install <realm> [--aur] <pkg>...`: install packages in a realm (FR-040, FR-043).

The work is `ops.packages.install_packages`. A stopped realm starts first, as enter starts one.
The packages are recorded in the Install Manifest as `ginn` installs (DR-007). `--aur` builds an
AUR package on Arch inside the realm (FR-043, IR-023). On any other distribution it is refused with
GINN-E-OPTION-INVALID. The realm user's group is the host group, as enter passes it.

A name that is an init entry of the realm is not installed again (ADR-015 Decision 14). Its note is
written to stderr in text mode, and in JSON mode it rides in the result as `notes`, which is always
present. On failure the notes ride on the error object. The result lists only the packages that
were installed.
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import GinnError, make
from ginn.errors import Warning as GinnWarning
from ginn.ops.packages import install_packages


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm to install into")
    parser.add_argument("--aur", action="store_true", help="build an AUR package on Arch (FR-043)")
    parser.add_argument(
        "packages", metavar="PKG", nargs="*", help="package names; every word after REALM"
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    names = [str(word) for word in args.packages]
    if not names:
        raise make("GINN-E-USAGE", detail="install needs at least one PKG after REALM")
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    notices: list[GinnWarning] = []
    notes: list[str] = []
    sent: list[str] = []
    try:
        record = install_packages(
            realms.env,
            realms.podman,
            name,
            names,
            aur=bool(args.aur),
            session=realms.host,
            gid=realms.gid,
            hooks=_context.package_hooks(realms.paths),
            warnings=notices,
            notes=notes,
            sent=sent,
        )
    except GinnError as err:
        # The notes were collected before the install ran, so the failure carries them as well.
        for note in notes:
            err.add_note(note)
        raise
    _context.finish(realms, ctx.output)
    for notice in notices:
        ctx.output.warn(notice)
    for note in notes:
        ctx.output.note(note)
    payload: dict[str, object] = {
        "realm": record.name.value,
        "packages": sent,
        "manifest": [entry.to_dict() for entry in record.manifest],
        "notes": notes,
    }
    if sent:
        text = f"installed in realm {record.name.value}: {' '.join(sent)}"
    else:
        text = f"nothing was installed in realm {record.name.value}"
    ctx.output.emit(payload, render=lambda: text)
    return 0


SPEC = CommandSpec(
    name="install",
    help="install packages in a realm with its native manager (FR-040)",
    example="ginn install debian htop --json",
    configure=_configure,
    run=_run,
)
