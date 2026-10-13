# SPDX-License-Identifier: Apache-2.0
"""CLI launchers of `ginn export --bin` and `ginn unexport --bin` (FR-047, AS-008).

Each test runs against the pinned host of tests/unit/realm_cli_support.py (fixture `world`), with
the realm records made there. No podman runs a command: a launcher is only written and read.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from ginn.domain import RealmName, RealmState
from ginn.errors import GinnError
from ginn.export.bins import (
    bin_on_path,
    check_binary,
    export_bin,
    launcher_text,
    remove_realm_launchers,
    shell_quote,
    unexport_bin,
)
from ginn.ops._mutating import OpEnv
from tests.unit.realm_cli_support import World, make_realm

PROGRAM = "/usr/bin/ginn"


def _env(world: World) -> OpEnv:
    return OpEnv.for_paths(world.paths, "alice", root_dir=world.host)


def _code(err: pytest.ExceptionInfo[GinnError]) -> str:
    return err.value.code


@pytest.mark.fr("FR-047")
def test_the_launcher_runs_the_binary_through_ginn_exec() -> None:
    text = launcher_text(PROGRAM, "debian", "rg")
    assert text.startswith("#!/bin/sh\n# ginn-launcher: realm=debian binary=rg\n")
    assert text.endswith(f"exec '{PROGRAM}' exec 'debian' -- 'rg' \"$@\"\n")


@pytest.mark.fr("FR-047")
def test_a_launcher_is_a_posix_script_that_runs_under_sh(tmp_path: Path) -> None:
    """A fake `ginn` records its argv, so the launcher's arguments can be seen without podman."""
    recorder = tmp_path / "ginn"
    recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    recorder.chmod(0o755)
    text = launcher_text(str(recorder), "debian", "rg")
    script = tmp_path / "rg"
    script.write_text(text)
    sh = shutil.which("sh") or "/bin/sh"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sh, str(script), "--version", "two words", "$HOME"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert result.stdout.splitlines() == [
        "exec",
        "debian",
        "--",
        "rg",
        "--version",
        "two words",
        "$HOME",
    ]


@pytest.mark.fr("FR-047")
def test_a_quote_in_the_program_path_stays_one_word() -> None:
    assert shell_quote("/opt/it's/ginn") == "'/opt/it'\\''s/ginn'"
    assert "exec '/opt/it'\\''s/ginn' exec" in launcher_text("/opt/it's/ginn", "debian", "rg")


@pytest.mark.fr("FR-047")
@pytest.mark.parametrize("name", ["rg", "g++", "python3.14", "a_b-c.d", "Tool"])
def test_plain_binary_names_are_accepted(name: str) -> None:
    assert check_binary(name) == name


@pytest.mark.fr("FR-047")
@pytest.mark.parametrize("name", ["", "../rg", "bin/rg", "/bin/sh", "-rf", ".hidden", "a b", "x;y"])
def test_binary_names_with_slashes_or_metacharacters_are_refused(name: str) -> None:
    with pytest.raises(GinnError) as err:
        check_binary(name)
    assert _code(err) == "GINN-E-BIN-NAME-INVALID"


@pytest.mark.fr("FR-047")
@pytest.mark.parametrize("name", ["ginn", "ginn-rebind", "ginn-", "podman"])
def test_the_names_ginn_and_ginn_dash_are_reserved(name: str) -> None:
    with pytest.raises(GinnError) as err:
        check_binary(name)
    assert _code(err) == "GINN-E-BIN-NAME-INVALID"


@pytest.mark.fr("FR-047")
@pytest.mark.parametrize("name", ["ginnx", "podman-ps", "podmanx"])
def test_a_name_that_only_starts_with_a_reserved_word_is_not_reserved(name: str) -> None:
    assert check_binary(name) == name


@pytest.mark.fr("FR-047")
def test_unexport_removes_only_the_launchers_of_the_named_realm(world: World) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    export_bin(_env(world), RealmName("debian"), "rg", program=PROGRAM)
    export_bin(_env(world), RealmName("arch"), "fd", program=PROGRAM)
    (world.paths.bin / "mine").write_text("#!/bin/sh\n", encoding="utf-8")
    removed = remove_realm_launchers(world.paths, RealmName("debian"))
    assert removed == ["rg"]
    assert (world.paths.bin / "fd").exists()
    assert (world.paths.bin / "mine").exists()


@pytest.mark.fr("FR-047")
def test_export_writes_an_executable_launcher_in_the_bin_directory(world: World) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    result = export_bin(_env(world), RealmName("debian"), "rg", program=PROGRAM)
    assert result.changed is True
    assert result.path == world.paths.bin / "rg"
    assert result.path.read_text(encoding="utf-8") == launcher_text(PROGRAM, "debian", "rg")
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o755
    assert world.fake.ops() == []  # nothing ran in the realm: no start, no exec


@pytest.mark.fr("FR-047")
def test_export_twice_changes_nothing_the_second_time(world: World) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    export_bin(_env(world), RealmName("debian"), "rg", program=PROGRAM)
    again = export_bin(_env(world), RealmName("debian"), "rg", program=PROGRAM)
    assert again.changed is False


@pytest.mark.fr("FR-047")
def test_export_of_an_unknown_realm_is_refused_before_any_write(world: World) -> None:
    with pytest.raises(GinnError) as err:
        export_bin(_env(world), RealmName("nosuch"), "rg", program=PROGRAM)
    assert _code(err) == "GINN-E-NO-SUCH-REALM"
    assert not (world.paths.bin / "rg").exists()


@pytest.mark.fr("FR-047")
def test_export_never_overwrites_a_file_ginn_did_not_write(world: World) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    world.paths.bin.mkdir(parents=True, exist_ok=True)
    mine = world.paths.bin / "rg"
    mine.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    with pytest.raises(GinnError) as err:
        export_bin(_env(world), RealmName("debian"), "rg", program=PROGRAM)
    assert _code(err) == "GINN-E-BIN-EXISTS"
    assert mine.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


@pytest.mark.fr("FR-047")
def test_export_refuses_a_launcher_that_belongs_to_another_realm(world: World) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    export_bin(_env(world), RealmName("arch"), "rg", program=PROGRAM)
    with pytest.raises(GinnError) as err:
        export_bin(_env(world), RealmName("debian"), "rg", program=PROGRAM)
    assert _code(err) == "GINN-E-BIN-EXISTS"
    assert "realm=arch" in (world.paths.bin / "rg").read_text(encoding="utf-8")


@pytest.mark.fr("FR-047")
def test_a_symbolic_link_at_the_launcher_path_is_not_followed(world: World) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    world.paths.bin.mkdir(parents=True, exist_ok=True)
    target = world.tmp / "elsewhere"
    target.write_text("keep\n", encoding="utf-8")
    (world.paths.bin / "rg").symlink_to(target)
    with pytest.raises(GinnError) as err:
        export_bin(_env(world), RealmName("debian"), "rg", program=PROGRAM)
    assert _code(err) == "GINN-E-BIN-EXISTS"
    assert target.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.fr("FR-047")
def test_unexport_removes_the_launcher_of_that_realm(world: World) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    export_bin(_env(world), RealmName("debian"), "rg", program=PROGRAM)
    result = unexport_bin(_env(world), RealmName("debian"), "rg")
    assert result.changed is True
    assert not (world.paths.bin / "rg").exists()


@pytest.mark.fr("FR-047")
def test_unexport_of_a_missing_launcher_is_refused(world: World) -> None:
    with pytest.raises(GinnError) as err:
        unexport_bin(_env(world), RealmName("debian"), "rg")
    assert _code(err) == "GINN-E-BIN-NOT-EXPORTED"


@pytest.mark.fr("FR-047")
def test_unexport_never_removes_a_file_ginn_did_not_write(world: World) -> None:
    world.paths.bin.mkdir(parents=True, exist_ok=True)
    mine = world.paths.bin / "rg"
    mine.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    with pytest.raises(GinnError) as err:
        unexport_bin(_env(world), RealmName("debian"), "rg")
    assert _code(err) == "GINN-E-BIN-NOT-EXPORTED"
    assert mine.exists()


@pytest.mark.fr("FR-047")
def test_unexport_works_after_the_realm_record_is_gone(world: World) -> None:
    make_realm(world, "debian", RealmState.STOPPED, running=False)
    export_bin(_env(world), RealmName("debian"), "rg", program=PROGRAM)
    (world.paths.realms / "debian.json").unlink(missing_ok=True)
    unexport_bin(_env(world), RealmName("debian"), "rg")
    assert not (world.paths.bin / "rg").exists()


@pytest.mark.fr("FR-047")
def test_a_bin_directory_on_path_is_recognized_whatever_its_spelling(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    assert bin_on_path(bin_dir, f"/usr/bin:{bin_dir}/:/bin")
    assert bin_on_path(bin_dir, f"{tmp_path}/bin")
    assert not bin_on_path(bin_dir, "/usr/bin:/bin")
    assert not bin_on_path(bin_dir, "")
