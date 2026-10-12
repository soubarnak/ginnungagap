# SPDX-License-Identifier: Apache-2.0
"""CLI smoke tests: version, usage errors, JSON errors and help examples (IR-010, IR-011)."""

import argparse
import json

import pytest

import ginn.cli.main as cli_main
from ginn import __version__
from ginn.__main__ import main
from ginn.cli.commands import CommandSpec, Context


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
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == "GINN-E-USAGE"
    assert payload["error"]["exit_code"] == 2


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

    probe = CommandSpec(
        name="probe",
        help="probe global flags",
        example="ginn probe --json",
        configure=lambda parser: None,
        run=run_probe,
    )
    monkeypatch.setattr(cli_main, "COMMANDS", [probe])
    assert cli_main.run(argv) == 0
    assert seen == [expected]
