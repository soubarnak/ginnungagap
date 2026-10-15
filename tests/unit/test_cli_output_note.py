# SPDX-License-Identifier: Apache-2.0
"""The note line of a successful command on the output channel (IR-030, ADR-015 Decision 14).

In text mode `Output.note` writes `note: text` to stderr, once per note and whatever --verbose is.
In JSON mode it writes nothing, because the command carries its notes in the result, under `notes`.
"""

from __future__ import annotations

import json

import pytest

from ginn.cli.output import Output


@pytest.mark.fr("IR-030")
def test_text_note_is_one_line_on_stderr_and_stdout_stays_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    Output(json_mode=False, verbose=False).note("'systemd' is an init package of realm 'deb'")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "note: 'systemd' is an init package of realm 'deb'\n"


@pytest.mark.fr("IR-030")
def test_text_notes_are_written_in_order_without_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = Output(json_mode=False, verbose=False)
    out.note("first")
    out.note("second")

    assert capsys.readouterr().err == "note: first\nnote: second\n"


@pytest.mark.fr("IR-030")
def test_json_note_writes_nothing_on_either_stream(capsys: pytest.CaptureFixture[str]) -> None:
    out = Output(json_mode=True, verbose=True)
    out.note("'systemd' is an init package of realm 'deb'")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.fr("IR-030")
def test_json_result_carries_the_notes_and_stderr_stays_clear(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = Output(json_mode=True, verbose=False)
    out.note("kept in the result")
    out.emit({"realm": "deb", "notes": ["kept in the result"]})

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"realm": "deb", "notes": ["kept in the result"]}
    assert captured.err == ""
