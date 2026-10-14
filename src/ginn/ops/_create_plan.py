# SPDX-License-Identifier: Apache-2.0
"""What a create is asked for, and what it will build, checked before any side effect (AS-013).

`CreateRequest` is the user's request and `CreateHooks` the seams for the host and the clock.
`_validate` turns the request into a `_Plan`: the settings, the distribution definition, the
RealmSpec (the tier's options, services as requested, pinned by digest), the trust entry, the
validated `--with` names, the host session, the permissions of the tier, and the seccomp profile of
a realm with nested containers. It runs no podman and writes nothing (NFR-SEC-005). It reads the
profile source, so a missing source is refused before any write (ADR-006). ops/create.py runs the
plan.
FR-121's space estimates are here too: the base image's, and the Kali everything set's, read from
the package lists inside the realm (`size_estimate`). ops/disk.py applies the 1.10 factor and does
the free-space check.
"""

import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from ginn.distros import DistroDef
from ginn.distros import load as load_distro
from ginn.domain import (
    DistroId,
    PackageName,
    RealmName,
    RealmSettings,
    Tier,
)
from ginn.domain.permissions import Permissions, Toggle, effective_toggles, initial_permissions
from ginn.errors import make
from ginn.ops._mutating import OpEnv
from ginn.ops.disk import StatVfs, estimate_required
from ginn.ops.security_ops import nvidia_plan
from ginn.packages.manager import refused
from ginn.runtime.attach import MOUNTINFO
from ginn.runtime.env import SessionEnv, session_env
from ginn.runtime.mounts import Exists
from ginn.runtime.realm_security import SecurityHooks
from ginn.runtime.spec import (
    SERVICES_MITIGATIONS,
    RealmSpec,
    realm_passwd_entry,
    realm_spec,
)
from ginn.security import seccomp
from ginn.security.caps import KVM_DEVICE, NESTED_DEVICES, CdiGrant, TierExtras, option_keys
from ginn.security.nvidia import DEVICE as NVIDIA_DEVICE
from ginn.security.nvidia import NvidiaPlan, Status
from ginn.services.runit_user import conmon_pidfile, journald_dropin
from ginn.trust import manifest as trust
from ginn.trust.manifest import TrustEntry, TrustManifest

USER_NAME: Final = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
"""The `<user>` field of the realm's passwd entry (T4.9b, ADR-006 amendment). The entry is one
field, so there is no `:`, `$`, newline or leading `-`. The name is a lowercase login name: a
letter or `_` first, then up to 31 letters, digits, `_` or `-`."""
EVERYTHING_LISTING_MAX: Final = 32 * 1024 * 1024
"""Bytes the closure listing of the everything metapackage (`resolve`) may print (FR-014)."""
EVERYTHING_CHUNK: Final = 200
"""Package names per `estimate` call, so each call's listing stays under EVERYTHING_CHUNK_MAX."""
EVERYTHING_CHUNK_MAX: Final = 8 * 1024 * 1024
"""Bytes one `estimate` call may print: the stanzas of at most EVERYTHING_CHUNK packages."""
_KIB: Final = 1024
"""apt states Installed-Size in KiB (deb-control(5)), and Size in bytes."""


@dataclass(frozen=True)
class SizeEstimate:
    """The size of a package set, as FR-014 shows it before a download: download and installed.

    `download_bytes` is the sum of the packages' `Size` fields (the archives), and
    `installed_bytes` the sum of their `Installed-Size` fields in bytes.
    """

    packages: int
    download_bytes: int
    installed_bytes: int

    @property
    def total_bytes(self) -> int:
        """What FR-121 checks: the download and the installed size, before the 1.10 factor."""
        return self.download_bytes + self.installed_bytes


def estimate_bytes(entry: TrustEntry) -> int:
    """The base image's size estimate: compressed download plus installed size (FR-121).

    It covers the official image only. The Integration Package Set and `--with` closures are not
    counted (see the module docstring).
    """
    return entry.compressed_size_bytes + entry.estimated_installed_bytes


def required_bytes(entry: TrustEntry) -> int:
    """The free space FR-121 requires: the base estimate times 1.10, rounded up (ops/disk.py)."""
    return estimate_required(estimate_bytes(entry))


def closure_names(
    listing: str, name_pattern: re.Pattern[str], *, realm: str, step: str
) -> list[str]:
    """The packages of an `apt-cache depends --recurse` listing, in order, each once.

    A package starts its line at column 0, and its dependency lines below it are indented. A
    virtual package is written `<name>`, and nothing is downloaded for it, so both are skipped. A
    name the distribution's pattern does not accept is refused, since it is not read as a package.
    """
    names: dict[str, None] = {}
    for line in listing.splitlines():
        if not line or line[0].isspace() or line.startswith("<"):
            continue
        name = line.strip()
        if name_pattern.fullmatch(name) is None:
            raise refused(realm, step, f"a package name in the listing is not valid: {name[:64]!r}")
        names.setdefault(name, None)
    if not names:
        raise refused(realm, step, "the listing names no package")
    return list(names)


def size_totals(text: str, names: Sequence[str], *, realm: str, step: str) -> SizeEstimate:
    """The download and installed bytes of `names`, from an `apt-cache show` listing.

    Each stanza is one package's record, made of `Key: value` lines. A package must have both
    `Size` and `Installed-Size`, or the listing is refused, so a missing field never counts as zero.
    A stanza for a name that is not in `names`, or a repeated one, is not counted again.
    """
    sizes: dict[str, tuple[int, int]] = {}
    for stanza in text.split("\n\n"):
        values: dict[str, str] = {}
        for line in stanza.splitlines():
            key, sep, value = line.partition(": ")
            if sep and key in ("Package", "Size", "Installed-Size"):
                values[key] = value.strip()
        package = values.get("Package")
        if package is None or package in sizes:
            continue
        size, installed = values.get("Size", ""), values.get("Installed-Size", "")
        if not size.isdigit() or not installed.isdigit():
            raise refused(realm, step, f"package {package!r} has no usable Size or Installed-Size")
        sizes[package] = (int(size), int(installed) * _KIB)
    missing = [name for name in names if name not in sizes]
    if missing:
        raise refused(realm, step, f"no size for {len(missing)} package(s), e.g. {missing[0]!r}")
    return SizeEstimate(
        packages=len(names),
        download_bytes=sum(sizes[name][0] for name in names),
        installed_bytes=sum(sizes[name][1] for name in names),
    )


def human_bytes(count: int) -> str:
    """A size for a person, in the largest binary unit that keeps the figure at one or more."""
    if count < _KIB:
        return f"{count} bytes"
    value, unit = count / _KIB, "KiB"
    for larger in ("MiB", "GiB", "TiB"):
        if value < _KIB:
            break
        value, unit = value / _KIB, larger
    return f"{value:.1f} {unit}"


@dataclass(frozen=True)
class CreateRequest:
    """What the user asked for. `create_realm` checks it before anything is written.

    `uid`, `gid` and `host_home` are the host user's (AS-007). `host_env` is the host environment.
    Only its allowlisted variables are read (runtime/env.py). `with_packages` are plain package
    names (AS-027, FR-010).
    """

    name: RealmName
    distro: str
    uid: int
    gid: int
    host_home: Path
    host_env: Mapping[str, str] = field(default_factory=dict)
    tier: Tier = Tier.BASIC
    services: bool = False
    everything: bool = False
    with_packages: Sequence[str] = ()


@dataclass(frozen=True)
class CreateHooks:
    """The seams for the parts that touch the host or wait. The defaults are the real ones.

    `statvfs` is the filesystem probe of the FR-121 check (ops/disk.py). `sleep` waits between
    retries (FR-120). `exists`, `mountinfo` and `helper` are the probes and paths that runtime/
    reads. `manifest` replaces the packaged Trust Manifest. `on_step` runs after each journal step,
    so a caller can show progress (AS-018), or a test can inject a failure there.

    `confirm` answers the prompt of the Kali everything install (FR-014). It gets the prompt text
    and returns True to go on. Without it, or when it answers no, the install does not run and the
    create is rolled back.

    `integrate` False skips the refresh, the Integration Package Set and its steps, and records the
    `integrated` step with nothing run. Only the host gate sets it (T4.9, Gate 3: no package
    install and no refresh in a test realm). The CLI never sets it, so every real create refreshes.
    """

    statvfs: StatVfs = os.statvfs
    sleep: Callable[[float], None] = time.sleep
    exists: Exists = os.path.exists
    mountinfo: Path = MOUNTINFO
    helper: str | None = None
    manifest: TrustManifest | None = None
    on_step: Callable[[str], None] | None = None
    integrate: bool = True
    seccomp_source: Path = seccomp.DEFAULT_SOURCE
    """The host's default seccomp profile, read for a nested-container realm (ADR-006)."""
    extras: TierExtras | None = None
    """Options that another security module adds to the create (security/caps.py). None is none."""
    security: SecurityHooks = field(default_factory=SecurityHooks)
    """The NVIDIA probe and the other security seams of the create (FR-069, ADR-012)."""
    program: str | None = None
    """The absolute ginn command that a services-on realm's run and finish scripts execute."""
    confirm: Callable[[str], bool] | None = None
    """Answers the Kali everything prompt (FR-014). None declines, as delete does without one."""


@dataclass(frozen=True)
class _Plan:
    """Everything the body needs, all checked before the operation begins.

    `created` is the toggle-driven option names the container is made with (security/caps.py).
    `seccomp` is the profile of a nested-container realm, or None. `kvm_present` is the host probe.
    `toggles` are the effective toggles of the realm, and `gpu_cdi` is whether its NVIDIA CDI grant
    is made (the host driver is ready and the GPU toggle is on).
    """

    settings: RealmSettings
    definition: DistroDef
    spec: RealmSpec
    entry: TrustEntry
    packages: tuple[str, ...]
    everything: tuple[str, ...]
    """The Kali everything metapackages (FR-014), empty unless the request asks for them."""
    session: SessionEnv
    owned: list[Path]
    user: str
    permissions: Permissions
    created: list[str]
    seccomp: seccomp.SeccompPlan | None
    kvm_present: bool
    gpu: NvidiaPlan
    toggles: frozenset[Toggle]
    gpu_cdi: bool


def _refuse_missing_devices(exists: Exists) -> None:
    """The inner podman needs `/dev/fuse` and `/dev/net/tun` (ADR-006). A host without one fails.

    The probe is `hooks.exists`, so a test can answer it. The check runs before any write.
    """
    for device in NESTED_DEVICES:
        if not exists(device):
            raise make(
                "GINN-E-HOST-DEVICE", device=device, feature="a realm with nested containers"
            )


def _validate(env: OpEnv, request: CreateRequest, hooks: CreateHooks) -> _Plan:
    """Check the request and build the spec, with no side effect (NFR-SEC-005, AS-013).

    Raises GINN-E-USAGE for an unknown distribution, GINN-E-OPTION-INVALID for a bad setting and
    GINN-E-PKG-NAME-INVALID for a bad package name. A host home or user name that the passwd entry
    cannot carry is refused with GINN-E-REALM-USER (T4.9b). A services-on create with `--with`
    packages or `--everything` is refused with GINN-E-NOT-SUPPORTED-YET (T7.4), since their install
    needs a running container. `--everything` on a distribution without an everything set is
    GINN-E-OPTION-INVALID (FR-014 AC-3).
    """
    distro = DistroId(request.distro)
    definition = load_distro(distro.value)
    settings = RealmSettings(
        name=request.name,
        distro=distro,
        tier=request.tier,
        services=request.services,
        autostart=False,
        everything=request.everything,
    )
    if USER_NAME.fullmatch(env.user) is None:
        detail = f"host user {env.user!r} is not a realm user name"
        raise make("GINN-E-REALM-USER", realm=request.name.value, detail=detail)
    try:
        # The same check RealmSpec makes, here so that it refuses before any side effect.
        realm_passwd_entry(env.user, request.uid, request.gid, request.host_home, definition.shell)
    except ValueError as exc:
        raise make("GINN-E-REALM-USER", realm=request.name.value, detail=str(exc)) from exc
    packages = tuple(PackageName(raw, distro).value for raw in request.with_packages)
    if request.services and packages:
        # The packages install through exec in a running container, and a services-on realm starts
        # only through its runit entry (ADR-003), which arrives in T7.4.
        raise make(
            "GINN-E-NOT-SUPPORTED-YET",
            realm=request.name.value,
            feature="--with packages on a services-on realm",
            milestone="T7.4",
        )
    everything = definition.everything if request.everything else ()
    if request.everything and not everything:
        raise make("GINN-E-OPTION-INVALID", option="--everything", distro=distro.value)
    if request.services and everything:
        # The everything set installs the same way as --with, so it waits for the same task.
        raise make(
            "GINN-E-NOT-SUPPORTED-YET",
            realm=request.name.value,
            feature="--everything on a services-on realm",
            milestone="T7.4",
        )
    owned = env.paths.all_owned_roots()
    permissions = initial_permissions(request.tier)
    toggles = effective_toggles(request.tier, permissions.toggles)
    kvm_present = hooks.exists(KVM_DEVICE)
    # The profile is read here and written by the body, so a refused source writes nothing.
    nested = Toggle.NESTED in toggles
    if nested:
        _refuse_missing_devices(hooks.exists)
    gpu = nvidia_plan(env.paths, hooks.security)
    gpu_cdi = gpu.status is Status.READY and Toggle.GPU in toggles
    if gpu_cdi:
        grant = CdiGrant(env.paths.cdi, NVIDIA_DEVICE)
        base = TierExtras() if hooks.extras is None else hooks.extras
        extras_for_spec: TierExtras | None = replace(base, cdi=grant)
    else:
        extras_for_spec = hooks.extras
    profile = seccomp.plan(env.paths.seccomp, hooks.seccomp_source) if nested else None
    spec = realm_spec(
        request.name,
        distro,
        uid=request.uid,
        gid=request.gid,
        user=env.user,
        login_shell=definition.shell,
        host_home=request.host_home,
        realm_home=env.paths.homes / request.name.value,
        owned_paths=owned,
        env=request.host_env,
        manifest=hooks.manifest,
        exists=hooks.exists,
        tier=request.tier,
        permissions=permissions,
        kvm_present=kvm_present,
        seccomp_profile=None if profile is None else profile.path,
        extras=extras_for_spec,
    )
    # realm_spec takes the tier. Replaying only the services flag sets it on the spec, and RealmSpec
    # checks the result.
    spec = replace(spec, services=request.services)
    if request.services:
        # The services-on fields come after `services`, which RealmSpec checks (ADR-003, ADR-007).
        spec = replace(
            spec,
            conmon_pidfile=str(conmon_pidfile(env.paths, request.name)),
            services_mitigations=SERVICES_MITIGATIONS,
            journald_dropin=(str(journald_dropin(env.paths)) if SERVICES_MITIGATIONS else None),
        )
    manifest = trust.load() if hooks.manifest is None else hooks.manifest
    session = session_env(
        request.host_env,
        home=request.host_home,
        uid=request.uid,
        owned_paths=owned,
        toggles=toggles,
    )
    return _Plan(
        settings=settings,
        definition=definition,
        spec=spec,
        entry=manifest.entry(distro.value),
        packages=packages,
        everything=everything,
        session=session,
        owned=owned,
        user=env.user,
        permissions=permissions,
        created=option_keys(toggles, kvm_present=kvm_present),
        seccomp=profile,
        kvm_present=kvm_present,
        gpu=gpu,
        toggles=toggles,
        gpu_cdi=gpu_cdi,
    )
