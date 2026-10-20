# SPDX-License-Identifier: Apache-2.0
"""`ginn _sync <realm>`: bring the host menu in line with a realm's desktop entries (FR-044).

Hidden from the top-level help: a package transaction runs it, not the user. The watcher passes the
hidden `--from-watch`: then the command exits 64 at once, before any lock, when the stamp is as the
last finished sync saw it (export/watch.py `sync_is_redundant`). The sync itself is
export/sync.py, a mutating operation. The result lists what was added, updated, removed,
unchanged and skipped, and each warning (GINN-W-*) is written before it.
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.export.sync import SyncHooks, SyncResult, sync_realm
from ginn.export.watch import FROM_WATCH, SYNC_SKIP_STATUS, WatchHooks, sync_is_redundant


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm whose desktop entries are synced")
    parser.add_argument(FROM_WATCH, dest="from_watch", action="store_true", help=argparse.SUPPRESS)


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
    if args.from_watch and sync_is_redundant(_context.owned_paths(), name):
        return SYNC_SKIP_STATUS  # the menu is current: no lock, journal or audit entry (ADR-016)
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    program = _context.ginn_program(realms.paths)
    hooks = SyncHooks(program=program, watch=WatchHooks(program=program))
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
