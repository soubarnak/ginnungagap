# SPDX-License-Identifier: Apache-2.0
"""What a create is asked for, and what it will build, checked before any side effect (AS-013).

`CreateRequest` is the user's request and `CreateHooks` the seams for the host and the clock.
`_validate` turns the request into a `_Plan`: the settings, the distribution definition, the
RealmSpec (Basic, services off, pinned by digest), the trust entry, the validated `--with` names
and the host session. It runs no podman and writes nothing (NFR-SEC-005). ops/create.py runs the
plan. FR-121's space estimate is here too, so ops/disk.py (T8.2) can replace it in one place.
"""

import os
import re
import shutil
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
from ginn.errors import make
from ginn.ops._mutating import OpEnv
from ginn.runtime.attach import MOUNTINFO
from ginn.runtime.env import SessionEnv, session_env
from ginn.runtime.mounts import Exists
from ginn.runtime.spec import RealmSpec, realm_passwd_entry, realm_spec
from ginn.trust import manifest as trust
from ginn.trust.manifest import TrustEntry, TrustManifest

DISK_MARGIN_PERCENT: Final = 110
"""The FR-121 factor: free space must cover the estimate times 110 percent."""
USER_NAME: Final = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
"""The `<user>` field of the realm's passwd entry (T4.9b, ADR-006 amendment). The entry is one
field, so there is no `:`, `$`, newline or leading `-`. The name is a lowercase login name: a
letter or `_` first, then up to 31 letters, digits, `_` or `-`."""


def _free_bytes(path: Path) -> int:
    """Free bytes on the filesystem that holds `path`. A missing path is probed at its parent."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def estimate_bytes(entry: TrustEntry) -> int:
    """The base image's size estimate: compressed download plus installed size (FR-121).

    It covers the official image only. The Integration Package Set and `--with` closures are not
    counted (see the module docstring). ops/disk.py (T8.2) replaces this estimate.
    """
    return entry.compressed_size_bytes + entry.estimated_installed_bytes


def required_bytes(entry: TrustEntry) -> int:
    """The free space FR-121 requires: the estimate times 1.10, in whole bytes."""
    return estimate_bytes(entry) * DISK_MARGIN_PERCENT // 100


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

    `disk_free` reports free bytes for a path (FR-121). `sleep` waits between retries (FR-120).
    `exists`, `mountinfo` and `helper` are the probes and paths that runtime/ reads. `manifest`
    replaces the packaged Trust Manifest. `on_step` runs after each journal step, so a caller can
    show progress (AS-018), or a test can inject a failure there.

    `integrate` False skips the refresh, the Integration Package Set and its steps, and records the
    `integrated` step with nothing run. Only the host gate sets it (T4.9, Gate 3: no package
    install and no refresh in a test realm). The CLI never sets it, so every real create refreshes.
    """

    disk_free: Callable[[Path], int] = _free_bytes
    sleep: Callable[[float], None] = time.sleep
    exists: Exists = os.path.exists
    mountinfo: Path = MOUNTINFO
    helper: str | None = None
    manifest: TrustManifest | None = None
    on_step: Callable[[str], None] | None = None
    integrate: bool = True


@dataclass(frozen=True)
class _Plan:
    """Everything the body needs, all checked before the operation begins."""

    settings: RealmSettings
    definition: DistroDef
    spec: RealmSpec
    entry: TrustEntry
    packages: tuple[str, ...]
    session: SessionEnv
    owned: list[Path]
    user: str


def _validate(env: OpEnv, request: CreateRequest, hooks: CreateHooks) -> _Plan:
    """Check the request and build the spec, with no side effect (NFR-SEC-005, AS-013).

    Raises GINN-E-USAGE for an unknown distribution, GINN-E-OPTION-INVALID for a bad setting and
    GINN-E-PKG-NAME-INVALID for a bad package name. A host home or user name that the passwd entry
    cannot carry is refused with GINN-E-REALM-USER (T4.9b). A tier, services mode or distribution
    that the code does not support yet (T6.2, M7, T10.1) is refused by RealmSpec, as
    NotImplementedError.
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
    owned = env.paths.all_owned_roots()
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
    )
    # realm_spec always builds a Basic, services-off spec. Replaying the settings through
    # replace() re-runs RealmSpec's own refusals for Custom, Development and services on.
    spec = replace(spec, tier=request.tier, services=request.services)
    manifest = trust.load() if hooks.manifest is None else hooks.manifest
    session = session_env(
        request.host_env, home=request.host_home, uid=request.uid, owned_paths=owned
    )
    return _Plan(
        settings=settings,
        definition=definition,
        spec=spec,
        entry=manifest.entry(distro.value),
        packages=packages,
        session=session,
        owned=owned,
        user=env.user,
    )
