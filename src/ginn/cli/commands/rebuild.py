# SPDX-License-Identifier: Apache-2.0
"""`ginn rebuild <realm>`: rebuild a realm's root from its pinned image (FR-021, FR-022, IR-010).

The work is `ops.rebuild.rebuild_realm`. The new generation is built beside the old one, the Install
Manifest is replayed into it, and the swap is one record write (DR-034). The Realm Home, the tier,
the permissions and the services mode are kept (FR-021 AC-1). The command asks no question and takes
no flag: IR-010 and FR-021 name none, and a Kali everything set was consented at create. A running
realm is stopped and started again. A package that is no longer available upstream is a
GINN-W-REPLAY-MISSING warning, and the rebuild still completes (FR-022). The JSON result is the
realm's record view with `generation`, `image_digest`, `replay_missing` and `old_removed`.
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli._views import record_json
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.ops.rebuild import RebuildResult, rebuild_realm


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm to rebuild")


def _payload(result: RebuildResult) -> dict[str, object]:
    record = result.record
    return {
        **record_json(record),
        "generation": record.generation,
        "image_digest": record.image_digest,
        "replay_missing": list(result.replay_missing),
        "old_removed": result.old_removed,
    }


def _text(result: RebuildResult) -> str:
    record = result.record
    lines = [
        f"realm {record.settings.name.value} rebuilt: generation {record.generation}, "
        f"state {record.state.value}"
    ]
    lines.extend(record.share_skipped)
    if not result.old_removed:
        lines.append("note: an older root was not removed; recovery or 'ginn delete' removes it")
    return "\n".join(lines)


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output, sweep=True)
    result = rebuild_realm(
        realms.env,
        realms.podman,
        name,
        session=realms.host,
        gid=realms.gid,
        hooks=_context.rebuild_hooks(realms.paths, ctx.output),
    )
    _context.finish(realms, ctx.output)
    for notice in result.warnings:
        ctx.output.warn(notice)
    ctx.output.emit(_payload(result), render=lambda: _text(result))
    return 0


SPEC = CommandSpec(
    name="rebuild",
    help="rebuild a realm's root from its pinned image, keeping its home and packages (FR-021)",
    example="ginn rebuild arch --json",
    configure=_configure,
    run=_run,
)
