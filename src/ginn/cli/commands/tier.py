# SPDX-License-Identifier: Apache-2.0
"""`ginn tier <realm> [basic|development|custom]`: show or change a realm's permission tier.

With a tier, the work is `ops.permissions.set_tier`. Without one, the tier and its permissions are
printed and nothing is written (FR-060). A change applies at the next start, and a running realm
reports that a restart is required. A change that adds or removes a create-time option is recorded
too: the next start rebuilds the realm into a container with the new options, and keeps its packages
and Realm Home (FR-066, ADR-006). The change prints GINN-W-REBUILD-PENDING, which says so (CL-19).
`ginn perms` changes the Custom toggles and shares.
"""

from __future__ import annotations

import argparse
from typing import Final

from ginn.cli import _context
from ginn.cli._views import permissions_json, permissions_text
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName, Tier
from ginn.errors import Warning as GinnWarning
from ginn.ops.permissions import read_permissions, set_tier

TIERS: Final = tuple(tier.value for tier in Tier)


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm whose tier changes")
    parser.add_argument(
        "tier",
        metavar="TIER",
        nargs="?",
        choices=TIERS,
        help="permission tier: basic, development or custom (FR-060); omit it to show the tier",
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output, read_only=args.tier is None)
    if args.tier is None:
        record = read_permissions(realms.env, name)
        _context.finish(realms, ctx.output)
    else:
        notices: list[GinnWarning] = []
        record = set_tier(
            realms.env,
            name,
            Tier.parse(str(args.tier)),
            warnings=notices,
        )
        _context.finish(realms, ctx.output)
        for notice in notices:
            ctx.output.warn(notice)
    ctx.output.emit(permissions_json(record), render=lambda: permissions_text(record))
    return 0


SPEC = CommandSpec(
    name="tier",
    help="show or change a realm's permission tier (FR-060, FR-066)",
    example="ginn tier debian-dev development --json",
    configure=_configure,
    run=_run,
)
