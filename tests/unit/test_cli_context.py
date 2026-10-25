# SPDX-License-Identifier: Apache-2.0
"""The production objects of the realm commands and their recovery report (IR-010, FR-122, NFR-042).

These tests read `ginn.cli._context` as the commands do. The defaults are the real ones: the host
tree is `/`, the helper is the installed one, and a create runs its package steps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.cli.output import Output
from ginn.domain import RealmState
from ginn.errors import EXEC_GINN_FAILURE, make
from ginn.ops.recovery import OUTCOME_BLOCKED, RecoveryOutcome
from ginn.podman.cli import PodmanCli
from ginn.store.journal import Journal
from tests.unit.realm_cli_support import World, make_realm, seed_killed_estimate, stored
from tests.unit.test_ops_recovery import _dead, _entry, _write_journal

REAL_PODMAN = _context.podman
"""The production adapter builder, captured before the `world` fixture replaces it."""


@pytest.mark.fr("IR-010")
def test_the_default_seams_are_the_real_host() -> None:
    seams = _context.seams()
    assert seams.root_dir == Path("/")
    assert seams.helper is None
    assert seams.integrate is True
    assert seams.mountinfo == Path("/proc/self/mountinfo")


@pytest.mark.fr("NFR-042")
def test_the_real_adapter_echoes_its_commands_to_verbose_output(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    out = Output(json_mode=False, verbose=True)
    adapter = REAL_PODMAN(world.paths, out)
    assert isinstance(adapter, PodmanCli)
    assert adapter.verbose == out.verbose_line
    assert adapter.binary == _context.podman_program(world.paths)  # absolute, outside bin
    assert adapter.oci_runtime == "crun"


@pytest.mark.fr("FR-025")
def test_tightened_directories_are_verbose_lines_only(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    out = Output(json_mode=False, verbose=False)
    adapter = REAL_PODMAN(world.paths, out)
    adapter.tightened.append("tightened /x/ginn/realms from 0755 to 0700")
    realms = _context.Realms(
        paths=world.paths,
        env=_context.op_env(world.paths),
        podman=adapter,
        host=_context.host_user(),
        gid=0,
    )
    _context.finish(realms, out)
    assert capsys.readouterr().err == ""
    verbose = Output(json_mode=False, verbose=True)
    _context.finish(realms, verbose)
    assert "tightened /x/ginn/realms" in capsys.readouterr().err


@pytest.mark.fr("FR-122")
def test_a_settled_interrupted_create_is_a_warning_with_its_remedy(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "debian", RealmState.CREATING, running=False)
    _write_journal(world.paths, _dead(op="create", realm="debian", step="pulled"))
    assert main(["list"]) == 0
    captured = capsys.readouterr()
    assert captured.err.startswith(
        "GINN-W-RECOVERED: recovery settled an interrupted create of realm 'debian' as broken: "
    )
    assert "  fix: run 'ginn status debian'" in captured.err
    assert "realm debian is broken" in captured.out


@pytest.mark.fr("FR-122")
def test_a_sweep_of_a_killed_estimate_is_a_verbose_line_even_when_it_is_blocked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocked = RecoveryOutcome(
        realm="estimate:kali",
        op="estimate",
        op_id=None,
        outcome=OUTCOME_BLOCKED,
        reason="the sweep stopped (GINN-E-INTERNAL); the leftovers are kept for the next command",
        path="containers labelled io.ginn.estimate=kali",
    )

    _context._report(blocked, Output(json_mode=False, verbose=False))
    assert capsys.readouterr().err == ""  # no warning, and nothing without --verbose

    _context._report(blocked, Output(json_mode=False, verbose=True))
    err = capsys.readouterr().err
    assert err.startswith("recovery: containers labelled io.ginn.estimate=kali: blocked: ")
    assert "GINN-W" not in err


@pytest.mark.fr("FR-122")
def test_a_live_journal_entry_is_only_a_verbose_line(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "debian", RealmState.CREATING, running=False)
    own = os.getpid()
    _write_journal(world.paths, _entry(op="create", realm="debian", pid=own, pid_start=None))
    assert main(["list"]) == 0
    captured = capsys.readouterr()
    assert "GINN-W" not in captured.err
    assert captured.err == ""


@pytest.mark.fr("FR-122")
def test_a_json_list_carries_the_recovery_warning_in_its_result(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "debian", RealmState.CREATING, running=False)
    _write_journal(world.paths, _dead(op="create", realm="debian", step="pulled"))
    assert main(["--json", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"][0]["code"] == "GINN-W-RECOVERED"


@pytest.mark.fr("IR-011")
def test_the_ginn_side_exit_status_for_exec_is_125() -> None:
    assert EXEC_GINN_FAILURE == 125


@pytest.mark.fr("IR-011")
def test_json_exec_flag_before_the_command_parses(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True, distro="arch")
    assert main(["--json", "exec", "arch", "--", "true"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.fr("IR-011")
def test_a_failure_prints_each_note_it_collected_in_text_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    err = make("GINN-E-INTERNAL")
    err.add_note("stop after failed start: GINN-E-NETWORK")
    err.add_note("record not set stopped: GINN-E-INTERNAL")
    Output(json_mode=False, verbose=False).error(err)
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("GINN-E-INTERNAL: ")
    assert lines[1].startswith("  fix: ")
    assert lines[2:] == [
        "  note: stop after failed start: GINN-E-NETWORK",
        "  note: record not set stopped: GINN-E-INTERNAL",
    ]


@pytest.mark.fr("IR-011")
def test_a_json_failure_carries_its_notes_in_one_object_per_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    err = make("GINN-E-INTERNAL")
    err.add_note("rollback: container: removed")
    Output(json_mode=True, verbose=False).error(err)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    error = json.loads(lines[0])["error"]
    assert error["notes"] == ["rollback: container: removed"]


@pytest.mark.fr("IR-011")
def test_a_failure_without_notes_has_an_empty_notes_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    Output(json_mode=True, verbose=False).error(make("GINN-E-USAGE", detail="x"))
    error = json.loads(capsys.readouterr().err.splitlines()[-1])["error"]
    assert error["notes"] == []


@pytest.mark.fr("FR-122")
@pytest.mark.parametrize("command", ["status", "list"])
def test_status_and_list_warn_and_go_on_when_the_journal_scan_raises_os_error(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)

    def denied(self: Journal) -> list[object]:
        raise PermissionError(13, "Permission denied", str(world.paths.journal))

    monkeypatch.setattr(Journal, "scan", denied)
    argv = [command, "arch"] if command == "status" else [command]
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert "arch" in captured.out
    assert captured.err.startswith("GINN-W-RECOVERY-UNSETTLED: ")
    assert "PermissionError" in captured.err


@pytest.mark.fr("FR-122")
def test_a_realm_command_that_changes_state_stops_when_the_journal_scan_raises(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)

    def denied(self: Journal) -> list[object]:
        raise PermissionError(13, "Permission denied", str(world.paths.journal))

    monkeypatch.setattr(Journal, "scan", denied)
    assert main(["start", "arch"]) == 70
    assert capsys.readouterr().err.startswith("GINN-E-INTERNAL: ")
    assert stored(world, "arch").state is RealmState.STOPPED


@pytest.mark.fr("FR-107")
def test_create_hooks_report_each_step_on_the_output_of_realms(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    out = Output(json_mode=True, verbose=False)
    _context.realms(out)

    hooks = _context.create_hooks(world.paths)

    assert hooks.on_step is not None
    hooks.on_step("pulled")
    lines = capsys.readouterr().err.splitlines()
    assert [json.loads(line) for line in lines] == [
        {"progress": {"step": "pulled", "message": "create: step pulled done"}}
    ]


@pytest.mark.fr("FR-107")
def test_delete_hooks_report_each_step_on_an_explicit_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    hooks = _context.delete_hooks(Output(json_mode=True, verbose=False))

    assert hooks.on_step is not None
    hooks.on_step("container-removed")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "progress": {"step": "container-removed", "message": "delete: step container-removed done"}
    }


@pytest.mark.fr("FR-107")
def test_text_mode_hooks_print_nothing_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    hooks = _context.delete_hooks(Output(json_mode=False, verbose=False))

    assert hooks.on_step is not None
    hooks.on_step("home")
    assert capsys.readouterr().err == ""


@pytest.mark.fr("FR-107")
def test_hooks_have_no_progress_without_an_output() -> None:
    _context._output = None

    assert _context.create_hooks(None, None).on_step is None
    assert _context.delete_hooks().on_step is None
    assert _context.rebuild_hooks(None, None).on_step is None


@pytest.mark.fr("FR-021")
def test_the_rebuild_hooks_report_each_step_as_a_rebuild_progress_line(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    hooks = _context.rebuild_hooks(world.paths, Output(json_mode=True, verbose=False))

    assert hooks.on_step is not None
    hooks.on_step("swapping")

    line = json.loads(capsys.readouterr().err)
    assert line == {"progress": {"step": "swapping", "message": "rebuild: step swapping done"}}


@pytest.mark.fr("FR-083")
def test_the_lifecycle_hooks_carry_the_rebuild_seams_and_the_ginn_command(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that rebuilds a stale services-on realm writes the ginn command into its runit
    scripts. Without a program the rebuild fails with GINN-E-INTERNAL, so the start carries it."""
    monkeypatch.setattr(_context, "ginn_program", lambda paths=None: "/usr/bin/ginn")

    hooks = _context.lifecycle_hooks(world.paths)

    rebuild = hooks.rebuild
    assert rebuild is not None
    assert rebuild.program == "/usr/bin/ginn"
    assert rebuild.helper == hooks.helper
    assert rebuild.exists is hooks.exists
    assert rebuild.mountinfo == hooks.mountinfo
    # The rebuild's own stop, start and runit seams are the host's, not the library defaults.
    assert rebuild.lifecycle.helper == hooks.helper
    assert rebuild.lifecycle.exists is hooks.exists
    assert rebuild.lifecycle.mountinfo == hooks.mountinfo


@pytest.mark.fr("FR-083")
def test_a_bare_lifecycle_hooks_call_looks_up_the_ginn_command_for_the_paths_of_realms(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ginn start` calls `lifecycle_hooks()` with no paths. It uses the paths that `realms()` was
    given, as create does, so the program is found the same way for both (FR-046, FR-083)."""
    seen: list[object] = []

    def program(paths: object = None) -> str:
        seen.append(paths)
        return "/usr/bin/ginn"

    monkeypatch.setattr(_context, "ginn_program", program)
    monkeypatch.setattr(_context, "_paths", None)
    paths = _context.realms(Output(json_mode=True, verbose=False)).paths

    hooks = _context.lifecycle_hooks()

    assert seen == [paths]
    assert hooks.rebuild is not None
    assert hooks.rebuild.program == "/usr/bin/ginn"


SWEEPING_COMMANDS = [
    ["create", "--resume", "nope"],
    ["delete", "nope"],
    ["rebuild", "nope"],
    ["update", "nope"],
]
"""The commands that touch the Kali image, so each sweeps a killed estimate before its own work."""
LAUNCH_COMMANDS = [["enter", "nope"], ["start", "nope"], ["install", "nope", "htop"]]
"""The app launch, the start and the install, which run no sweep (NFR-004)."""
READ_ONLY_COMMANDS = [["list"], ["status", "nope"], ["update", "--check"]]
"""The commands that write no state, which run no sweep."""


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-014")
@pytest.mark.parametrize("argv", SWEEPING_COMMANDS, ids=" ".join)
def test_create_delete_rebuild_and_update_sweep_a_killed_estimate_before_their_work(
    world: World, argv: list[str]
) -> None:
    seed_killed_estimate(world)

    main(argv)  # refused for the unknown realm, after the sweep has run

    assert world.fake.containers == {}


@pytest.mark.fr("FR-122")
@pytest.mark.fr("NFR-004")
@pytest.mark.parametrize("argv", LAUNCH_COMMANDS, ids=" ".join)
def test_an_app_launch_a_start_and_an_install_leave_a_killed_estimate_alone(
    world: World, argv: list[str]
) -> None:
    seed_killed_estimate(world)

    main(argv)  # refused for the unknown realm, with no sweep before it

    assert len(world.fake.containers) == 1


@pytest.mark.fr("FR-122")
@pytest.mark.parametrize("argv", READ_ONLY_COMMANDS, ids=" ".join)
def test_a_read_only_command_leaves_a_killed_estimate_alone(world: World, argv: list[str]) -> None:
    seed_killed_estimate(world)

    main(argv)

    assert len(world.fake.containers) == 1


def _realms_of(world: World) -> _context.Realms:
    return _context.Realms(
        paths=world.paths,
        env=_context.op_env(world.paths),
        podman=world.fake,
        host=_context.host_user(),
        gid=0,
    )


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-080")
def test_a_command_that_passes_the_sweep_flag_removes_a_killed_creates_scratch(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scratch of a killed create is swept by the commands that pass `sweep`, and by no other:
    a read-only command runs no sweep, so it leaves the leftover where it is (F3, F6)."""
    leftover = world.paths.runtime / "create-scratch-killed"
    leftover.mkdir()
    (leftover / "init").write_bytes(b"systemd")
    out = Output(json_mode=False, verbose=True)
    _context.settle_interrupted(_realms_of(world), out, read_only=True, sweep=True)
    assert leftover.is_dir(), "a read-only command never sweeps"
    _context.settle_interrupted(_realms_of(world), out, sweep=True)
    assert not leftover.exists()
    assert "scratch directories of killed creates are removed" in capsys.readouterr().err


@pytest.mark.fr("FR-122")
def test_a_scratch_sweep_outcome_is_a_verbose_line_even_when_it_is_blocked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocked = RecoveryOutcome(
        realm="scratch",
        op="scratch",
        op_id=None,
        outcome=OUTCOME_BLOCKED,
        reason="is not a directory of this user (a link, a file or another owner)",
        path="/run/ginn/1000/create-scratch-link",
    )
    _context._report(blocked, Output(json_mode=False, verbose=False))
    assert capsys.readouterr().err == ""  # no warning, and nothing without --verbose
    _context._report(blocked, Output(json_mode=False, verbose=True))
    err = capsys.readouterr().err
    assert err.startswith("recovery: /run/ginn/1000/create-scratch-link: blocked: ")
    assert "GINN-W" not in err
