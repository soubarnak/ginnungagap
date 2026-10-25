# SPDX-License-Identifier: Apache-2.0
"""`ginn doctor` through the CLI, under a fake host tree: exit codes, output and refusals.

The host root is passed as --root-dir, which is hidden from --help. The conftest of the store tests
is not used here, so this module pins the XDG variables, HOME and PATH itself. The account and the
owner and free-space lookups are patched to the fake tree, as a test user cannot own root paths.
The podman check runs the replay shim, so no real podman runs. Exit 0 is success. Exit 1 is
GINN-E-DOCTOR-FAILED (FR-007 AC-1, AC-2). Exit 2 is GINN-E-USAGE, for a run as root.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import ginn.cli.commands.doctor as doctor_cmd
import ginn.cli.main as cli_main
from ginn.doctor import checks
from ginn.doctor.checks import Identity
from tests.fakes import doctor_host as fake
from tests.fakes.doctor_host import GIB, UID, USER


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    host_root = tmp_path / "root"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    for directory in (host_root, home, bin_dir):
        directory.mkdir()
    fake.build_host(host_root, home)
    fake.install_podman(bin_dir)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("GINN_RUNTIME_DIR", raising=False)
    identity = Identity(user=USER, uid=UID, gid=fake.GID, home=str(home))
    monkeypatch.setattr(doctor_cmd, "_identity", lambda: identity)
    monkeypatch.setattr(checks, "_lstat_owner", fake.owner_fake(host_root))
    monkeypatch.setattr(checks, "_disk_free", lambda _path: 50 * GIB)
    return host_root


def _run(root: Path, *extra: str) -> list[str]:
    argv = ["doctor", "--root-dir", str(root)]
    argv.extend(extra)
    return argv


@pytest.mark.fr("FR-007")
def test_help_shows_the_example_and_hides_root_dir(capsys: pytest.CaptureFixture[str]) -> None:
    status = cli_main.run(["doctor", "--help"])
    out = capsys.readouterr().out
    assert status == 0
    assert "Example: ginn doctor --json" in out
    assert "--json" in out
    assert "--root-dir" not in out


@pytest.mark.fr("FR-007")
def test_healthy_host_prints_the_report_and_exits_zero(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = cli_main.run(_run(root))
    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == ""
    assert captured.out.splitlines()[0] == f"ginn doctor for {USER} (uid {UID})"
    assert "summary: " in captured.out
    assert not any(line.startswith("FAIL") for line in captured.out.splitlines())


@pytest.mark.fr("FR-007")
def test_json_report_on_a_healthy_host_exits_zero(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = cli_main.run(_run(root, "--json"))
    captured = capsys.readouterr()
    assert status == 0
    obj = json.loads(captured.out)
    assert obj["schema"] == 1
    assert obj["ok"] is True
    assert len(obj["checks"]) == len(checks.CHECKS)
    assert captured.err == ""


@pytest.mark.fr("FR-007")
@pytest.mark.fr("NFR-041")
def test_failing_check_exits_one_with_the_fix_printed(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "etc/subuid").write_text("bob:524288:524288\n", encoding="utf-8")
    status = cli_main.run(_run(root))
    captured = capsys.readouterr()
    assert status == 1
    assert "FAIL  subids:" in captured.out
    assert f"  fix: sudo ginn host-setup --user {USER}" in captured.out
    assert f"GINN-E-DOCTOR-FAILED: 1 of {len(checks.CHECKS)} doctor checks failed" in captured.err


@pytest.mark.fr("FR-007")
def test_json_failure_keeps_the_error_object_last_on_stderr(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "etc/subuid").write_text("bob:524288:524288\n", encoding="utf-8")
    status = cli_main.run(_run(root, "--json"))
    captured = capsys.readouterr()
    assert status == 1
    assert json.loads(captured.out)["ok"] is False
    error_line = captured.err.strip().splitlines()[-1]
    error = json.loads(error_line)["error"]
    assert error["code"] == "GINN-E-DOCTOR-FAILED"
    assert error["exit_code"] == 1
    assert error["details"] == {"failed": 1, "total": len(checks.CHECKS)}


@pytest.mark.fr("FR-007")
def test_warnings_alone_exit_zero(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root_control = root / "sys/fs/cgroup/cgroup.subtree_control"
    root_control.write_text("cpuset cpu io memory\n", encoding="utf-8")
    status = cli_main.run(_run(root))
    captured = capsys.readouterr()
    assert status == 0
    assert "WARN  cgroup:" in captured.out


@pytest.mark.fr("FR-007")
def test_run_as_root_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        doctor_cmd, "_identity", lambda: Identity(user="root", uid=0, gid=0, home="/root")
    )
    status = cli_main.run(_run(root))
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert "GINN-E-USAGE" in captured.err


@pytest.mark.fr("FR-007")
def test_verbose_doctor_echoes_the_podman_argv(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = cli_main.run(["--verbose", *_run(root)])
    captured = capsys.readouterr()
    assert status == 0
    assert "--remote=false" in captured.err
    assert "info --format json" in captured.err


@pytest.mark.fr("FR-007")
def test_identity_takes_the_primary_group_from_the_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subordinate IDs and the chown use the account's primary group, not the process group."""
    account = SimpleNamespace(pw_name="carol", pw_gid=4242, pw_dir="/home/carol")
    monkeypatch.setattr(doctor_cmd.pwd, "getpwuid", lambda _uid: account)
    identity = doctor_cmd._identity()
    assert identity.user == "carol"
    assert identity.gid == 4242
    assert identity.home == "/home/carol"
