# SPDX-License-Identifier: Apache-2.0
"""`ginn log [<realm>] [--since <time>] [--json]` through the CLI entry point (FR-125, IR-010).

The Audit Log is written with the store, then read through `ginn.__main__.main`. The conftest pins
every XDG variable and the runtime override to tmp_path, so no test reads the real host. The
command is read-only, and a test checks that.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.store.audit import AuditEntry, AuditLog
from ginn.store.paths import OwnedPaths

WRITTEN = (
    AuditEntry(
        user="tester",
        realm="arch",
        op="create",
        outcome="ok",
        params={"distro": "arch"},
        ts="2026-10-01T09:00:00.000Z",
    ),
    AuditEntry(
        user="tester",
        realm="debian",
        op="create",
        outcome="error",
        error_code="GINN-E-NETWORK",
        ts="2026-10-01T10:00:00.000Z",
    ),
    AuditEntry(
        user="tester",
        realm="arch",
        op="tier",
        outcome="ok",
        before={"tier": "basic"},
        after={"tier": "development"},
        ts="2026-10-02T08:30:00.000Z",
    ),
    AuditEntry(
        user="tester",
        realm=None,
        op="integrity",
        outcome="ok",
        ts="2026-10-03T07:00:00.000Z",
    ),
)


@pytest.fixture(autouse=True)
def _pinned_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every XDG variable and the runtime override to tmp_path, as the store tests do."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))


@pytest.fixture
def paths() -> Iterator[OwnedPaths]:
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    yield resolved


@pytest.fixture
def log(paths: OwnedPaths) -> OwnedPaths:
    audit = AuditLog(paths)
    for entry in WRITTEN:
        audit.append(entry)
    return paths


def _json(text: str) -> Any:
    return json.loads(text)


def _starts(lines: list[str]) -> list[str]:
    return [line.split(" ", 1)[0] for line in lines]


@pytest.mark.fr("FR-125")
def test_text_output_lists_every_entry_oldest_start_first(
    log: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["log"]) == 0
    captured = capsys.readouterr()

    lines = captured.out.splitlines()
    assert _starts(lines) == [entry.ts for entry in WRITTEN]
    assert lines[0] == "2026-10-01T09:00:00.000Z tester arch create ok"
    assert lines[1] == "2026-10-01T10:00:00.000Z tester debian create error GINN-E-NETWORK"
    assert lines[3] == "2026-10-03T07:00:00.000Z tester - integrity ok"
    assert captured.err == ""


@pytest.mark.fr("FR-125")
def test_a_realm_argument_keeps_only_that_realms_entries(
    log: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["log", "arch"]) == 0
    lines = capsys.readouterr().out.splitlines()

    assert _starts(lines) == ["2026-10-01T09:00:00.000Z", "2026-10-02T08:30:00.000Z"]
    assert all(" arch " in line for line in lines)


@pytest.mark.fr("IR-010")
@pytest.mark.fr("FR-125")
def test_json_output_is_an_entries_object_with_the_dr050_fields(
    log: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "log", "arch"]) == 0
    payload = _json(capsys.readouterr().out)

    assert set(payload) == {"entries"}
    rows = payload["entries"]
    assert [row["ts"] for row in rows] == ["2026-10-01T09:00:00.000Z", "2026-10-02T08:30:00.000Z"]
    assert set(rows[0]) == {
        "ts",
        "user",
        "realm",
        "op",
        "params",
        "before",
        "after",
        "outcome",
        "error_code",
    }
    assert rows[1]["before"] == {"tier": "basic"}
    assert rows[1]["after"] == {"tier": "development"}


@pytest.mark.fr("IR-010")
def test_json_output_for_all_realms_includes_the_host_event_with_a_null_realm(
    log: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "log"]) == 0
    rows = _json(capsys.readouterr().out)["entries"]

    assert len(rows) == 4
    assert rows[1]["error_code"] == "GINN-E-NETWORK"
    assert rows[3]["realm"] is None


@pytest.mark.fr("FR-125")
def test_since_keeps_the_entries_that_started_at_or_after_the_time(
    log: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "log", "--since", "2026-10-01T10:00:00Z"]) == 0
    rows = _json(capsys.readouterr().out)["entries"]

    assert [row["ts"] for row in rows] == [
        "2026-10-01T10:00:00.000Z",
        "2026-10-02T08:30:00.000Z",
        "2026-10-03T07:00:00.000Z",
    ]


@pytest.mark.fr("FR-125")
def test_since_without_a_zone_is_read_as_local_time(
    log: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    floor = datetime(2026, 10, 2, 0, 0).astimezone()
    expected = [e.ts for e in WRITTEN if datetime.fromisoformat(str(e.ts)) >= floor]

    assert main(["--json", "log", "--since", "2026-10-02T00:00:00"]) == 0
    rows = _json(capsys.readouterr().out)["entries"]

    assert [row["ts"] for row in rows] == expected


@pytest.mark.fr("IR-011")
def test_an_unreadable_since_value_is_a_usage_error_naming_the_value(
    log: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["log", "--since", "yesterday"]) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "GINN-E-USAGE" in captured.err
    assert "'yesterday'" in captured.err


@pytest.mark.fr("IR-011")
def test_an_invalid_realm_name_is_a_usage_error(
    log: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "log", "Bad_Name"]) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    payload = _json(captured.err)
    assert payload["error"]["code"] == "GINN-E-NAME-INVALID"
    assert payload["error"]["exit_code"] == 2


@pytest.mark.fr("FR-125")
def test_an_empty_log_prints_no_entries_and_exits_zero(
    paths: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["log"]) == 0
    assert capsys.readouterr().out == "no audit entries\n"

    assert main(["--json", "log"]) == 0
    assert _json(capsys.readouterr().out) == {"entries": []}


@pytest.mark.fr("FR-124")
@pytest.mark.fr("FR-125")
def test_a_torn_last_line_from_a_crash_is_skipped(
    log: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    with log.audit_log.open("ab") as handle:
        handle.write(b'{"ts": "2026-10-04T00:00:00.000Z", "user": "tes')

    assert main(["--json", "log"]) == 0
    rows = _json(capsys.readouterr().out)["entries"]

    assert len(rows) == len(WRITTEN)


@pytest.mark.fr("IR-011")
@pytest.mark.fr("FR-124")
def test_a_damaged_line_before_the_end_fails_with_internal_and_empty_stdout(
    log: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = log.audit_log.read_bytes().splitlines(keepends=True)
    log.audit_log.write_bytes(lines[0] + b"garbage, not json\n" + b"".join(lines[1:]))

    assert main(["--json", "log"]) == 70
    captured = capsys.readouterr()

    assert captured.out == ""
    error = _json(captured.err.strip().splitlines()[-1])["error"]
    assert error["code"] == "GINN-E-INTERNAL"
    assert error["exit_code"] == 70
    assert error["details"]["line"] == 2


@pytest.mark.fr("FR-125")
def test_the_log_command_only_reads(log: OwnedPaths, capsys: pytest.CaptureFixture[str]) -> None:
    before = sorted(p.relative_to(log.state) for p in log.state.rglob("*"))
    content = log.audit_log.read_bytes()

    assert main(["log", "arch", "--since", "2026-10-01T00:00:00Z"]) == 0
    capsys.readouterr()

    assert log.audit_log.read_bytes() == content
    assert sorted(p.relative_to(log.state) for p in log.state.rglob("*")) == before
