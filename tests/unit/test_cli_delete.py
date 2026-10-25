# SPDX-License-Identifier: Apache-2.0
"""`ginn delete` through the CLI entry point: the purge confirmation (FR-019, AS-014, FR-006).

The prompt is written to stderr and read from stdin, and it is asked only when both are terminals.
A test stands in terminals with `TerminalInput` and `TerminalOutput`. A script's case uses plain
text input, and a `--json` run never prompts. The read-only checks of the purge run before the
prompt, so a refused purge asks nothing.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.domain import RealmName, RealmState
from ginn.ops._mutating import OpEnv
from ginn.ops.delete import DeleteHooks, check_purge
from ginn.store.records import RealmStore
from tests.unit.realm_cli_support import World, make_realm, stored


class TerminalInput(io.StringIO):
    """Standard input that reports a terminal, with the answer as its one line."""

    def isatty(self) -> bool:
        return True


class TerminalOutput(io.StringIO):
    """Standard error that reports a terminal, so the prompt is written where a user sees it."""

    def isatty(self) -> bool:
        return True


def _terminals(monkeypatch: pytest.MonkeyPatch, answer: str) -> TerminalOutput:
    """Stand both streams in terminals, with `answer` on stdin. Returns the stderr stand-in."""
    stderr = TerminalOutput()
    monkeypatch.setattr(sys, "stdin", TerminalInput(answer))
    monkeypatch.setattr(sys, "stderr", stderr)
    return stderr


def _home(world: World, name: str) -> Path:
    home = world.paths.homes / name
    (home / "notes").mkdir(parents=True, exist_ok=True)
    (home / "notes" / "todo.txt").write_text("keep me\n", encoding="utf-8")
    return home


@pytest.mark.fr("FR-019")
def test_a_plain_delete_removes_the_realm_and_keeps_the_home(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    home = _home(world, "arch")
    assert main(["delete", "arch"]) == 0
    out = capsys.readouterr().out
    assert "realm arch deleted" in out
    assert "Realm Home kept" in out
    assert (home / "notes" / "todo.txt").read_text(encoding="utf-8") == "keep me\n"
    assert RealmStore(world.paths).list_names() == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("AS-014")
def test_purge_with_yes_removes_the_home_without_a_prompt(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    home = _home(world, "arch")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # a prompt would read end of input
    assert main(["delete", "arch", "--purge-home", "--yes"]) == 0
    assert "Realm Home removed" in capsys.readouterr().out
    assert not home.exists()


@pytest.mark.fr("AS-014")
def test_purge_without_a_terminal_or_yes_is_refused_before_anything_runs(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    home = _home(world, "arch")
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    assert main(["delete", "arch", "--purge-home"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-USAGE: ")
    assert "--yes" in err
    assert home.exists()
    assert stored(world, "arch").state is RealmState.STOPPED


@pytest.mark.fr("AS-014")
def test_a_stdin_terminal_with_a_redirected_stderr_is_not_a_prompt(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The prompt must be seen: stderr has to be a terminal as well as stdin (AS-014)."""
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    home = _home(world, "arch")
    monkeypatch.setattr(sys, "stdin", TerminalInput("y\n"))
    assert main(["delete", "arch", "--purge-home"]) == 2
    assert capsys.readouterr().err.startswith("GINN-E-USAGE: ")
    assert home.exists()


@pytest.mark.fr("AS-014")
def test_a_json_purge_without_yes_is_refused_and_never_prompts(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    home = _home(world, "arch")
    stderr = _terminals(monkeypatch, "y\n")
    assert main(["--json", "delete", "arch", "--purge-home"]) == 2
    assert capsys.readouterr().out == ""
    error = json.loads(stderr.getvalue().splitlines()[-1])["error"]
    assert error["code"] == "GINN-E-USAGE"
    assert "--yes" in error["message"]
    assert "[y/N]" not in stderr.getvalue()
    assert home.exists()


@pytest.mark.fr("AS-014")
def test_a_terminal_answer_of_yes_confirms_the_purge(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    home = _home(world, "arch")
    stderr = _terminals(monkeypatch, "yes\n")
    assert main(["delete", "arch", "--purge-home"]) == 0
    assert "[y/N] " in stderr.getvalue()
    assert "Realm Home removed" in capsys.readouterr().out
    assert not home.exists()


@pytest.mark.fr("AS-014")
@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_a_terminal_answer_that_is_not_yes_declines_and_keeps_the_home(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    home = _home(world, "arch")
    _terminals(monkeypatch, answer)
    assert main(["delete", "arch", "--purge-home"]) == 0
    assert "purge declined: the Realm Home is kept" in capsys.readouterr().out
    assert home.exists()
    assert not (world.paths.realms / "arch.json").exists()


@pytest.mark.fr("AS-014")
def test_a_refused_purge_asks_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A homes directory that is a link is refused by the read-only check, before the prompt."""
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    real_homes = world.paths.homes
    moved = world.tmp / "moved-homes"
    real_homes.rename(moved)
    real_homes.symlink_to(moved, target_is_directory=True)
    stderr = _terminals(monkeypatch, "y\n")
    assert main(["delete", "arch", "--purge-home"]) == 77
    assert stderr.getvalue().startswith("GINN-E-HOME-UNSAFE: ")
    assert "[y/N]" not in stderr.getvalue()
    assert stored(world, "arch").state is RealmState.STOPPED


@pytest.mark.fr("FR-025")
def test_a_purge_of_an_unknown_realm_is_not_found_before_the_prompt(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stderr = _terminals(monkeypatch, "y\n")
    assert main(["delete", "nosuch", "--purge-home"]) == 3
    assert stderr.getvalue().startswith("GINN-E-NO-SUCH-REALM: ")
    assert "[y/N]" not in stderr.getvalue()


@pytest.mark.fr("FR-019")
def test_the_json_result_names_the_home_and_the_image(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    assert main(["--json", "delete", "arch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["realm"] == "arch"
    assert payload["home"] == "kept"
    assert payload["purge_declined"] is False
    assert payload["image"] in {"removed", "kept", "absent"}
    assert isinstance(payload["notes"], list)
    assert "warnings" not in payload  # emit adds the key only when a warning was queued


@pytest.mark.fr("FR-006")
@pytest.mark.parametrize("as_json", [False, True])
def test_a_host_change_during_delete_is_warned_in_text_and_in_the_json_result(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")

    def change_after_stop(step: str) -> None:
        if step == "stopped":
            (world.host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")

    monkeypatch.setattr(_context, "delete_hooks", lambda: DeleteHooks(on_step=change_after_stop))
    argv = ["--json", "delete", "arch"] if as_json else ["delete", "arch"]
    assert main(argv) == 0
    captured = capsys.readouterr()
    if as_json:
        (notice,) = json.loads(captured.out)["warnings"]
        assert notice["code"] == "GINN-W-HOST-CHANGED"
    else:
        assert captured.err.startswith("GINN-W-HOST-CHANGED: ")


@pytest.mark.fr("FR-019")
def test_check_purge_is_read_only(world: World) -> None:
    """The read-only check of a purge changes no record, no home and no journal file."""
    make_realm(world, "arch", RealmState.STOPPED, running=False, distro="arch")
    home = _home(world, "arch")
    before = (world.paths.realms / "arch.json").read_bytes()
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host)
    check_purge(env, RealmName("arch"))
    assert (world.paths.realms / "arch.json").read_bytes() == before
    assert home.exists()
    assert not list(world.paths.journal.glob("*.json"))
