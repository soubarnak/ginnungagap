# SPDX-License-Identifier: Apache-2.0
"""FR-004: the xbps PATH shim is in force, fails when called, and logs each call to its own file.

The subprocess tests point GINN_XBPS_SHIM_LOG_DIR at a temporary directory, so the session log
directory stays empty and the session-finish check in conftest.py passes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHIM_DIR = REPO / "tests" / "shims" / "xbps-fail"
SHIM = SHIM_DIR / "xbps-install"
LOG_DIR_ENV = "GINN_XBPS_SHIM_LOG_DIR"
CALL_FILE = re.compile(r"[0-9]+-[0-9]+\.log")


def _run_shim(
    argv: list[str], env: dict[str, str], shim: Path = SHIM
) -> subprocess.CompletedProcess[bytes]:
    # Fixed argv list with an absolute path to the shim; no shell.
    return subprocess.run(  # noqa: S603
        [str(shim), *argv],
        capture_output=True,
        env=env,
        check=False,
    )


def _env_without_log_dir() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != LOG_DIR_ENV}


def _copy_shim(into: Path) -> Path:
    """Copy the shim into `into` so that its .logdir fallback can point elsewhere."""
    into.mkdir(parents=True)
    copy = into / "xbps-install"
    shutil.copy2(SHIM, copy)
    return copy


@pytest.mark.fr("FR-004")
def test_xbps_install_resolves_to_the_shim() -> None:
    found = shutil.which("xbps-install")
    assert found is not None, "xbps-install is not on PATH; the shim is not installed"
    assert Path(found).resolve().parent == SHIM_DIR.resolve()


@pytest.mark.fr("FR-004")
def test_shim_fails_with_97_and_logs_one_call(tmp_path: Path) -> None:
    log_dir = tmp_path / "calls"
    log_dir.mkdir()
    env = {**os.environ, LOG_DIR_ENV: str(log_dir)}
    result = _run_shim(["-S", "ginn"], env)
    assert result.returncode == 97
    assert b"xbps shim: ginn must never invoke" in result.stderr
    assert b"(FR-004)" in result.stderr
    (call,) = list(log_dir.iterdir())
    assert CALL_FILE.fullmatch(call.name), call.name
    assert call.read_text(encoding="utf-8") == "xbps-install -S ginn\n"


@pytest.mark.fr("FR-004")
def test_each_call_gets_its_own_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "calls"
    log_dir.mkdir()
    env = {**os.environ, LOG_DIR_ENV: str(log_dir)}
    _run_shim(["-S", "ginn"], env)
    _run_shim(["-Su"], env)
    contents = sorted(path.read_text(encoding="utf-8") for path in log_dir.iterdir())
    assert contents == ["xbps-install -S ginn\n", "xbps-install -Su\n"]


@pytest.mark.fr("FR-004")
def test_shim_falls_back_to_the_logdir_file(tmp_path: Path) -> None:
    shim = _copy_shim(tmp_path / "bin")
    log_dir = tmp_path / "calls"
    log_dir.mkdir()
    (tmp_path / "bin" / ".logdir").write_text(f"{log_dir}\n", encoding="utf-8")
    result = _run_shim(["-S", "ginn"], _env_without_log_dir(), shim)
    assert result.returncode == 97
    (call,) = list(log_dir.iterdir())
    assert call.read_text(encoding="utf-8") == "xbps-install -S ginn\n"


@pytest.mark.fr("FR-004")
def test_shim_still_fails_when_it_cannot_log(tmp_path: Path) -> None:
    shim = _copy_shim(tmp_path / "bin")
    result = _run_shim(["-S", "ginn"], _env_without_log_dir(), shim)
    assert result.returncode == 97
    assert list((tmp_path / "bin").iterdir()) == [shim]


@pytest.mark.fr("FR-004")
def test_session_log_directory_is_empty(xbps_shim: Path) -> None:
    assert os.environ.get(LOG_DIR_ENV) == str(xbps_shim)
    assert list(xbps_shim.iterdir()) == []
