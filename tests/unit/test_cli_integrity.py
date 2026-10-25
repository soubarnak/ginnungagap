# SPDX-License-Identifier: Apache-2.0
"""`ginn integrity [--ack]` through the CLI entry point, under temporary roots (FR-006, IR-010).

The AC-2 analogue: an unchanged host prints "host unchanged since baseline" and exits 0. The host
root is passed as --root-dir. That option is hidden from --help, and the tests use it so that no
test reads the real /etc. The conftest pins every XDG variable and the runtime override to tmp_path.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest

import ginn.cli.main as cli_main
from ginn.__main__ import main
from ginn.cli import commands as registry
from ginn.cli.commands import CommandSpec, Context
from ginn.errors import GinnError
from ginn.integrity import state as state_module
from ginn.integrity.state import IntegrityState
from ginn.store.audit import AuditLog
from ginn.store.paths import OwnedPaths

SCOPE = ("etc", "var/db/xbps", "var/service", "usr/local", "opt")


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
def paths() -> OwnedPaths:
    return OwnedPaths.from_env(uid=os.getuid())


@pytest.fixture
def host(tmp_path: Path) -> Path:
    root = tmp_path / "host"
    for scope in SCOPE:
        (root / scope).mkdir(parents=True)
    (root / "etc" / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    return root


def _integrity(*args: str, root: Path) -> list[str]:
    return ["integrity", *args, "--root-dir", str(root)]


def _json(text: str) -> Any:
    return json.loads(text)


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    """Configure hook for a probe with no arguments."""


def _install_probe(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:
    """Register one `probe` command for this test only, as test_cli_smoke does."""
    probe = CommandSpec(
        name="probe",
        help="probe the CLI",
        example="ginn probe",
        configure=_no_arguments,
        run=run,
    )
    monkeypatch.setattr(registry, "COMMANDS", [probe])


@pytest.mark.fr("FR-006")
def test_the_first_run_creates_the_baseline_and_exits_zero(
    host: Path, paths: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_integrity(root=host)) == 0
    out = capsys.readouterr().out
    assert out.startswith("host integrity baseline created: ")
    assert (paths.integrity / "baseline.json").is_file()
    assert not (paths.integrity / "pending.json").exists()


@pytest.mark.fr("FR-006")
def test_an_unchanged_host_reports_host_unchanged_since_baseline_and_exits_zero(
    host: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    capsys.readouterr()

    assert main(_integrity(root=host)) == 0
    captured = capsys.readouterr()
    assert captured.out == "host unchanged since baseline\n"
    assert captured.err == ""


@pytest.mark.fr("FR-006")
def test_json_output_for_an_unchanged_host_has_an_empty_changed_list(
    host: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    capsys.readouterr()

    assert main(["--json", *_integrity(root=host)]) == 0
    payload = _json(capsys.readouterr().out)
    assert payload == {"changed": [], "pending": None, "status": "unchanged"}


@pytest.mark.fr("FR-006")
def test_a_changed_host_warns_exits_zero_and_must_be_acknowledged_before_operations(
    host: Path, paths: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")
    capsys.readouterr()

    assert main(_integrity(root=host)) == 0
    captured = capsys.readouterr()
    assert "GINN-W-HOST-CHANGED: host files changed since baseline: /etc/hosts" in captured.err
    assert "  fix: review the listed paths; the likely cause is xbps" in captured.err
    assert captured.out == (
        "host changed since baseline: 1 path(s) listed above; run 'ginn integrity --ack' after "
        "review\n"
    )

    assert [entry.op for entry in AuditLog(paths).read()] == ["integrity", "integrity"]
    with pytest.raises(GinnError) as refused:
        IntegrityState(paths, root_dir=host).require_acknowledged()
    assert refused.value.code == "GINN-E-HOST-UNACKED"


@pytest.mark.fr("FR-006")
def test_json_output_of_a_change_carries_the_warning_in_the_result(
    host: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["--json", *_integrity(root=host)]) == 0
    captured = capsys.readouterr()
    payload = _json(captured.out)
    assert payload["status"] == "changed"
    assert payload["changed"] == ["/etc/hosts"]
    assert payload["pending"] == ["/etc/hosts"]
    assert [w["code"] for w in payload["warnings"]] == ["GINN-W-HOST-CHANGED"]
    assert payload["warnings"][0]["message"].endswith("/etc/hosts")
    assert captured.err == ""


@pytest.mark.fr("FR-006")
def test_acknowledging_lists_every_accepted_path_and_clears_the_pending_change(
    host: Path, paths: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")
    (host / "etc" / "zz").write_text("z", encoding="utf-8")
    main(_integrity(root=host))
    capsys.readouterr()

    assert main(_integrity("--ack", root=host)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "host integrity acknowledged: baseline updated, 3 path(s) accepted"
    assert lines[1:] == ["  /etc", "  /etc/hosts", "  /etc/zz"]
    assert not (paths.integrity / "pending.json").exists()
    assert [entry.op for entry in AuditLog(paths).read()][-1] == "integrity-ack"

    assert main(_integrity(root=host)) == 0
    assert capsys.readouterr().out == "host unchanged since baseline\n"


@pytest.mark.fr("FR-006")
def test_an_ack_that_would_accept_an_unlisted_change_is_refused_with_its_paths(
    host: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")
    capsys.readouterr()

    assert main(_integrity("--ack", root=host)) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "GINN-E-HOST-UNACKED: host integrity warning not acknowledged; changed paths: " in (
        captured.err
    )
    assert "/etc/hosts" in captured.err
    assert "  fix: review the listed paths, then run 'ginn integrity --ack'" in captured.err

    assert main(_integrity("--ack", root=host)) == 0
    assert "  /etc/hosts" in capsys.readouterr().out


@pytest.mark.fr("FR-006")
def test_a_refused_operation_prints_the_changed_paths_in_text_mode(
    host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")

    def run_operation(args: argparse.Namespace, ctx: Context) -> int:
        paths = OwnedPaths.from_env()
        IntegrityState(paths, root_dir=host).before_op(AuditLog(paths), "tester")
        return 0

    _install_probe(monkeypatch, run_operation)
    capsys.readouterr()

    assert cli_main.run(["probe"]) == 4
    err = capsys.readouterr().err
    assert err.startswith("GINN-E-HOST-UNACKED: host integrity warning not acknowledged; ")
    assert "changed paths: /etc/hosts" in err


@pytest.mark.fr("FR-006")
def test_an_unchanged_host_with_a_pending_file_says_so_and_exits_zero(
    host: Path, paths: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    # A real change never returns to the baseline: a rewrite moves ctime, so the host cannot match
    # the stored stat records again. This state is written by hand to cover the report of a
    # pending file whose host shows no difference.
    main(_integrity(root=host))
    (paths.integrity / "pending.json").write_text(
        '{"detected_at": "2026-10-12T09:00:00.000Z", "paths": ["/etc/hosts"], "schema_version": 1}',
        encoding="utf-8",
    )
    capsys.readouterr()

    assert main(["--json", *_integrity(root=host)]) == 0
    payload = _json(capsys.readouterr().out)
    assert payload == {"changed": [], "pending": ["/etc/hosts"], "status": "unchanged"}

    assert main(_integrity(root=host)) == 0
    assert capsys.readouterr().out == (
        "host unchanged since baseline; an earlier change of 1 path(s) is still pending: run "
        "'ginn integrity --ack' after review\n"
    )


@pytest.mark.fr("FR-006")
def test_a_corrupt_baseline_stops_the_check_and_ack_recaptures_it_and_says_so(
    host: Path, paths: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    (paths.integrity / "baseline.json").write_text("{not json", encoding="utf-8")
    capsys.readouterr()

    assert main(_integrity(root=host)) == 70
    err = capsys.readouterr().err
    assert "GINN-E-INTERNAL" in err
    assert "  fix: review 'ginn integrity', then run 'ginn integrity --ack'" in err
    assert "baseline.json" in err

    assert main(_integrity("--ack", root=host)) == 0
    out = capsys.readouterr().out
    assert out.startswith("host integrity acknowledged: baseline updated, 0 path(s) accepted\n")
    assert "note: baseline.json discarded and rewritten: integrity state file " in out
    entry = list(AuditLog(paths).read())[-1]
    assert entry.op == "integrity-ack"
    assert entry.params["discarded"] == ["baseline.json"]

    assert main(_integrity(root=host)) == 0
    assert capsys.readouterr().out == "host unchanged since baseline\n"


@pytest.mark.fr("FR-006")
def test_a_corrupt_pending_file_does_not_trap_the_user_in_an_acknowledgement(
    host: Path, paths: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    (paths.integrity / "pending.json").write_text("[", encoding="utf-8")
    capsys.readouterr()

    assert main(_integrity("--ack", root=host)) == 0
    assert "note: pending.json discarded and rewritten: integrity state file " in (
        capsys.readouterr().out
    )
    assert not (paths.integrity / "pending.json").exists()


@pytest.mark.fr("FR-006")
def test_the_root_dir_option_is_hidden_from_help_and_the_ack_flag_is_shown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["integrity", "--help"]) == 0
    out = capsys.readouterr().out
    assert "Example: ginn integrity --ack" in out
    assert "--ack" in out
    assert "--root-dir" not in out


@pytest.mark.fr("FR-006")
def test_a_baseline_made_under_one_root_is_refused_under_another_with_a_remedy_naming_the_file(
    host: Path, paths: OwnedPaths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    other = tmp_path / "other"
    for scope in SCOPE:
        (other / scope).mkdir(parents=True)
    capsys.readouterr()

    assert main(_integrity(root=other)) == 2
    err = capsys.readouterr().err
    assert "GINN-E-USAGE" in err
    assert (
        f"  fix: run 'ginn integrity --ack' to accept this host under root {other} and rewrite "
        in err
    )
    assert str(paths.integrity / "baseline.json") in err


@pytest.mark.fr("FR-006")
def test_ack_under_another_root_discards_the_old_baseline_and_reports_why(
    host: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    other = tmp_path / "other"
    for scope in SCOPE:
        (other / scope).mkdir(parents=True)
    capsys.readouterr()

    assert main(_integrity("--ack", root=other)) == 0
    out = capsys.readouterr().out
    assert out.startswith("host integrity acknowledged: baseline updated, 0 path(s) accepted\n")
    assert "note: baseline.json discarded and rewritten: was made for root " in out
    assert main(_integrity(root=other)) == 0
    assert capsys.readouterr().out == "host unchanged since baseline\n"


@pytest.mark.fr("FR-006")
def test_a_pending_file_that_outlived_its_baseline_is_reported_by_the_created_branch(
    host: Path, paths: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")
    main(_integrity(root=host))
    (paths.integrity / "baseline.json").unlink()
    capsys.readouterr()

    assert main(["--json", *_integrity(root=host)]) == 0
    payload = _json(capsys.readouterr().out)
    assert payload == {"changed": [], "pending": ["/etc/hosts"], "status": "baseline-created"}

    assert main(_integrity("--ack", root=host)) == 0
    assert main(_integrity(root=host)) == 0
    assert capsys.readouterr().out.endswith("host unchanged since baseline\n")


@pytest.mark.fr("FR-006")
def test_the_created_text_says_a_pending_change_outlived_the_baseline(
    host: Path, paths: OwnedPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")
    main(_integrity(root=host))
    (paths.integrity / "baseline.json").unlink()
    capsys.readouterr()

    assert main(_integrity(root=host)) == 0
    assert capsys.readouterr().out == (
        "host integrity baseline created: 6 path(s) fingerprinted; an earlier change of 1 path(s) "
        "is still pending: run 'ginn integrity --ack' after review\n"
    )


@pytest.mark.fr("FR-006")
def test_a_check_reads_the_baseline_file_once(
    host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_integrity(root=host))
    reads: list[str] = []
    real_read = state_module.read_json

    def counting_read(path: Path, schema_version: int) -> dict[str, Any]:
        reads.append(path.name)
        return real_read(path, schema_version)

    monkeypatch.setattr(state_module, "read_json", counting_read)
    capsys.readouterr()

    assert main(_integrity(root=host)) == 0
    assert reads.count("baseline.json") == 1
