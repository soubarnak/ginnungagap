# SPDX-License-Identifier: Apache-2.0
"""Shared setup of the realm CLI tests (T4.11): a pinned host, a fake podman and the seams.

The tests call `ginn.__main__.main`, which builds the production objects through
`ginn.cli._context`. This module replaces only the host seams of that module: the owned paths
come from a pinned environment under tmp_path, the adapter is LifecyclePodman (FakePodman with the
helper answered from tmp_path), the host tree and the path probe are under tmp_path, and the host
user has an empty session. No test reaches the real host, podman or the user's storage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ginn.cli import _context
from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.ops.lifecycle import HostUser
from ginn.podman.adapter import CreateSpec
from ginn.runtime import attach
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from tests.unit.test_ops_lifecycle import LifecyclePodman

DIGEST = "registry.example/debian@sha256:" + "ab" * 32
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
SCOPE = ("etc", "var/db/xbps", "var/service", "usr/local", "opt")
BASE_MOUNTINFO = (
    "36 35 98:0 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
    "37 36 98:1 / /opt rw - ext4 /dev/root rw\n"
)


@dataclass
class World:
    """The pinned host of one CLI test. Every path is under `tmp`."""

    tmp: Path
    paths: OwnedPaths
    fake: LifecyclePodman
    home: Path
    host: Path


def build_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """Pin the environment, the seams and the adapter. See the module docstring.

    `integrate` is False, so a create runs no package step (Gate 3, CreateHooks.integrate).
    """
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    for name, value in {
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }.items():
        monkeypatch.setenv(name, value)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(runtime))
    session = tmp_path / "session"
    session.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(session))
    host = tmp_path / "host"
    for scope in SCOPE:
        (host / scope).mkdir(parents=True)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(BASE_MOUNTINFO, encoding="utf-8")
    paths = OwnedPaths.from_env(uid=os.getuid())
    paths.ensure()
    fake = LifecyclePodman(root=str(tmp_path))
    seams = _context.Seams(
        root_dir=host,
        integrate=False,
        exists=lambda p: p.startswith(str(tmp_path)) and os.path.exists(p),
        mountinfo=mountinfo,
    )
    real_identity = attach.read_identity

    def identity_under_tmp(path: str, *, mountinfo: Path = attach.MOUNTINFO) -> Any:
        return real_identity(path, mountinfo=mountinfo) if path.startswith(str(tmp_path)) else None

    monkeypatch.setattr(attach, "read_identity", identity_under_tmp)
    monkeypatch.setattr(_context, "seams", lambda: seams)
    monkeypatch.setattr(_context, "podman", lambda _paths, _output: fake)
    monkeypatch.setattr(
        _context,
        "host_user",
        lambda: HostUser(uid=os.getuid(), host_home=home, host_env={}),
    )
    return World(tmp=tmp_path, paths=paths, fake=fake, home=home, host=host)


def record_of(
    name: str,
    state: RealmState,
    container_id: str | None,
    *,
    tier: Tier = Tier.BASIC,
    services: bool = False,
    distro: str = "debian",
) -> RealmRecord:
    """A stored-form record of a realm, as ops/ writes it."""
    settings = RealmSettings(
        name=RealmName(name),
        distro=DistroId(distro),
        tier=tier,
        services=services,
        autostart=False,
        everything=False,
    )
    return RealmRecord(
        settings=settings,
        state=state,
        previous_state=None,
        image_ref=DIGEST,
        image_digest=DIGEST.partition("@")[2],
        container_id=container_id,
        generation=1,
        created_at=T0,
        updated_at=T0,
        restart_required=False,
        share_skipped=[],
    )


def make_realm(
    world: World,
    name: str,
    state: RealmState,
    *,
    running: bool,
    distro: str = "debian",
) -> RealmRecord:
    """A stored realm with a fake container. `running` starts the container in the fake."""
    labels = {LABEL_REALM: name, LABEL_DISTRO: distro, LABEL_GENERATION: "1"}
    spec = CreateSpec(
        name=f"ginn-{name}-g1", image_digest_ref=DIGEST, network="host", labels=labels
    )
    cid = world.fake.create(spec)
    if running:
        world.fake.start(cid)
    (world.paths.homes / name).mkdir(parents=True, exist_ok=True)
    record = record_of(name, state, cid, distro=distro)
    RealmStore(world.paths).save(record)
    world.fake.calls.clear()
    return record


def stored(world: World, name: str) -> RealmRecord:
    return RealmStore(world.paths).load(RealmName(name))
