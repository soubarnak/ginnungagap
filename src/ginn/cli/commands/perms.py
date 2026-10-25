# SPDX-License-Identifier: Apache-2.0
"""`ginn perms <realm>`, with the options --share, --share-ro, --no-share, --enable and --disable.

The Custom toggles and shares of a realm (FR-063, DR-004). Each share is checked with DR-004 when
it is added, and a path that DR-004 refuses gives GINN-E-PATH-FORBIDDEN with nothing written. A
share or a toggle applies at the next start (FR-066). With no change option, `ginn perms <realm>`
prints the permissions and writes nothing. The shares and toggles belong to a Custom realm only, so
any change to a Basic or Development realm gives GINN-E-TIER-NOT-CUSTOM.
"""

from __future__ import annotations

import argparse
from typing import Final

from ginn.cli import _context
from ginn.cli._views import permissions_json, permissions_text
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.domain.paths import Mode
from ginn.domain.permissions import Toggle
from ginn.errors import Warning as GinnWarning
from ginn.ops.permissions import read_permissions, set_permissions

TOGGLE_HELP: Final = ", ".join(toggle.value for toggle in Toggle) + " (FR-063)"
"""Every toggle name, from domain/permissions.py, so the help cannot drift from the set."""


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="Custom realm whose permissions change")
    parser.add_argument(
        "--share",
        dest="share",
        action="append",
        metavar="PATH",
        help="share PATH read-write at the same path inside the realm (FR-063, DR-004)",
    )
    parser.add_argument(
        "--share-ro",
        dest="share_ro",
        action="append",
        metavar="PATH",
        help="share PATH read-only at the same path inside the realm (FR-063, DR-004)",
    )
    parser.add_argument(
        "--no-share",
        dest="no_share",
        action="append",
        metavar="PATH",
        help="stop sharing PATH with the realm (FR-063)",
    )
    parser.add_argument(
        "--enable",
        dest="enable",
        action="append",
        metavar="TOGGLE",
        help=f"switch a toggle on: {TOGGLE_HELP}",
    )
    parser.add_argument(
        "--disable",
        dest="disable",
        action="append",
        metavar="TOGGLE",
        help=f"switch a toggle off: {TOGGLE_HELP}",
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    share: list[tuple[str, Mode]] = [(path, "rw") for path in args.share or []]
    share += [(path, "ro") for path in args.share_ro or []]
    unshare: list[str] = list(args.no_share or [])
    enable: list[str] = list(args.enable or [])
    disable: list[str] = list(args.disable or [])
    changing = bool(share or unshare or enable or disable)
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output, read_only=not changing)
    if changing:
        notices: list[GinnWarning] = []
        record = set_permissions(
            realms.env,
            name,
            realms.host,
            share=share,
            unshare=unshare,
            enable=enable,
            disable=disable,
            warnings=notices,
        )
        for notice in notices:
            ctx.output.warn(notice)
    else:
        record = read_permissions(realms.env, name)
    _context.finish(realms, ctx.output)
    ctx.output.emit(permissions_json(record), render=lambda: permissions_text(record))
    return 0


SPEC = CommandSpec(
    name="perms",
    help="show or change a Custom realm's toggles and shares (FR-063)",
    example="ginn perms debian-lab --share ~/Music --share-ro ~/Pictures --json",
    configure=_configure,
    run=_run,
)
