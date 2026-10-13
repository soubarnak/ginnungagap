# SPDX-License-Identifier: Apache-2.0
"""`ginn delete <realm> [--purge-home] [--yes]`: remove a realm (FR-019, AS-014).

The realm's containers, image (when no other container uses it) and record go. The Realm Home is
kept unless `--purge-home` is given and confirmed (AS-014). Confirmation is a prompt that defaults
to no, and it is asked only when both stdin and stderr are terminals. A script, and every `--json`
run, passes `--yes` instead. Without `--yes`, a purge that cannot be confirmed is refused before
anything runs (GINN-E-USAGE): no catalogued code is a confirmation refusal. The Realm Home checks of
a purge run before the prompt (`check_purge`), so a purge that cannot run is refused without a
question. `delete_realm` runs the prompt, then checks again under the locks. The FR-006 warnings of
the delete are written before the result.
"""

from __future__ import annotations

import argparse
import sys

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName
from ginn.errors import make
from ginn.ops.delete import DeleteResult, check_purge, delete_realm

_YES: frozenset[str] = frozenset({"y", "yes"})


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("realm", metavar="REALM", help="realm to delete")
    parser.add_argument(
        "--purge-home",
        action="store_true",
        help="also remove the Realm Home, after confirmation; it cannot be undone (AS-014)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm --purge-home without the prompt, for scripts",
    )


def _is_terminal(stream: object) -> bool:
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except ValueError:
        return False


def _can_prompt() -> bool:
    """True when the prompt can be seen and answered: stdin and stderr are both terminals."""
    return _is_terminal(sys.stdin) and _is_terminal(sys.stderr)


def _ask(prompt: str) -> bool:
    """Ask on stderr and read stdin. Only y or yes confirms; end of input declines."""
    sys.stderr.write(f"{prompt} [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in _YES


def _text(result: DeleteResult) -> str:
    lines = [f"realm {result.realm} deleted"]
    if result.purge_declined:
        lines.append("purge declined: the Realm Home is kept")
    elif result.home == "removed":
        lines.append("Realm Home removed")
    elif result.home == "kept":
        lines.append("Realm Home kept")
    if result.image_code is not None:
        lines.append(f"image kept: {result.image_code}")
    lines.extend(f"note: {note}" for note in result.notes)
    return "\n".join(lines)


def _run(args: argparse.Namespace, ctx: Context) -> int:
    name = RealmName(str(args.realm))
    purge = bool(args.purge_home)
    if purge and not args.yes:
        if ctx.output.json_mode:
            raise make(
                "GINN-E-USAGE",
                detail="--purge-home with --json needs --yes: a JSON run does not prompt",
            )
        if not _can_prompt():
            raise make(
                "GINN-E-USAGE",
                detail="--purge-home needs a confirmation: answer the prompt on a terminal, "
                "or pass --yes",
            )
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    if purge:
        check_purge(realms.env, name, hooks=_context.delete_hooks())
    confirm = (lambda _prompt: True) if args.yes else _ask
    result = delete_realm(
        realms.env,
        realms.podman,
        name,
        purge_home=purge,
        confirm=confirm,
        hooks=_context.delete_hooks(),
    )
    _context.finish(realms, ctx.output)
    for notice in result.warnings:
        ctx.output.warn(notice)
    payload: dict[str, object] = {
        "realm": result.realm,
        "home": result.home,
        "purge_declined": result.purge_declined,
        "image": result.image,
        "image_code": result.image_code,
        "notes": list(result.notes),
    }
    ctx.output.emit(payload, render=lambda: _text(result))
    return 0


SPEC = CommandSpec(
    name="delete",
    help="delete a realm; its Realm Home is kept unless --purge-home is confirmed (FR-019)",
    example="ginn delete debian --json",
    configure=_configure,
    run=_run,
)
