# SPDX-License-Identifier: Apache-2.0
"""CLI tests: version, usage errors, stream contract, exit statuses and help audit.

The probe command is registered only inside a test, by replacing ginn.cli.commands.COMMANDS.
The root parser reads that name at call time, so the replacement reaches it.
"""

import argparse
import json
from collections.abc import Callable
from typing import Any

import pytest

import ginn.cli.main as cli_main
from ginn import __version__
from ginn.__main__ import main
from ginn.cli import commands as registry
from ginn.cli.commands import CommandSpec, Context
from ginn.cli.output import Output
from ginn.errors import make, warn

RunFn = Callable[[argparse.Namespace, Context], int]


def _install_probe(
    monkeypatch: pytest.MonkeyPatch,
    run: RunFn,
    configure: Callable[[argparse.ArgumentParser], None] | None = None,
) -> None:
    """Register one `probe` command for this test only."""
    probe = CommandSpec(
        name="probe",
        help="probe the CLI",
        example="ginn probe",
        configure=configure or _no_arguments,
        run=run,
    )
    monkeypatch.setattr(registry, "COMMANDS", [probe])


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    """Configure hook for a probe with no arguments."""


def _positional_target(parser: argparse.ArgumentParser) -> None:
    """Configure hook for a probe with one required positional, with help text."""
    parser.add_argument("target", help="the probe target")


def _succeed(args: argparse.Namespace, ctx: Context) -> int:
    return 0


def _raising(exc: BaseException) -> RunFn:
    def run(args: argparse.Namespace, ctx: Context) -> int:
        raise exc

    return run


def _json_lines(text: str) -> list[Any]:
    return [json.loads(line) for line in text.splitlines()]


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("the root parser has no subparsers action")


@pytest.mark.fr("IR-010")
def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.fr("IR-011")
def test_no_command_exits_two_with_usage_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    captured = capsys.readouterr()
    assert "GINN-E-USAGE" in captured.err
    assert "Example:" in captured.err


@pytest.mark.fr("IR-011")
def test_unknown_command_exits_two_with_usage_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bogus"]) == 2
    err = capsys.readouterr().err
    assert "GINN-E-USAGE: unknown command 'bogus'" in err
    assert "  fix: " in err


@pytest.mark.fr("AS-025")
def test_json_unknown_command_writes_one_json_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "bogus"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "GINN-E-USAGE"
    assert payload["error"]["exit_code"] == 2
    assert payload["error"]["details"] == {"detail": "unknown command 'bogus'"}


@pytest.mark.fr("NFR-052")
def test_help_has_example(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "Example:" in capsys.readouterr().out


@pytest.mark.fr("AS-025")
@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["--json", "probe"], True), (["probe", "--json"], True), (["probe"], False)],
)
def test_json_flag_reaches_command_before_or_after_name(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: bool
) -> None:
    seen: list[bool] = []

    def run_probe(args: argparse.Namespace, ctx: Context) -> int:
        seen.append(ctx.output.json_mode)
        return 0

    _install_probe(monkeypatch, run_probe)
    assert cli_main.run(argv) == 0
    assert seen == [expected]


@pytest.mark.fr("NFR-042")
@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["--verbose", "probe"], True), (["probe", "--verbose"], True), (["probe"], False)],
)
def test_verbose_flag_reaches_command_before_or_after_name(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: bool
) -> None:
    seen: list[bool] = []

    def run_probe(args: argparse.Namespace, ctx: Context) -> int:
        seen.append(ctx.output.verbose)
        return 0

    _install_probe(monkeypatch, run_probe)
    assert cli_main.run(argv) == 0
    assert seen == [expected]


@pytest.mark.fr("AS-025")
def test_json_error_keeps_stdout_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(args: argparse.Namespace, ctx: Context) -> int:
        raise make("GINN-E-NAME-TAKEN", name="arch")

    _install_probe(monkeypatch, fail)
    assert main(["--json", "probe"]) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "GINN-E-NAME-TAKEN"
    assert payload["error"]["exit_code"] == 4
    assert payload["error"]["details"] == {"name": "arch"}


@pytest.mark.fr("IR-011")
def test_argparse_missing_positional_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_probe(monkeypatch, _succeed, _positional_target)
    assert main(["probe"]) == 2
    assert "GINN-E-USAGE" in capsys.readouterr().err


@pytest.mark.fr("IR-011")
def test_argparse_unknown_option_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_probe(monkeypatch, _succeed)
    assert main(["probe", "--bogus"]) == 2
    assert "GINN-E-USAGE" in capsys.readouterr().err


@pytest.mark.fr("IR-010")
@pytest.mark.parametrize("argv", [["--js", "probe", "x"], ["probe", "x", "--js"]])
def test_abbreviated_option_is_rejected_in_both_positions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    ran: list[str] = []

    def run_probe(args: argparse.Namespace, ctx: Context) -> int:
        ran.append("probe")
        return 0

    _install_probe(monkeypatch, run_probe, _positional_target)
    assert main(argv) == 2
    assert "GINN-E-USAGE" in capsys.readouterr().err
    assert ran == []


@pytest.mark.fr("IR-011")
def test_keyboard_interrupt_exits_130_with_interrupted_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_probe(monkeypatch, _raising(KeyboardInterrupt()))
    assert main(["probe"]) == 130
    assert "GINN-E-INTERRUPTED" in capsys.readouterr().err


@pytest.mark.fr("NFR-040")
def test_unexpected_error_exits_70_with_internal_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_probe(monkeypatch, _raising(RuntimeError("boom")))
    assert main(["probe"]) == 70
    captured = capsys.readouterr()
    assert "GINN-E-INTERNAL" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.fr("NFR-042")
def test_json_verbose_traceback_precedes_the_error_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_probe(monkeypatch, _raising(RuntimeError("boom")))
    assert main(["--json", "--verbose", "probe"]) == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = _json_lines(captured.err)
    assert len(lines) >= 2
    assert "boom" in str(lines[0]["verbose"])
    assert lines[-1]["error"]["code"] == "GINN-E-INTERNAL"
    assert lines[-1]["error"]["exit_code"] == 70


@pytest.mark.fr("NFR-042")
def test_text_verbose_traceback_precedes_the_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_probe(monkeypatch, _raising(RuntimeError("boom")))
    assert main(["--verbose", "probe"]) == 70
    err = capsys.readouterr().err
    assert err.index("Traceback") < err.index("GINN-E-INTERNAL: ")


@pytest.mark.fr("IR-010")
def test_root_options_are_flags_only() -> None:
    """Pins the assumption behind _first_positional: no root option takes a value."""
    parser = cli_main.build_parser()
    for action in parser._actions:
        if action.option_strings:
            assert action.nargs == 0, f"root option {action.option_strings} takes a value"


@pytest.mark.fr("NFR-052")
def test_help_audit_every_subcommand_has_example_and_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_probe(monkeypatch, _succeed, _positional_target)
    parser = cli_main.build_parser()
    subparsers = _subparsers(parser)
    assert set(subparsers) == {spec.name for spec in registry.COMMANDS}
    audited = {"root": parser, **subparsers}
    for name, target in audited.items():
        assert "Example:" in (target.epilog or ""), f"{name}: epilog has no example"
        for action in target._actions:
            assert action.help is not None, f"{name}: {action.dest} has no help"
            assert action.help != argparse.SUPPRESS, f"{name}: {action.dest} is suppressed"


@pytest.mark.fr("AS-025")
def test_json_warning_is_added_to_the_result(capsys: pytest.CaptureFixture[str]) -> None:
    out = Output(json_mode=True, verbose=False)
    out.warn(warn("GINN-W-HOST-CHANGED", paths="/etc/hosts"))
    out.emit({"ok": True})
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": True,
        "warnings": [
            {
                "code": "GINN-W-HOST-CHANGED",
                "message": "host files changed since baseline: /etc/hosts",
                "remedy": "review the listed paths, then run 'ginn integrity --ack'",
            }
        ],
    }


@pytest.mark.fr("NFR-040")
def test_text_warning_goes_to_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    out = Output(json_mode=False, verbose=False)
    out.warn(warn("GINN-W-HOST-CHANGED", paths="/etc/hosts"))
    out.emit({"ok": True}, render=lambda: "done")
    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert captured.err.startswith(
        "GINN-W-HOST-CHANGED: host files changed since baseline: /etc/hosts\n  fix: "
    )


@pytest.mark.fr("AS-025")
def test_json_warning_is_kept_when_the_command_fails(capsys: pytest.CaptureFixture[str]) -> None:
    out = Output(json_mode=True, verbose=False)
    out.warn(warn("GINN-W-HOST-CHANGED", paths="/etc/hosts"))
    out.error(make("GINN-E-NAME-TAKEN", name="arch"))
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = _json_lines(captured.err)
    assert lines[0]["warning"]["code"] == "GINN-W-HOST-CHANGED"
    assert lines[-1]["error"]["code"] == "GINN-E-NAME-TAKEN"


@pytest.mark.fr("NFR-042")
def test_json_verbose_line_is_one_json_object(capsys: pytest.CaptureFixture[str]) -> None:
    Output(json_mode=True, verbose=True).verbose_line("podman ps\n")
    assert capsys.readouterr().err == '{"verbose": "podman ps"}\n'


@pytest.mark.fr("NFR-040")
def test_registry_error_renders_as_internal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken command registry fails inside the boundary, as GINN-E-INTERNAL (exit 70)."""
    broken = CommandSpec(
        name="probe", help="probe", example="  ", configure=_no_arguments, run=_succeed
    )
    monkeypatch.setattr(registry, "COMMANDS", [broken])
    assert main(["--json", "probe"]) == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_lines(captured.err)[-1]["error"]["code"] == "GINN-E-INTERNAL"
