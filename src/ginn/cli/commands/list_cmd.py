# SPDX-License-Identifier: Apache-2.0
"""`ginn list [--json]`: every realm of this user, sorted by name (FR-020, FR-025, AS-025).

The JSON result is `{"realms": [...]}`, one `RealmStatus.to_json()` object per realm. A record
that cannot be read is listed with its `error`, and the rest of the list is shown. Only this
user's realms are listed, because the Owned Paths are this user's (FR-025). Recovery runs first
and a failure becomes a warning, as in `status`.
"""

from __future__ import annotations

import argparse

from ginn.cli import _context
from ginn.cli._views import list_text
from ginn.cli.commands import CommandSpec, Context
from ginn.ops.lifecycle import list_realms


def _configure(parser: argparse.ArgumentParser) -> None:
    """`list` takes no arguments; --json is a root flag (IR-010)."""


def _run(args: argparse.Namespace, ctx: Context) -> int:
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output, read_only=True)
    statuses = list_realms(realms.env, realms.podman, hooks=_context.lifecycle_hooks())
    _context.finish(realms, ctx.output)
    payload = {"realms": [status.to_json() for status in statuses]}
    ctx.output.emit(payload, render=lambda: list_text(statuses))
    return 0


SPEC = CommandSpec(
    name="list",
    help="list every realm with its state, container, tier and disk use (FR-020)",
    example="ginn list --json",
    configure=_configure,
    run=_run,
)
