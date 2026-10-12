# SPDX-License-Identifier: Apache-2.0
"""`ginn log [<realm>] [--since <time>] [--json]`: show Audit Log entries (FR-125, IR-010).

Entries come back oldest start first, across the rotated files (DR-032). A realm argument keeps
only that realm's entries. `--since` keeps the entries that started at or after the time. The time
is ISO-8601. Without a zone it is local time. FR-125 does not define the form of <time>, so this is
ginn's choice. The command only reads: it takes no lock and writes nothing. A read that overlaps a
rotation of the log (store/audit.py, DR-019) may be inexact, because the files move while they are
read. The command reports what it read and does not retry.

A torn last line, left by a crash mid-write, is skipped. Any other damaged line fails the command
with GINN-E-INTERNAL, because store/audit.py reports it rather than hide it. An empty log prints
"no audit entries" and exits 0. In JSON mode the result is {"entries": [...]}, empty when there is
nothing to show.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import make
from ginn.store.audit import AuditEntry, AuditLog
from ginn.store.paths import OwnedPaths

NO_ENTRIES = "no audit entries"


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", nargs="?", metavar="REALM", help="show only this realm's entries")
    parser.add_argument(
        "--since",
        metavar="TIME",
        help="show entries that started at or after TIME, an ISO-8601 time such as "
        "2026-10-01T09:00:00Z; without a zone it is local time",
    )


def _since(text: str | None) -> datetime | None:
    """Parse --since. An aware time keeps its zone, and a naive one is local time."""
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        detail = f"--since expects an ISO-8601 time, got '{text}'"
        raise make("GINN-E-USAGE", detail=detail) from None
    return moment.astimezone()


def _line(entry: AuditEntry) -> str:
    """One entry as text: start time, account, realm, operation, outcome, and any error code."""
    parts = [entry.ts or "-", entry.user, entry.realm or "-", entry.op, entry.outcome]
    if entry.error_code is not None:
        parts.append(entry.error_code)
    return " ".join(parts)


def _text(entries: list[AuditEntry]) -> str:
    return NO_ENTRIES if not entries else "\n".join(_line(entry) for entry in entries)


def _run(args: argparse.Namespace, ctx: Context) -> int:
    realm = None if args.realm is None else RealmName(str(args.realm))
    since = _since(args.since)
    audit = AuditLog(OwnedPaths.from_env())
    # Materialised here, so a damaged line fails before anything is written to stdout.
    entries = list(audit.read(realm=realm, since=since))
    rows = [entry.to_json() for entry in entries]
    ctx.output.emit({"entries": rows}, render=lambda: _text(entries))
    return 0


SPEC = CommandSpec(
    name="log",
    help="show Audit Log entries, oldest first, for all realms or one; --since filters by time",
    example="ginn log arch --since 2026-10-01T00:00:00Z --json",
    configure=_configure,
    run=_run,
)
