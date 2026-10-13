# SPDX-License-Identifier: Apache-2.0
"""`ginn exec --root` and the Install Manifest, through the CLI (T5.2, FR-042 AC-1, NFR-004).

A root command that installs a package (`pacman -S htop`, as root in the realm) records it in the
manifest as native. A command without `--root` runs no package-manager read at all. A failed
record is a warning on stderr, and the command's own status is still the exit status.
"""

from __future__ import annotations

import pytest

from ginn.__main__ import main
from ginn.distros import Verb, load
from ginn.domain import RealmState
from ginn.podman.models import ExecResult
from tests.unit.realm_cli_support import World, make_realm, stored

LIST = tuple(load("arch").argv(Verb.LIST_EXPLICIT))
PACMAN_INSTALL = ("pacman", "--sync", "--noconfirm", "htop")


@pytest.mark.fr("FR-042")
@pytest.mark.fr("DR-007")
def test_a_root_command_that_installs_a_package_records_it_as_native(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    world.fake.exec_sequences[LIST] = [
        ExecResult(0, "vim\n", ""),
        ExecResult(0, "htop\nvim\n", ""),
    ]
    assert main(["exec", "--root", "arch", "--", *PACMAN_INSTALL]) == 0
    entries = [(entry.name, entry.method.value) for entry in stored(world, "arch").manifest]
    assert entries == [("htop", "native")]
    assert capsys.readouterr().err == ""


@pytest.mark.fr("NFR-004")
@pytest.mark.fr("FR-049")
def test_a_command_without_root_runs_no_package_read(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["exec", "arch", "--", "true"]) == 0
    assert [call for call in world.fake.exec_calls if call.argv == LIST] == []
    assert stored(world, "arch").manifest == []


@pytest.mark.fr("FR-042")
@pytest.mark.fr("IR-011")
def test_a_failed_read_is_a_warning_and_the_command_status_is_kept(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    world.fake.exec_sequences[LIST] = [ExecResult(1, "", "error: database is locked")]
    world.fake.exec_results[PACMAN_INSTALL] = ExecResult(7, "", "")
    assert main(["exec", "--root", "arch", "--", *PACMAN_INSTALL]) == 7
    err = capsys.readouterr().err
    assert "GINN-W-MANIFEST-UNRECORDED" in err
    assert "fix: run 'ginn install arch PACKAGE'" in err
    assert stored(world, "arch").manifest == []
