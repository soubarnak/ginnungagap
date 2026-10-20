# SPDX-License-Identifier: Apache-2.0
"""Host gate for the AUR transient build (T5.8, M5), on real rootless Podman and the real AUR.

Run with `GINN_INTEGRATION=1 pytest tests/integration/test_aur_real.py -v -s` on a Void host with
network access. The harness (launcher, storage base, `run_ginn`, `realm`) is the one of
test_export_real.py: the product CLI as a subprocess on the conftest storage base.

The package. `GINN_AUR_PACKAGE` names the AUR package to build. The default, `yay-bin`, is a
prebuilt binary package with no AUR dependency and no `validpgpkeys` source, which is what the
product can build today (an AUR-only dependency is refused, and the build keyring is empty, see the
T5.8 note in docs/roadmap.md). This file cannot check that the package still exists in the AUR; a
build that fails for that reason is a failure of the item, so set the variable to another package.
`GINN_AUR_FAILING_PACKAGE`, when set, is an AUR package whose build is known to fail. It adds a
second FR-043 AC-2 case. The always-run AC-2 case uses a name that cannot exist, and it proves the
restore only for a clone failure, not for a failure inside makepkg.

Items:
- FR-043 AC-1: the package set after is the set before plus the package and its runtime closure. The
  test reads this as "every package that is new, apart from the AUR package, is required by
  something" (`pacman -Qi` Required By), so no build-only package (git, base-devel members, make
  dependencies) remains, and it checks the base-devel group explicitly.
- FR-043 AC-2: a failing build restores the package set.
- The Install Manifest records the package as `aur` / `ginn` (DR-007).

Not covered here: a package with a PGP-signed source (open decision of T5.8), and the toolchain
residue after a refused removal (GINN-W-BUILD-RESIDUAL), which needs a build that pacman refuses to
clean up.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import pytest

from ginn.domain import RealmName
from ginn.domain.packages import InstallMethod, InstallSource
from tests.integration import test_export_real as harness
from tests.integration.test_export_real import (
    CREATE_TIMEOUT,
    PREFIX,
    Ran,
    World,
    in_realm,
    realm,
    run_ginn,
)

world = harness.world
"""The storage base and launcher fixture of the export gate (a fixture, reused by name)."""

AUR_REALM = f"{PREFIX}aur"
AUR_PACKAGE = os.environ.get("GINN_AUR_PACKAGE", "yay-bin")
FAILING_PACKAGE = os.environ.get("GINN_AUR_FAILING_PACKAGE", "")
NO_SUCH_PACKAGE = "ginn-gate-no-such-aur-package-zz"
_BLOCK = re.compile(r"^Name\s*:\s*(\S+)$", re.MULTILINE)


def explicit_names(world: World, name: str) -> set[str]:
    """Every package of the realm, as `pacman -Qq` lists it."""
    ran = in_realm(world, name, "pacman -Qq")
    assert ran.rc == 0, ran.err[-400:]
    return set(ran.out.split())


def required_by(world: World, name: str, packages: set[str]) -> dict[str, list[str]]:
    """For each package: the packages that require it (`pacman -Qi` Required By), [] for None."""
    if not packages:
        return {}
    ran = in_realm(world, name, "LC_ALL=C pacman -Qi " + " ".join(sorted(packages)))
    assert ran.rc == 0, ran.err[-400:]
    found: dict[str, list[str]] = {}
    for block in ran.out.split("\n\n"):
        if not (match := _BLOCK.search(block)):
            continue
        for line in block.splitlines():
            if line.startswith("Required By"):
                value = line.split(":", 1)[1].strip()
                found[match.group(1)] = [] if value == "None" else value.split()
    return found


def base_devel_members(world: World, name: str) -> set[str]:
    ran = in_realm(world, name, "pacman -Sgq base-devel")
    assert ran.rc == 0, ran.err[-400:]
    members = set(ran.out.split())
    assert members, "pacman lists no base-devel members: the sync databases are missing"
    return members


@pytest.fixture(scope="module")
def arch_realm(world: World) -> Iterator[str]:
    """One running Arch realm (create, start). The AUR items build into it."""
    with realm(world, "arch", AUR_REALM) as name:
        yield name


def assert_unchanged(world: World, name: str, before: set[str], ran: Ran) -> None:
    after = explicit_names(world, name)
    assert after == before, (
        f"{ran.error_text()}: package set changed, added {sorted(after - before)}, "
        f"removed {sorted(before - after)}"
    )


@pytest.mark.fr("FR-043")
def test_aur_build_leaves_package_and_runtime_closure_only(world: World, arch_realm: str) -> None:
    """FR-043 AC-1: S before; after, S plus the package plus its runtime closure, no build-only
    package; the Install Manifest records the package as an AUR install through ginn."""
    before = explicit_names(world, arch_realm)
    group = base_devel_members(world, arch_realm)
    ran = run_ginn(world, "install", arch_realm, "--aur", AUR_PACKAGE, timeout=CREATE_TIMEOUT)
    assert ran.rc == 0, ran.error_text()
    after = explicit_names(world, arch_realm)
    assert before <= after, f"packages vanished: {sorted(before - after)}"
    assert AUR_PACKAGE in after, f"{AUR_PACKAGE} is not installed"
    new = after - before - {AUR_PACKAGE}
    needed = required_by(world, arch_realm, new)
    leftovers = sorted(p for p in new if not needed.get(p))
    assert not leftovers, f"packages added and required by nothing (build residue): {leftovers}"
    stray = sorted((group - before) & after)
    assert not stray, f"base-devel members were added by the build and remain: {stray}"
    record = world.store().load(RealmName(arch_realm))
    entries = [e for e in record.manifest if e.name == AUR_PACKAGE]
    assert entries, f"{AUR_PACKAGE} is not in the Install Manifest"
    assert entries[0].source is InstallSource.AUR
    assert entries[0].method is InstallMethod.GINN


@pytest.mark.fr("FR-043")
def test_aur_clone_failure_restores_the_package_set(world: World, arch_realm: str) -> None:
    """FR-043 AC-2, clone failure: an AUR package that does not exist fails the install with a
    non-zero status, and the package set equals the set before."""
    before = explicit_names(world, arch_realm)
    ran = run_ginn(world, "install", arch_realm, "--aur", NO_SUCH_PACKAGE, timeout=CREATE_TIMEOUT)
    assert ran.rc != 0, "installing a package that is not in the AUR succeeded"
    assert ran.body.get("error", {}).get("code"), ran.out + ran.err
    assert_unchanged(world, arch_realm, before, ran)
    record = world.store().load(RealmName(arch_realm))
    assert all(e.name != NO_SUCH_PACKAGE for e in record.manifest)


@pytest.mark.fr("FR-043")
def test_aur_build_failure_restores_the_package_set(world: World, arch_realm: str) -> None:
    """FR-043 AC-2, build failure: `GINN_AUR_FAILING_PACKAGE` names a package whose build fails.
    The toolchain was installed for the build, and the set must still equal the set before."""
    if not FAILING_PACKAGE:
        pytest.skip("set GINN_AUR_FAILING_PACKAGE to an AUR package whose build is known to fail")
    before = explicit_names(world, arch_realm)
    ran = run_ginn(world, "install", arch_realm, "--aur", FAILING_PACKAGE, timeout=CREATE_TIMEOUT)
    assert ran.rc != 0, f"{FAILING_PACKAGE} was expected to fail its build and did not"
    assert_unchanged(world, arch_realm, before, ran)
