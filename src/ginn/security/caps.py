# SPDX-License-Identifier: Apache-2.0
"""The podman options that a tier or a Custom toggle adds to a realm (FR-060..063, NFR-SEC-002).

docs/capabilities.md lists every option that ginn adds beyond the base argv. This module produces
that set for the toggles, and the test suite compares the two, so they cannot drift.

Create-time options per toggle (ADR-006, the Custom toggles table of capabilities.md):
- `nested` (FR-062 AC-3): `--device /dev/fuse`, `--device /dev/net/tun`, `--security-opt
  unmask=/proc/*`, and `--security-opt seccomp=<generated profile>`. The profile is made by
  security/seccomp.py, and its path is passed in here. The host must have the two devices (checked
  by ops/_create_plan.py before any write).
- `kvm` (FR-062 AC-4): `--device /dev/kvm` and `--group-add keep-groups`, only when the host has
  `/dev/kvm` at create (IR-040). Without the device, keep-groups has no purpose, so it is not added.

The other toggles change attach-time grants only (runtime/mounts.py, runtime/env.py): `projects`,
`shell_rc`, `downloads`, `mount_roots`, `display`, `audio` and each Custom share, attached after
start (ADR-008). `agent` adds no create-time option. The Agent Proxy sockets are attach-time grants
of security/agent.py (FR-067, T6.4). The `gpu` device and CDI grant and the `fonts_timezone` binds
are create-time grants of runtime/mounts.py and ops/_create_plan.py, not options: the record keeps
their toggle state at create (`gpu_at_create`, `fonts_at_create`).

`TierExtras` is the only way another module adds a create-time option. It is checked against an
allowlist, and nothing outside the allowlist passes (NFR-SEC-002, NFR-SEC-005):
- devices: a host device node under `/dev/dri`, `/dev/kvm`, `/dev/fuse` or `/dev/net/tun`.
- security options: exactly `unmask=/proc/*`. `seccomp=` is never taken from an extra; the profile
  is ginn's own, from this module's `seccomp_profile` argument.
- group-add: a plain group name, never `keep-groups`, and never with keep-groups already present.
- mounts: each source passes the DR-004 rules (domain/paths.py). They are create-time binds.
- CDI: a `CdiGrant` (for example `nvidia.com/gpu=all`, FR-069), with its spec directory under an
  Owned Path (DR-010). It reaches podman as the global `--cdi-spec-dir` flag and a `--device`.

Nothing here adds a capability, a privileged flag, an unconfined seccomp or AppArmor profile, a
label opt-out, `systempaths=unconfined`, `unmask=ALL` or `no-new-privileges=false` (NFR-SEC-001,
NFR-SEC-002). podman/adapter.py refuses those spellings at argv time too, as a second check.

`option_keys` gives the toggle-driven options as canonical names, such as `device=/dev/fuse`. The
realm record keeps these names as the options its container was made with (`created_options`), and
a tier or permission change that would change them is refused, because the container would need a
new create (T8.3, ADR-006). The seccomp path and the extras are left out of the names.

Pure, except the DR-004 check of extra mounts and the realpath of the paths that must lie under an
Owned Path. The caller passes the host probe result and the paths.
"""

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from ginn.domain.paths import Mode, SharedPath
from ginn.domain.permissions import Toggle
from ginn.podman.adapter import Mount

KVM_DEVICE: Final = "/dev/kvm"
FUSE_DEVICE: Final = "/dev/fuse"
TUN_DEVICE: Final = "/dev/net/tun"
NESTED_DEVICES: Final[tuple[str, ...]] = (FUSE_DEVICE, TUN_DEVICE)
"""The inner storage and network devices of ADR-006 (fuse-overlayfs, and pasta's tun)."""
NESTED_SECURITY_OPTS: Final[tuple[str, ...]] = ("unmask=/proc/*",)
"""The proc unmask that the inner proc mount needs (ADR-006). The seccomp profile follows it."""
SECCOMP: Final = "seccomp"
KEEP_GROUPS: Final = "keep-groups"
"""Keeps the host's supplementary groups, so the realm user keeps the kvm group (ADR-006)."""
PLACEHOLDER: Final = "<generated profile>"
"""The value that a key stands for in the document, where the profile path would be."""
EXTRA_DEVICE_PREFIXES: Final[tuple[str, ...]] = ("/dev/dri", KVM_DEVICE, FUSE_DEVICE, TUN_DEVICE)
"""The only host device nodes an extra may name. A prefix matches the node and what is under it."""
EXTRA_SECURITY_OPTS: Final[frozenset[str]] = frozenset(NESTED_SECURITY_OPTS)
"""The allowlist of security options an extra may carry: exact matches, nothing else."""
_FORBIDDEN_IN_VALUE: Final = frozenset(",:=\0\n\r")
"""Characters that podman's `--device`, `--security-opt` and `--mount` syntax would split on."""
_GROUP: Final = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_CDI_DEVICE: Final = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+/[a-z0-9_-]+=[A-Za-z0-9_.-]+")


def _plain(value: str) -> bool:
    """True for a non-empty value with no edge spaces and no character podman would split on."""
    return value != "" and value == value.strip() and not (_FORBIDDEN_IN_VALUE & set(value))


def _within(path: Path, roots: Iterable[Path]) -> bool:
    """True when `path`, resolved, is one of `roots` or lies under one (DR-010)."""
    resolved = Path(os.path.realpath(path))
    return any(
        resolved == Path(os.path.realpath(root)) or Path(os.path.realpath(root)) in resolved.parents
        for root in roots
    )


@dataclass(frozen=True)
class CdiGrant:
    """A CDI device of one realm, such as `nvidia.com/gpu=all` (FR-069, ADR-012).

    `spec_dir` holds the CDI spec that the device name refers to. It is an Owned Path (DR-010), and
    the create passes it as podman's global `--cdi-spec-dir` flag, so it is a path, not an option.
    """

    spec_dir: Path
    device: str

    def __post_init__(self) -> None:
        if not self.spec_dir.is_absolute() or not _plain(str(self.spec_dir)):
            raise ValueError("NFR-SEC-005: a CDI spec directory must be a plain absolute path")
        if _CDI_DEVICE.fullmatch(self.device) is None:
            raise ValueError(f"NFR-SEC-005: {self.device!r} is not a vendor/class=name CDI device")


@dataclass(frozen=True)
class TierExtras:
    """Create-time options from another security module, all empty by default (NFR-SEC-002).

    Each field is checked against the allowlist of the module docstring before any of it is used.
    `mounts` are create-time binds, checked against DR-004. A variable grant is attached after start
    instead (ADR-008), so it does not belong here.
    """

    devices: tuple[str, ...] = ()
    security_opts: tuple[str, ...] = ()
    group_add: tuple[str, ...] = ()
    mounts: tuple[Mount, ...] = ()
    cdi: CdiGrant | None = None


@dataclass(frozen=True)
class CreateOptions:
    """The create-time part of a realm's tier: options beyond the Basic binds of spec.py.

    The lists keep their order. An empty value is the Basic set, and `spec.py` adds it to the
    stable binds of the base argv. `cdi` is the one option that Basic may hold, for the GPU grant of
    FR-069 (ADR-012).
    """

    devices: tuple[str, ...] = ()
    security_opts: tuple[str, ...] = ()
    group_add: tuple[str, ...] = ()
    mounts: tuple[Mount, ...] = ()
    cdi: CdiGrant | None = None


def _device_allowed(device: str) -> bool:
    if not _plain(device) or not device.startswith("/") or ".." in device.split("/"):
        return False
    return any(
        device == prefix or device.startswith(prefix + "/") for prefix in EXTRA_DEVICE_PREFIXES
    )


def _checked_mounts(
    mounts: Sequence[Mount],
    *,
    home: Path | None,
    owned: Sequence[Path],
    env: Mapping[str, str] | None,
) -> tuple[Mount, ...]:
    """Each extra bind, checked with DR-004 (SharedPath) and kept at its own path (FR-065)."""
    checked: list[Mount] = []
    for mount in mounts:
        if mount.type != "bind":
            raise ValueError("NFR-SEC-002: an extra mount must be a bind of a host path")
        if home is None or env is None:
            raise ValueError(
                "NFR-SEC-002: an extra mount needs the home and environment for DR-004"
            )
        if mount.target != mount.source:
            raise ValueError("FR-065: an extra mount sits at the same path inside the realm")
        if any(option not in ("ro", "rw") for option in mount.options):
            raise ValueError("NFR-SEC-002: an extra mount may carry only the ro or rw option")
        mode: Mode = "ro" if "ro" in mount.options else "rw"
        shared = SharedPath.parse(mount.source, home=home, owned_paths=owned, mode=mode, env=env)
        checked.append(Mount("bind", str(shared.path), str(shared.path), mount.options))
    return tuple(checked)


def _checked(
    extras: TierExtras,
    *,
    toggled_groups: Sequence[str],
    home: Path | None,
    owned: Sequence[Path],
    env: Mapping[str, str] | None,
) -> TierExtras:
    """The extras, each checked against the allowlist. Raises ValueError on the first refusal."""
    for device in extras.devices:
        if not _device_allowed(device):
            raise ValueError(f"NFR-SEC-002: device {device!r} is not an allowed host device node")
    for option in extras.security_opts:
        if option not in EXTRA_SECURITY_OPTS:
            raise ValueError(f"NFR-SEC-002: security option {option!r} is not on the allowlist")
    for group in extras.group_add:
        if group == KEEP_GROUPS or _GROUP.fullmatch(group) is None:
            raise ValueError(f"NFR-SEC-002: group {group!r} is not a plain group name")
    if extras.group_add and KEEP_GROUPS in toggled_groups:
        raise ValueError("NFR-SEC-002: an extra group-add may not sit beside keep-groups (ADR-006)")
    cdi = extras.cdi
    if cdi is not None and (not owned or not _within(cdi.spec_dir, owned)):
        raise ValueError("DR-010: a CDI spec directory must lie under an Owned Path")
    mounts = _checked_mounts(extras.mounts, home=home, owned=owned, env=env)
    return replace(extras, mounts=mounts)


def _parts(
    toggles: Iterable[Toggle], *, kvm_present: bool, seccomp: str | None
) -> tuple[list[str], list[str], list[str]]:
    """The devices, security options and kept groups that the toggles add, in document order."""
    chosen = frozenset(toggles)
    devices: list[str] = []
    security: list[str] = []
    groups: list[str] = []
    if Toggle.NESTED in chosen:
        if seccomp is None:
            raise ValueError("the nested toggle needs the seccomp profile path (ADR-006)")
        devices += NESTED_DEVICES
        security += [*NESTED_SECURITY_OPTS, f"{SECCOMP}={seccomp}"]
    if Toggle.KVM in chosen and kvm_present:
        devices.append(KVM_DEVICE)
        groups.append(KEEP_GROUPS)
    return devices, security, groups


def _keys(devices: list[str], security: list[str], groups: list[str]) -> list[str]:
    keys = [f"device={device}" for device in devices]
    keys += [f"security-opt={option.partition('=')[0]}" for option in security]
    keys += [f"group-add={group}" for group in groups]
    return keys


def option_keys(toggles: Iterable[Toggle], *, kvm_present: bool) -> list[str]:
    """The canonical names of the toggle-driven create options, sorted and without duplicates.

    The seccomp path is left out, so the names do not depend on the host profile. Extras are not in
    the names: they come from other modules, and are not compared at start.
    """
    devices, security, groups = _parts(toggles, kvm_present=kvm_present, seccomp=PLACEHOLDER)
    return sorted(set(_keys(devices, security, groups)))


def create_options(
    toggles: Iterable[Toggle],
    *,
    kvm_present: bool,
    seccomp_profile: Path | None,
    extras: TierExtras | None = None,
    home: Path | None = None,
    owned_paths: Sequence[Path] = (),
    env: Mapping[str, str] | None = None,
) -> CreateOptions:
    """The create-time options of a realm with `toggles`, with the checked extras added at the end.

    `kvm_present` is the probe result for `/dev/kvm` at create. `seccomp_profile` is the generated
    profile, which must be given when `nested` is on, and which must lie under `owned_paths` when
    those are given. `home` and `env` are the DR-004 inputs for extra mounts. Raises ValueError when
    an input or an extra is refused.
    """
    toggled = frozenset(toggles)
    if seccomp_profile is not None:
        if not seccomp_profile.is_absolute() or not _plain(str(seccomp_profile)):
            raise ValueError("NFR-SEC-005: the seccomp profile path must be plain and absolute")
        if owned_paths and not _within(seccomp_profile, owned_paths):
            raise ValueError("DR-010: the seccomp profile must lie under an Owned Path")
    seccomp = None if seccomp_profile is None else str(seccomp_profile)
    devices, security, groups = _parts(toggled, kvm_present=kvm_present, seccomp=seccomp)
    checked = _checked(
        TierExtras() if extras is None else extras,
        toggled_groups=groups,
        home=home,
        owned=owned_paths,
        env=env,
    )
    return CreateOptions(
        devices=_unique([*devices, *checked.devices]),
        security_opts=_unique([*security, *checked.security_opts]),
        group_add=_unique([*groups, *checked.group_add]),
        mounts=checked.mounts,
        cdi=checked.cdi,
    )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


__all__ = [
    "KEEP_GROUPS",
    "KVM_DEVICE",
    "NESTED_DEVICES",
    "NESTED_SECURITY_OPTS",
    "PLACEHOLDER",
    "CdiGrant",
    "CreateOptions",
    "TierExtras",
    "create_options",
    "option_keys",
]
