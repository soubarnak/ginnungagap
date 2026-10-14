# SPDX-License-Identifier: Apache-2.0
"""`ginn create <distro> [--name REALM] [--tier ...] [--services on|off] [--with PKG ...]`.

Create a stopped realm with services off (FR-010), Basic by default. `--tier` takes basic,
development or custom (FR-060), and each tier's options are in security/caps.py (NFR-SEC-002). The
options that the build does not have yet are refused before anything is written, with
GINN-E-NOT-SUPPORTED-YET and the milestone that adds them: services on (M7), `--everything` (M8,
T8.7, Kali only: other distributions get GINN-E-OPTION-INVALID, FR-014 AC-3), `--resume` (M8,
T8.5) and Kali itself (M10). A failure that ops/create.py reports is raised
as it is. A real create runs its package steps. Only the tests and the host gate set
`Seams.integrate` to False, which skips them (CreateHooks.integrate, T4.9 Gate 3).
"""

from __future__ import annotations

import argparse
from typing import Final

from ginn.cli import _context
from ginn.cli.commands import CommandSpec, Context
from ginn.domain import RealmName, Tier
from ginn.errors import CATALOGUE, GinnError, make
from ginn.ops.create import CreateRequest, CreateResult, create_realm
from ginn.runtime.spec import KALI

TIERS: Final = tuple(tier.value for tier in Tier)
_NOT_YET: Final = "GINN-E-NOT-SUPPORTED-YET"


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "distro", metavar="DISTRO", help="distribution to build from: arch, debian or fedora"
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
        help="run systemd inside the realm (default: off; on arrives in M7)",
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
        help="install the Kali everything metapackage (Kali only; arrives in M8)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume an interrupted create (arrives in M8)",
    )


def _not_yet(name: RealmName, feature: str, milestone: str) -> GinnError:
    return make(_NOT_YET, realm=name.value, feature=feature, milestone=milestone)


def _refuse_unbuilt(args: argparse.Namespace, name: RealmName, distro: str) -> None:
    """Refuse an option or a distribution that this build cannot create yet, before any write."""
    if args.resume:
        raise _not_yet(name, "create --resume", "M8")
    if args.everything:
        if distro != KALI:
            raise make("GINN-E-OPTION-INVALID", option="--everything", distro=distro)
        raise _not_yet(name, "create --everything", "M8")
    if distro == KALI:
        raise _not_yet(name, "create of a kali realm", "M10")
    if args.services == "on":
        raise _not_yet(name, "create of a services-on realm", "M7")


def _text(result: CreateResult) -> str:
    record = result.record
    lines = [f"realm {record.settings.name.value} created: state {record.state.value}"]
    if result.locale_note is not None:
        lines.append(f"note: {result.locale_note}")
    if result.gpu_note is not None:
        lines.append(f"note: {result.gpu_note}")
    return "\n".join(lines)


def _run(args: argparse.Namespace, ctx: Context) -> int:
    distro = str(args.distro)
    name = RealmName(str(args.name or distro))
    _refuse_unbuilt(args, name, distro)
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
        services=False,
        everything=False,
        with_packages=tuple(args.with_packages or ()),
    )
    try:
        result = create_realm(realms.env, realms.podman, request, hooks=_context.create_hooks())
    except NotImplementedError as exc:
        # The spec refuses what _refuse_unbuilt does not name (RealmSpec, runtime/spec.py).
        message = f"realm '{name.value}' cannot be used yet: {exc}"
        raise GinnError(
            _NOT_YET,
            message,
            CATALOGUE[_NOT_YET].remedy,
            CATALOGUE[_NOT_YET].exit_code,
            details={"realm": name.value, "feature": str(exc)},
        ) from exc
    for notice in result.warnings:
        ctx.output.warn(notice)
    _context.finish(realms, ctx.output)
    record = result.record
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
