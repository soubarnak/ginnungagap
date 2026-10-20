# SPDX-License-Identifier: Apache-2.0
"""A failed install or remove reconciles the manifest per name, or says it could not (T8.3).

FR-040, FR-041, FR-122: the reconcile after a failed transaction keeps an existing entry's source
and method, and an unreadable explicit-package list is GINN-W-MANIFEST-UNVERIFIED with the
manifest unchanged.
"""

from __future__ import annotations

import pytest

from ginn.distros import Verb
from ginn.domain import InstallMethod, InstallSource, ManifestEntry, RealmName, RealmState
from ginn.errors import GinnError
from ginn.ops.packages import install_packages, remove_packages
from ginn.podman.models import ExecResult
from ginn.store.records import RealmStore
from tests.unit.realm_cli_support import World, make_realm, stored
from tests.unit.test_ops_packages import (
    ARCH,
    DEB,
    INSTALL_DEB,
    LIST_DEB,
    _argv,
    _env,
    _ginn,
    _hooks,
    _manifest,
    _session,
    _with_manifest,
)

UNVERIFIED = "GINN-W-MANIFEST-UNVERIFIED"


def _aur(name: str) -> ManifestEntry:
    return ManifestEntry(name=name, source=InstallSource.AUR, method=InstallMethod.GINN)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_a_failed_install_with_an_unreadable_list_warns_and_keeps_the_manifest(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[INSTALL_DEB] = ExecResult(100, "", "E: Unable to locate package gimp")
    world.fake.exec_results[LIST_DEB] = ExecResult(1, "", "E: could not read the database")
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName(DEB),
            ["gimp", "htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    notes = getattr(caught.value, "__notes__", [])
    (note,) = [n for n in notes if n.startswith(UNVERIFIED)]
    assert "after a failed install" in note
    assert "run the same install or remove again" in note
    assert _manifest(world, DEB) == []


@pytest.mark.fr("FR-041")
@pytest.mark.fr("FR-122")
def test_a_failed_remove_with_an_unreadable_list_warns_and_keeps_the_manifest(
    world: World,
) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    RealmStore(world.paths).save(_with_manifest(stored(world, ARCH), [_ginn("gimp")]))
    world.fake.exec_results[_argv("arch", Verb.REMOVE, "gimp")] = ExecResult(
        1, "", "error: target not found: x"
    )
    world.fake.exec_results[_argv("arch", Verb.LIST_EXPLICIT)] = ExecResult(
        1, "", "E: could not read the database"
    )
    with pytest.raises(GinnError) as caught:
        remove_packages(
            _env(world),
            world.fake,
            RealmName(ARCH),
            ["gimp"],
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    notes = getattr(caught.value, "__notes__", [])
    (note,) = [n for n in notes if n.startswith(UNVERIFIED)]
    assert "after a failed remove" in note
    assert _manifest(world, ARCH) == [_ginn("gimp")]


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_a_failed_install_keeps_the_source_of_an_existing_aur_entry(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    RealmStore(world.paths).save(_with_manifest(stored(world, DEB), [_aur("gimp")]))
    world.fake.exec_results[INSTALL_DEB] = ExecResult(100, "", "E: Unable to locate package x")
    world.fake.exec_sequences[LIST_DEB] = [ExecResult(0, "gimp\nhtop\n", "")]
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName(DEB),
            ["gimp", "htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert not [n for n in getattr(caught.value, "__notes__", []) if n.startswith(UNVERIFIED)]
    assert _manifest(world, DEB) == [_aur("gimp"), _ginn("htop")]
