# SPDX-License-Identifier: Apache-2.0
"""`ginn delete` removes the realm's exported entries (FR-019 AC-1, DR-008). Fakes only."""

from __future__ import annotations

import pytest

from ginn.domain import RealmName, RealmState
from ginn.export.bins import export_bin
from ginn.export.icons import ICON_LIST
from ginn.export.sync import DESKTOP_LIST, SyncHooks, sync_realm
from ginn.ops._mutating import OpEnv
from ginn.ops.delete import DeleteHooks, delete_realm
from ginn.podman.models import ExecResult
from tests.unit.realm_cli_support import World, make_realm

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
ENTRY = b"[Desktop Entry]\nType=Application\nName=Firefox\nExec=firefox %u\nIcon=firefox\n"


def _env(world: World) -> OpEnv:
    return OpEnv.for_paths(world.paths, "alice", root_dir=world.host)


@pytest.mark.fr("FR-019")
def test_delete_removes_menu_entries_icon_copies_and_launchers_of_the_realm(world: World) -> None:
    make_realm(world, "fedora", RealmState.RUNNING, running=True)
    path = "/usr/share/applications/firefox.desktop"
    icon = "/usr/share/icons/hicolor/48x48/apps/firefox.png"
    world.fake.files[path] = ENTRY
    world.fake.files[icon] = PNG
    world.fake.exec_results[DESKTOP_LIST] = ExecResult(0, f"{len(ENTRY)} 1 {path}\n", "")
    world.fake.exec_results[ICON_LIST] = ExecResult(0, f"f {len(PNG)} {icon} \n", "")
    sync_realm(_env(world), world.fake, RealmName("fedora"), hooks=SyncHooks("/usr/bin/ginn"))
    export_bin(_env(world), RealmName("fedora"), "rg", program="/usr/bin/ginn")
    entry = world.paths.applications_dir / "ginn-fedora-firefox.desktop"
    assert entry.exists()
    assert (world.paths.icons / "fedora" / "firefox.png").exists()
    assert (world.paths.bin / "rg").exists()

    delete_realm(_env(world), world.fake, RealmName("fedora"), hooks=DeleteHooks())

    assert not entry.exists()
    assert not (world.paths.icons / "fedora").exists()
    assert not (world.paths.bin / "rg").exists()
    assert not (world.paths.state / "exports" / "fedora.json").exists()


@pytest.mark.fr("FR-019")
def test_delete_keeps_the_exports_of_other_realms(world: World) -> None:
    make_realm(world, "fedora", RealmState.RUNNING, running=True)
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    export_bin(_env(world), RealmName("arch"), "gcc", program="/usr/bin/ginn")
    delete_realm(_env(world), world.fake, RealmName("fedora"), hooks=DeleteHooks())
    assert (world.paths.bin / "gcc").exists()


@pytest.mark.fr("FR-019")
def test_the_exports_step_runs_before_the_record_is_removed(world: World) -> None:
    make_realm(world, "fedora", RealmState.RUNNING, running=True)
    export_bin(_env(world), RealmName("fedora"), "rg", program="/usr/bin/ginn")
    seen: list[tuple[str, bool]] = []

    def on_step(name: str) -> None:
        seen.append((name, (world.paths.bin / "rg").exists()))

    delete_realm(_env(world), world.fake, RealmName("fedora"), hooks=DeleteHooks(on_step=on_step))
    assert [name for name, _ in seen].index("exports-removed") < [name for name, _ in seen].index(
        "record-removed"
    )
    assert dict(seen)["exports-removed"] is False
