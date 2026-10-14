# SPDX-License-Identifier: Apache-2.0
"""`ginn create <distro> [--name REALM] [--tier ...] [--services on|off] [--with PKG ...]`.

Create a stopped realm, services off by default (FR-010), Basic by default. `--tier` takes basic,
development or custom (FR-060), and each tier's options are in security/caps.py (NFR-SEC-002).
`--services on` creates the container and leaves it stopped (ADR-003, GINN-W-SERVICES-PENDING):
its start goes through its runit entry, and the CLI never starts a services-on container. The
options that the build does not have yet are refused before anything is written, with
GINN-E-NOT-SUPPORTED-YET: `--with` and `--everything` on a services-on create, since the packages
go in after the realm's first supervised start, with `ginn install`. A failure that ops/create.py
reports is raised as it is. A real services-off create runs its package steps; a services-on create
runs none, since it starts nothing. Only the tests and the host gate set `CreateHooks.integrate` to
False, which skips them (T4.9 Gate 3).

`ginn create --resume <realm>` finishes an interrupted create (FR-122, ops/resume.py). The realm
name is the argument, and the options of the create come from the realm's record. So any other
option with `--resume` is GINN-E-USAGE, and only `--yes` is allowed. A Kali everything set that the
create did not confirm is asked for as a create asks, and `--yes` answers it.

Kali (FR-026, FR-027, FR-014). A Kali realm is created like any other, on pasta with NET_RAW
(runtime/spec.py). `--everything` installs the Kali metapackage. Its size is estimated from the
realm's package lists after the refresh, and the install waits for a yes: the prompt on a terminal,
or `--yes` for scripts. Without a terminal, or with --json, it needs `--yes`, and the create is
refused before any write (GINN-E-USAGE). A no at the prompt rolls the realm back. Any other
distribution gets GINN-E-OPTION-INVALID (FR-014 AC-3).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import replace
from typing import Final

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName, Tier
from ginn.errors import make
from ginn.ops.create import CreateRequest, CreateResult, create_realm, human_bytes
from ginn.ops.resume import resume_create
from ginn.runtime.spec import KALI

TIERS: Final = tuple(tier.value for tier in Tier)
_YES: Final = frozenset({"y", "yes"})
_KALI_NOTE: Final = (
    "Kali (FR-026, FR-027, R-002, OOS-9): a Kali realm has its own network namespace with outbound "
    "access, and raw sockets work inside that namespace only. Kali realms cannot do Wi-Fi monitor "
    "mode, change the host's network interfaces, or run raw-socket or ARP scans of physical "
    "networks. Use Kali realms for authorized testing only."
)


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "distro",
        metavar="DISTRO",
        help="distribution to build from: arch, debian, fedora or kali; with --resume, the "
        "realm to resume",
    )
    parser.add_argument(
        "--name",
        metavar="REALM",
        help="realm name, lowercase, up to 32 characters (default: the distro name)",
    )
    parser.add_argument(
        "--tier",
        choices=TIERS,
        default=None,
        help="permission tier: basic, development or custom (default: basic, FR-060)",
    )
    parser.add_argument(
        "--services",
        choices=("on", "off"),
        default=None,
        help="run systemd inside the realm (default: off; on creates it stopped, for runit)",
    )
    parser.add_argument(
        "--with",
        dest="with_packages",
        action="extend",
        nargs="+",
        metavar="PKG",
        help="install these packages with their dependencies, after the integration set",
    )
    parser.add_argument(
        "--everything",
        action="store_true",
        help="install the Kali everything metapackage, after its size is estimated and "
        "confirmed (Kali only, FR-014)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="answer yes to the --everything prompt without asking, for scripts (FR-014)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="finish the interrupted create of the realm named by DISTRO, with no other option "
        "(FR-122)",
    )
    parser.epilog = _KALI_NOTE


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


def _confirmed(_prompt: str) -> bool:
    """The answer of `--yes`: the prompt is not shown, and the install goes on."""
    return True


def _refuse_unbuilt(args: argparse.Namespace, distro: str, *, json_mode: bool) -> None:
    """Refuse a request that needs a yes nobody can give, or the Kali option on another distro.

    Both checks run before any write. The services-on refusal of `--everything` is made by
    ops/_create_plan.py, which runs before the first write too.
    """
    if not args.everything:
        return
    if distro != KALI:
        raise make("GINN-E-OPTION-INVALID", option="--everything", distro=distro)
    if args.yes:
        return
    if json_mode:
        raise make(
            "GINN-E-USAGE",
            detail="--everything with --json needs --yes: a JSON run does not prompt",
        )
    if not _can_prompt():
        raise make(
            "GINN-E-USAGE",
            detail="--everything needs a confirmation: answer the prompt on a terminal, "
            "or pass --yes",
        )


def _refuse_with_resume(args: argparse.Namespace) -> None:
    """`--resume` takes the realm name and `--yes` only. Any other create option is a usage error.

    The options of the interrupted create are kept in its record (ops/resume.py), so a second copy
    on the command line could only disagree with them. The check runs before any write.
    """
    given = [
        flag
        for flag, value in (
            ("--name", args.name),
            ("--tier", args.tier),
            ("--services", args.services),
            ("--with", args.with_packages),
            ("--everything", args.everything),
        )
        if value
    ]
    if given:
        detail = f"--resume takes only the realm name and --yes, not {', '.join(given)}"
        raise make("GINN-E-USAGE", detail=detail)


def _text(result: CreateResult) -> str:
    record = result.record
    verb = "resumed" if result.resumed else "created"
    lines = [f"realm {record.settings.name.value} {verb}: state {record.state.value}"]
    if result.locale_note is not None:
        lines.append(f"note: {result.locale_note}")
    if result.gpu_note is not None:
        lines.append(f"note: {result.gpu_note}")
    if result.estimate is not None:
        estimate = result.estimate
        lines.append(
            f"everything: {estimate.packages} packages, download "
            f"{human_bytes(estimate.download_bytes)}, installed "
            f"{human_bytes(estimate.installed_bytes)}"
        )
    return "\n".join(lines)


def _run(args: argparse.Namespace, ctx: Context) -> int:
    if args.resume:
        return _resume(args, ctx)
    distro = str(args.distro)
    name = RealmName(str(args.name or distro))
    _refuse_unbuilt(args, distro, json_mode=ctx.output.json_mode)
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    request = CreateRequest(
        name=name,
        distro=distro,
        uid=realms.host.uid,
        gid=realms.gid,
        host_home=realms.host.host_home,
        host_env=realms.host.host_env,
        tier=Tier.parse(str(args.tier or Tier.BASIC.value)),
        services=(args.services or "off") == "on",
        everything=bool(args.everything),
        with_packages=tuple(args.with_packages or ()),
    )
    confirm: Callable[[str], bool] = _confirmed if args.yes else _ask
    hooks = replace(_context.create_hooks(realms.paths), confirm=confirm)
    result = create_realm(realms.env, realms.podman, request, hooks=hooks)
    return _report(result, realms, ctx)


def _resume(args: argparse.Namespace, ctx: Context) -> int:
    """`create --resume REALM`: finish the realm's interrupted create (FR-122, ops/resume.py).

    The answer to the Kali prompt is `--yes`, or the prompt on a terminal. Without either, a
    resume that needs the answer is refused by ops/resume.py before any write, as a create is.
    """
    name = RealmName(str(args.distro))
    _refuse_with_resume(args)
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    confirm: Callable[[str], bool] | None = None
    if args.yes:
        confirm = _confirmed
    elif _can_prompt() and not ctx.output.json_mode:
        confirm = _ask
    hooks = replace(_context.create_hooks(realms.paths), confirm=confirm)
    result = resume_create(
        realms.env,
        realms.podman,
        name,
        uid=realms.host.uid,
        gid=realms.gid,
        host_home=realms.host.host_home,
        host_env=realms.host.host_env,
        hooks=hooks,
    )
    return _report(result, realms, ctx)


def _report(result: CreateResult, realms: _context.Realms, ctx: Context) -> int:
    """Show the warnings and the result of a create or a resume, and finish the adapter's output."""
    for notice in result.warnings:
        ctx.output.warn(notice)
    _context.finish(realms, ctx.output)
    record = result.record
    estimate = result.estimate
    payload: dict[str, object] = {
        "name": record.settings.name.value,
        "distro": record.settings.distro.value,
        "state": record.state.value,
        "tier": record.settings.tier.value,
        "services": record.settings.services,
        "generation": record.generation,
        "image_digest": record.image_digest,
        "resumed": result.resumed,
        "locale_note": result.locale_note,
        "gpu_note": result.gpu_note,
        "everything": None
        if estimate is None
        else {
            "packages": estimate.packages,
            "download_bytes": estimate.download_bytes,
            "installed_bytes": estimate.installed_bytes,
        },
    }
    ctx.output.emit(payload, render=lambda: _text(result))
    return 0


SPEC = CommandSpec(
    name="create",
    help="create a stopped realm from the verified image (FR-010)",
    example="ginn create debian --name debian-dev --with htop --json",
    configure=_configure,
    run=_run,
)
