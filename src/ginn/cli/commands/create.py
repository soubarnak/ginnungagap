# SPDX-License-Identifier: Apache-2.0
"""`ginn create <distro> [--name REALM] [--tier ...] [--services on|off] [--with PKG ...]`.

Create a stopped realm, services off by default (FR-010), Basic by default. `--tier` takes basic,
development or custom (FR-060), and each tier's options are in security/caps.py (NFR-SEC-002).
`--services on` creates the container and leaves it stopped (ADR-003, GINN-W-SERVICES-PENDING):
its start goes through its runit entry, which arrives in T7.3 and T7.4. The options that the build
does not have yet are refused before anything is written, with GINN-E-NOT-SUPPORTED-YET and the
milestone that adds them: `--with` and `--everything` on a services-on realm (T7.4), and `--resume`
(M8, T8.5). A failure that ops/create.py reports is raised as it is. A real services-off create runs
its package steps; a services-on create runs none, since it starts nothing. Only the tests and the
host gate set `CreateHooks.integrate` to False, which skips them (T4.9 Gate 3).

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
from ginn.errors import GinnError, make
from ginn.ops.create import CreateRequest, CreateResult, create_realm, human_bytes
from ginn.runtime.spec import KALI

TIERS: Final = tuple(tier.value for tier in Tier)
_NOT_YET: Final = "GINN-E-NOT-SUPPORTED-YET"
_YES: Final = frozenset({"y", "yes"})
_KALI_NOTE: Final = (
    "Kali (FR-026, FR-027, R-002, OOS-9): a Kali realm has its own network namespace with outbound "
    "access, and raw sockets work inside that namespace only. Kali realms cannot do Wi-Fi monitor "
    "mode, change the host's network interfaces, or run raw-socket or ARP scans of physical "
    "networks. Use Kali realms for authorized testing only."
)


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "distro", metavar="DISTRO", help="distribution to build from: arch, debian, fedora or kali"
    )
    parser.add_argument(
        "--name",
        metavar="REALM",
        help="realm name, lowercase, up to 32 characters (default: the distro name)",
    )
    parser.add_argument(
        "--tier",
        choices=TIERS,
        default=Tier.BASIC.value,
        help="permission tier: basic, development or custom (default: basic, FR-060)",
    )
    parser.add_argument(
        "--services",
        choices=("on", "off"),
        default="off",
        help="run systemd inside the realm (default: off; on creates the realm stopped, see T7.4)",
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
        help="resume an interrupted create (arrives in M8)",
    )
    parser.epilog = _KALI_NOTE


def _not_yet(name: RealmName, feature: str, milestone: str) -> GinnError:
    return make(_NOT_YET, realm=name.value, feature=feature, milestone=milestone)


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


def _refuse_unbuilt(
    args: argparse.Namespace, name: RealmName, distro: str, *, json_mode: bool
) -> None:
    """Refuse an option this build cannot run, or one that would need a yes nobody can give.

    Both checks run before any write. The services-on refusal of `--everything` is made by
    ops/_create_plan.py, which runs before the first write too.
    """
    if args.resume:
        raise _not_yet(name, "create --resume", "M8")
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


def _text(result: CreateResult) -> str:
    record = result.record
    lines = [f"realm {record.settings.name.value} created: state {record.state.value}"]
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
    distro = str(args.distro)
    name = RealmName(str(args.name or distro))
    _refuse_unbuilt(args, name, distro, json_mode=ctx.output.json_mode)
    realms = _context.realms(ctx.output)
    _context.settle_interrupted(realms, ctx.output)
    request = CreateRequest(
        name=name,
        distro=distro,
        uid=realms.host.uid,
        gid=realms.gid,
        host_home=realms.host.host_home,
        host_env=realms.host.host_env,
        tier=Tier.parse(str(args.tier)),
        services=args.services == "on",
        everything=bool(args.everything),
        with_packages=tuple(args.with_packages or ()),
    )
    confirm: Callable[[str], bool] = _confirmed if args.yes else _ask
    hooks = replace(_context.create_hooks(realms.paths), confirm=confirm)
    result = create_realm(realms.env, realms.podman, request, hooks=hooks)
    for notice in result.warnings:
        ctx.output.warn(notice)
    _context.finish(realms, ctx.output)
    record = result.record
    estimate = result.estimate
    payload: dict[str, object] = {
        "name": record.settings.name.value,
        "distro": distro,
        "state": record.state.value,
        "tier": record.settings.tier.value,
        "services": record.settings.services,
        "generation": record.generation,
        "image_digest": record.image_digest,
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
