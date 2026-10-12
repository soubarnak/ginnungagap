# SPDX-License-Identifier: Apache-2.0
"""`ginn integrity [--ack]`: check the host against the integrity baseline (FR-006, IR-010).

The check fingerprints the AS-020 scope and compares it with the baseline. The first run creates
the baseline. A change is reported as GINN-W-HOST-CHANGED, recorded as pending and audited, and the
command still exits 0 (warnings do not fail). A pending change blocks mutating operations until
`--ack` accepts it: `--ack` accepts only the paths that were listed as pending, and refuses with
GINN-E-HOST-UNACKED, listing the paths, when the host differs in any other path.

The command is a thin wrapper over `ginn.integrity.state`, which takes its own lock. `--root-dir`
is hidden from the help: the tests pass it, so they never read the real host.
"""

from __future__ import annotations

import argparse
import os
import pwd
from pathlib import Path

from ginn.cli.commands import CommandSpec, Context
from ginn.errors import warn
from ginn.integrity.state import IntegrityState, Report
from ginn.store.audit import AuditLog
from ginn.store.paths import OwnedPaths


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ack",
        action="store_true",
        help="accept the listed changes as the new baseline; refused, with the paths listed, when "
        "the host differs in any path that was not listed; audited",
    )
    parser.add_argument("--root-dir", default="/", metavar="DIR", help=argparse.SUPPRESS)


def _account_name() -> str:
    uid = os.getuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _ack_text(report: Report) -> str:
    """The acknowledgement result: the count, then every accepted path, then any discarded file."""
    lines = [
        f"host integrity acknowledged: baseline updated, {len(report.changed)} path(s) accepted"
    ]
    lines.extend(f"  {path}" for path in report.changed)
    lines.extend(
        f"note: {name} discarded and rewritten: {reason}"
        for name, reason in report.discarded.items()
    )
    return "\n".join(lines)


def _pending_note(pending: list[str]) -> str:
    return (
        f"an earlier change of {len(pending)} path(s) is still pending: run 'ginn integrity --ack' "
        "after review"
    )


def _unchanged_text(pending: list[str] | None) -> str:
    if not pending:
        return "host unchanged since baseline"
    return f"host unchanged since baseline; {_pending_note(pending)}"


def _created_text(count: int, pending: list[str] | None) -> str:
    text = f"host integrity baseline created: {count} path(s) fingerprinted"
    return text if not pending else f"{text}; {_pending_note(pending)}"


def _run(args: argparse.Namespace, ctx: Context) -> int:
    paths = OwnedPaths.from_env()
    state = IntegrityState(paths, root_dir=Path(str(args.root_dir)))
    audit = AuditLog(paths)
    user = _account_name()
    out = ctx.output
    if args.ack:
        report = state.acknowledge_report(audit, user)
        out.emit(
            {
                "status": "acknowledged",
                "changed": report.changed,
                "pending": None,
                "discarded": report.discarded,
            },
            render=lambda: _ack_text(report),
        )
        return 0
    report = state.check_report(audit, user)
    if report.created:
        out.emit(
            {"status": "baseline-created", "changed": [], "pending": report.pending},
            render=lambda: _created_text(report.count, report.pending),
        )
        return 0
    if report.changed:
        out.warn(warn("GINN-W-HOST-CHANGED", paths=", ".join(report.changed)))
        out.emit(
            {"status": "changed", "changed": report.changed, "pending": report.changed},
            render=lambda: (
                f"host changed since baseline: {len(report.changed)} path(s) listed "
                "above; run 'ginn integrity --ack' after review"
            ),
        )
        return 0
    out.emit(
        {"status": "unchanged", "changed": [], "pending": report.pending},
        render=lambda: _unchanged_text(report.pending),
    )
    return 0


SPEC = CommandSpec(
    name="integrity",
    help="check host files against the integrity baseline, or acknowledge a change (FR-006)",
    example="ginn integrity --ack",
    configure=_configure,
    run=_run,
)
