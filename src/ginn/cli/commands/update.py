# SPDX-License-Identifier: Apache-2.0
"""`ginn update <realm>|--all [--check]`: the native full upgrade, or a check for updates (IR-010).

The work is `ops.update`. `update <realm>` upgrades one realm and `update --all` every realm of
the user. `--check` reports whether updates exist and changes nothing (FR-024). Without a REALM it
checks every realm, as FR-024 writes `[<realm>]`. Each realm is one row of the result, in name
order, and a failed realm does not stop the others. The JSON result is `{"realms": [...]}`. When any
realm failed, the first error is raised after the result is written, so stderr and the exit status
name it.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.distros.loader import UpdateStatus
from ginn.domain import RealmName
from ginn.errors import make
from ginn.ops.update import (
    DISK_PRECHECK_SKIPPED,
    RealmCheck,
    UpdateResult,
    check_realms,
    update_realms,
)
from ginn.packages.manager import CheckResult


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "realm", metavar="REALM", nargs="?", help="realm to update, or to check with --check"
    )
    parser.add_argument("--all", action="store_true", help="every realm of this user")
    parser.add_argument(
        "--check", action="store_true", help="report whether updates exist; change nothing (FR-024)"
    )


def _update_row(row: UpdateResult) -> dict[str, object]:
    """One realm of an update: `status` is updated or error, with the state after it."""
    state = None if row.record is None else row.record.state.value
    if row.error is not None:
        return {
            "realm": row.realm,
            "status": "error",
            "state": state,
            "code": row.error.code,
            "message": row.error.message,
            "disk_precheck": DISK_PRECHECK_SKIPPED,
        }
    return {
        "realm": row.realm,
        "status": "updated",
        "state": state,
        "code": None,
        "message": None,
        "disk_precheck": DISK_PRECHECK_SKIPPED,
    }


def _update_text(rows: Sequence[UpdateResult]) -> str:
    lines: list[str] = []
    for row in rows:
        if row.error is not None:
            lines.append(f"{row.realm}: not updated: {row.error.code}: {row.error.message}")
        else:
            state = "" if row.record is None else f", now {row.record.state.value}"
            lines.append(f"{row.realm}: updated{state}")
    return "\n".join(lines) if lines else "no realms to update"


def _check_json(row: RealmCheck) -> dict[str, object]:
    """One realm of a check. `status` is updates, none, skipped or error (FR-024)."""
    base: dict[str, object] = {"realm": row.realm, "state": row.state}
    if row.error is not None:
        return {
            **base,
            "status": "error",
            "updates": None,
            "count": None,
            "reason": None,
            "code": row.error.code,
        }
    if row.result is None:
        return {
            **base,
            "status": "skipped",
            "updates": None,
            "count": None,
            "reason": row.skipped,
            "code": None,
        }
    result: CheckResult = row.result
    status = "updates" if result.status is UpdateStatus.UPDATES else "none"
    return {
        **base,
        "status": status,
        "updates": result.updates,
        "count": result.count,
        "reason": None,
        "code": None,
    }


def _check_text(rows: Sequence[RealmCheck]) -> str:
    lines: list[str] = []
    for row in rows:
        if row.error is not None:
            lines.append(f"{row.realm}: check failed: {row.error.code}: {row.error.message}")
        elif row.result is None:
            lines.append(f"{row.realm}: not checked: {row.skipped}")
        elif row.result.updates:
            count = "" if row.result.count is None else f" ({row.result.count} packages)"
            lines.append(f"{row.realm}: updates available{count}")
        else:
            lines.append(f"{row.realm}: no updates")
    return "\n".join(lines) if lines else "no realms to check"


def _run(args: argparse.Namespace, ctx: Context) -> int:
    realm = None if args.realm is None else str(args.realm)
    every = bool(args.all)
    check = bool(args.check)
    if realm is not None and every:
        raise make("GINN-E-USAGE", detail="update takes a REALM or --all, not both")
    if realm is None and not every and not check:
        raise make("GINN-E-USAGE", detail="update needs a REALM, --all or --check")
    names = None if realm is None else [RealmName(realm)]
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output, read_only=check, sweep=True)
    hooks = _context.package_hooks(realms.paths)
    if check:
        checks = check_realms(realms.env, realms.podman, names, hooks=hooks)
        _context.finish(realms, ctx.output)
        ctx.output.emit(
            {"realms": [_check_json(row) for row in checks]},
            render=lambda: _check_text(checks),
        )
        failed = [row.error for row in checks if row.error is not None]
        if failed:
            raise failed[0]
        return 0
    updates = update_realms(realms.env, realms.podman, names, session=realms.host, hooks=hooks)
    _context.finish(realms, ctx.output)
    for row in updates:
        for notice in row.warnings:
            ctx.output.warn(notice)
    ctx.output.emit(
        {"realms": [_update_row(row) for row in updates]},
        render=lambda: _update_text(updates),
    )
    failed_updates = [row.error for row in updates if row.error is not None]
    if failed_updates:
        raise failed_updates[0]
    return 0


SPEC = CommandSpec(
    name="update",
    help="run the native full upgrade of a realm, or check it for updates (FR-023, FR-024)",
    example="ginn update arch --json",
    configure=_configure,
    run=_run,
)
