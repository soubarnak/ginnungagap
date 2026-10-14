# SPDX-License-Identifier: Apache-2.0
"""The `down` file that autostart toggles (FR-084, DR-012, AS-016).

services/autostart.py makes the `down` file of a services-on realm's runit service match the
autostart flag. The record write and the realm lock are tested with the operation in
tests/unit/test_ops_service.py. Here only the file and its refusals are checked.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ginn.domain import RealmName
from ginn.errors import GinnError
from ginn.services import autostart
from ginn.services.runit_user import DOWN, service_dir, write_service_dir
from ginn.store.paths import OwnedPaths

NAME = RealmName("lab")
PROGRAM = "/usr/bin/ginn"

pytestmark = [pytest.mark.fr("FR-084"), pytest.mark.fr("DR-012")]


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OwnedPaths:
    root = tmp_path
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    (root / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(root / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    write_service_dir(resolved, NAME, PROGRAM)
    return resolved


def test_a_new_services_realm_has_a_down_file_so_runit_does_not_start_it(paths: OwnedPaths) -> None:
    assert autostart.is_down(paths, NAME) is True


def test_autostart_on_removes_the_down_file(paths: OwnedPaths) -> None:
    autostart.apply(paths, NAME, True)
    assert (service_dir(paths, NAME) / DOWN).exists() is False
    assert autostart.is_down(paths, NAME) is False


def test_autostart_off_creates_the_down_file_atomically(paths: OwnedPaths) -> None:
    autostart.apply(paths, NAME, True)
    autostart.apply(paths, NAME, False)
    down = service_dir(paths, NAME) / DOWN
    assert down.is_file()
    assert down.read_bytes() == b""
    assert not any(p.name.endswith(".tmp") for p in service_dir(paths, NAME).iterdir())


@pytest.mark.parametrize("flag", [True, False])
def test_applying_the_same_flag_twice_is_idempotent(paths: OwnedPaths, flag: bool) -> None:
    autostart.apply(paths, NAME, flag)
    autostart.apply(paths, NAME, flag)
    assert autostart.is_down(paths, NAME) is (not flag)


def test_a_missing_service_directory_is_a_service_missing_error_and_writes_nothing(
    paths: OwnedPaths,
) -> None:
    shutil.rmtree(service_dir(paths, NAME))
    with pytest.raises(GinnError) as caught:
        autostart.apply(paths, NAME, True)
    assert caught.value.code == "GINN-E-SERVICE-MISSING"
    assert caught.value.details["realm"] == "lab"
    assert not service_dir(paths, NAME).exists()
