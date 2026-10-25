# SPDX-License-Identifier: Apache-2.0
"""`ginn enter` and `ginn exec` through the CLI entry point (FR-042, FR-048, FR-049, IR-011).

The exit status of the inner process is the exit status of the command. A ginn-side failure
exits 125 and keeps its GINN-E code on stderr (runtime/enter.py, errors.py EXEC_GINN_FAILURE).
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.cli import _enter as enter_cli
from ginn.domain import RealmState
from ginn.errors import make, warn
from ginn.podman.models import ExecResult
from tests.unit.realm_cli_support import World, make_realm, stored
from tests.unit.test_ops_recovery import _dead, _write_journal

RUN_ARGV = ("sh", "-c", "exit 7")


def _execs(world: World) -> list[object]:
    return [call for call in world.fake.exec_calls if call.op == "exec_interactive"]


@pytest.mark.fr("FR-049")
@pytest.mark.fr("IR-011")
def test_exec_returns_the_inner_status_and_passes_the_command(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    world.fake.exec_results[RUN_ARGV] = ExecResult(7, "", "")
    assert main(["exec", "arch", "--", "sh", "-c", "exit 7"]) == 7
    (call,) = _execs(world)
    assert call.argv == RUN_ARGV
    assert call.user == f"{os.getuid()}:{os.getgid()}"
    assert call.workdir == str(world.home)
    assert call.env["HOME"] == str(world.home)
    assert call.interactive is True
    assert call.tty is False
    assert capsys.readouterr().err == ""


@pytest.mark.fr("FR-049")
def test_exec_status_zero_is_zero(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["exec", "arch", "--", "true"]) == 0


@pytest.mark.fr("FR-049")
def test_exec_keeps_words_that_look_like_options_after_the_separator(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["exec", "arch", "--", "-c", "x"]) == 0
    assert _execs(world)[-1].argv == ("-c", "x")


@pytest.mark.fr("FR-042")
def test_exec_root_runs_as_container_root_with_the_root_home(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["exec", "--root", "arch", "--", "pacman", "-Q"]) == 0
    call = _execs(world)[-1]
    assert call.user == "0:0"
    assert call.workdir == "/root"
    assert call.env["HOME"] == "/root"


@pytest.mark.fr("IR-011")
def test_exec_without_a_command_is_a_usage_error(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["exec", "arch", "--"]) == 2
    assert capsys.readouterr().err.startswith("GINN-E-USAGE: ")
    assert _execs(world) == []


@pytest.mark.fr("FR-048")
def test_enter_without_a_terminal_runs_the_login_shell_with_no_tty(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["enter", "arch"]) == 0
    (call,) = _execs(world)
    assert call.argv[-1] == "-l"
    assert call.tty is False
    assert call.interactive is True
    assert call.user == f"{os.getuid()}:{os.getgid()}"


@pytest.mark.fr("FR-042")
@pytest.mark.fr("FR-048")
def test_enter_root_gives_the_root_user_and_home(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["enter", "--root", "arch"]) == 0
    call = _execs(world)[-1]
    assert call.user == "0:0"
    assert call.env["HOME"] == "/root"


@pytest.mark.fr("FR-017")
def test_enter_starts_a_stopped_realm_first(world: World) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    assert main(["enter", "arch"]) == 0
    assert stored(world, "arch").state is RealmState.RUNNING
    assert _execs(world)


@pytest.mark.fr("IR-011")
def test_the_inner_status_125_is_passed_through_unchanged(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    world.fake.exec_results[("sh", "-c", "exit 125")] = ExecResult(125, "", "")
    assert main(["exec", "arch", "--", "sh", "-c", "exit 125"]) == 125
    assert capsys.readouterr().err == ""


@pytest.mark.fr("IR-011")
def test_a_ginn_side_failure_exits_125_and_names_its_code(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.CREATING, running=False, distro="arch")
    assert main(["exec", "arch", "--", "true"]) == 125
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-REALM-STATE: ")
    assert "  fix: " in err


@pytest.mark.fr("IR-011")
def test_a_ginn_side_failure_in_json_mode_reports_125_as_the_exit_code(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.CREATING, running=False, distro="arch")
    assert main(["--json", "exec", "arch", "--", "true"]) == 125
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err.splitlines()[-1])["error"]
    assert error["code"] == "GINN-E-REALM-STATE"
    assert error["exit_code"] == 125


@pytest.mark.fr("FR-025")
@pytest.mark.fr("IR-011")
def test_an_unknown_realm_for_exec_is_a_ginn_side_failure(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["exec", "nosuch", "--", "true"]) == 125
    assert capsys.readouterr().err.startswith("GINN-E-NO-SUCH-REALM: ")


@pytest.mark.fr("FR-122")
def test_a_recovery_failure_stops_exec_before_the_command_runs(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(env: object, podman: object, **kwargs: object) -> list[object]:
        raise make("GINN-E-INTERNAL")

    monkeypatch.setattr(_context, "recover", broken)
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["exec", "arch", "--", "true"]) == 125
    assert _execs(world) == []
    assert capsys.readouterr().err.startswith("GINN-E-INTERNAL: ")


@pytest.mark.fr("FR-122")
@pytest.mark.fr("IR-011")
def test_json_exec_writes_a_recovery_warning_to_stderr_before_the_command(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exec's stdout is the command's, so a JSON warning goes to stderr, the first line."""
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    make_realm(world, "debian", RealmState.CREATING, running=False)
    _write_journal(world.paths, _dead(op="create", realm="debian", step="pulled"))
    assert main(["--json", "exec", "arch", "--", "true"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    first = json.loads(captured.err.splitlines()[0])
    assert first["warning"]["code"] == "GINN-W-RECOVERED"
    assert len(_execs(world)) == 1


@pytest.mark.fr("IR-011")
@pytest.mark.parametrize(
    ("argv", "root", "words"),
    [
        (["exec", "arch", "ls", "--root"], False, ("ls", "--root")),
        (["exec", "arch", "cmd", "--help"], False, ("cmd", "--help")),
        (["exec", "--root", "arch", "--", "id"], True, ("id",)),
        (["exec", "arch", "--", "id"], False, ("id",)),
    ],
)
def test_every_word_after_the_realm_belongs_to_the_command(
    world: World,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    root: bool,
    words: tuple[str, ...],
) -> None:
    """ginn options come before REALM; a word after it is the command's, even `--root`/`--help`."""
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(argv) == 0
    call = _execs(world)[-1]
    assert call.argv == words
    assert call.user == ("0:0" if root else f"{os.getuid()}:{os.getgid()}")
    assert capsys.readouterr().out == ""  # no help text, no ginn output


@pytest.mark.fr("IR-011")
def test_a_realm_name_after_the_command_words_is_not_a_second_realm(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["exec", "arch", "ls", "other"]) == 0
    assert _execs(world)[-1].argv == ("ls", "other")


@pytest.mark.fr("FR-006")
@pytest.mark.fr("IR-011")
def test_a_re_attach_warning_is_written_before_the_command_runs(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    order: list[str] = []

    def enter_that_warns(env: Any, podman: Any, name: Any, **kwargs: Any) -> int:
        kwargs["hooks"].notify(warn("GINN-W-HOST-CHANGED", paths="/etc/hosts"))
        order.append("command")
        return 0

    monkeypatch.setattr(enter_cli, "enter_realm", enter_that_warns)
    assert main(["--json", "exec", "arch", "--", "true"]) == 0
    captured = capsys.readouterr()
    assert order == ["command"]
    assert captured.out == ""
    first = json.loads(captured.err.splitlines()[0])
    assert first["warning"]["code"] == "GINN-W-HOST-CHANGED"


@pytest.mark.fr("IR-011")
def test_an_unexpected_exception_is_a_ginn_failure_with_exit_125(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")

    def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(enter_cli, "enter_realm", broken)
    assert main(["exec", "arch", "--", "true"]) == 125
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-INTERNAL: ")
    assert "  note: RuntimeError: boom" in err


@pytest.mark.fr("IR-011")
def test_the_json_error_of_an_unexpected_exception_carries_its_note_and_125(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")

    def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(enter_cli, "enter_realm", broken)
    assert main(["--json", "enter", "arch"]) == 125
    error = json.loads(capsys.readouterr().err.splitlines()[-1])["error"]
    assert error["code"] == "GINN-E-INTERNAL"
    assert error["exit_code"] == 125
    assert error["notes"] == ["RuntimeError: boom"]


@pytest.mark.fr("IR-011")
def test_ctrl_c_during_exec_is_130_and_not_a_125(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")

    def interrupted(*args: Any, **kwargs: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(enter_cli, "enter_realm", interrupted)
    assert main(["exec", "arch", "--", "true"]) == 130
    assert capsys.readouterr().err.startswith("GINN-E-INTERRUPTED: ")
