# SPDX-License-Identifier: Apache-2.0
"""The progress line of a long operation on the output channel (FR-107, IR-030).

In JSON mode each finished step is one `{"progress": {"step", "message"}}` line on stderr, and it
does not depend on --verbose, because the Control Plane reads it. In text mode a step is a verbose
line, so a default text run prints nothing extra.
"""

from __future__ import annotations

import json

import pytest

from ginn.cli.output import Output


@pytest.mark.fr("FR-107")
def test_json_progress_is_one_line_without_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    Output(json_mode=True, verbose=False).progress("pulled", "create: step pulled done")

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "progress": {"step": "pulled", "message": "create: step pulled done"}
    }


@pytest.mark.fr("FR-107")
def test_json_progress_is_written_with_verbose_on_too(capsys: pytest.CaptureFixture[str]) -> None:
    Output(json_mode=True, verbose=True).progress("created", "create: step created done")

    lines = capsys.readouterr().err.splitlines()
    assert [json.loads(line) for line in lines] == [
        {"progress": {"step": "created", "message": "create: step created done"}}
    ]


@pytest.mark.fr("FR-107")
def test_text_progress_is_silent_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    Output(json_mode=False, verbose=False).progress("pulled", "create: step pulled done")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.fr("FR-107")
def test_text_progress_shows_its_message_under_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    Output(json_mode=False, verbose=True).progress("pulled", "create: step pulled done")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "create: step pulled done\n"


@pytest.mark.fr("FR-107")
def test_progress_keeps_stdout_for_the_result(capsys: pytest.CaptureFixture[str]) -> None:
    out = Output(json_mode=True, verbose=False)
    out.progress("packages", "create: step packages done")
    out.emit({"name": "arch"})

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"name": "arch"}
    assert json.loads(captured.err) == {
        "progress": {"step": "packages", "message": "create: step packages done"}
    }
