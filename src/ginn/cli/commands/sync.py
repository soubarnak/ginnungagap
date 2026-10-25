# SPDX-License-Identifier: Apache-2.0
"""`ginn _sync <realm>`: bring the host menu in line with a realm's desktop entries (FR-044).

Hidden from the top-level help: a package transaction runs it, not the user. The sync itself is
export/sync.py, a mutating operation. The result lists what was added, updated, removed,
unchanged and skipped, and each warning (GINN-W-*) is written before it.
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.export.sync import SyncHooks, SyncResult, sync_realm


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm whose desktop entries are synced")


def _text(result: SyncResult) -> str:
    if not result.running:
        return f"realm {result.realm} does not run: its desktop entries were not synced"
    return (
        f"synced realm {result.realm}: added {len(result.added)}, updated {len(result.updated)}, "
        f"removed {len(result.removed)}, unchanged {len(result.unchanged)}, "
        f"skipped {len(result.skipped)}"
    )


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    hooks = SyncHooks(program=_context.ginn_program(realms.paths))
    result = sync_realm(realms.env, realms.podman, name, hooks=hooks)
    _context.finish(realms, ctx.output)
    for notice in result.warnings:
        ctx.output.warn(notice)
    payload: dict[str, object] = {
        "realm": result.realm,
        "running": result.running,
        "added": result.added,
        "updated": result.updated,
        "removed": result.removed,
        "unchanged": result.unchanged,
        "skipped": result.skipped,
    }
    ctx.output.emit(payload, render=lambda: _text(result))
    return 0


SPEC = CommandSpec(
    name="_sync",
    help="sync a realm's desktop entries to the host menu (internal, FR-044)",
    example="ginn _sync debian",
    configure=_configure,
    run=_run,
    hidden=True,
)
