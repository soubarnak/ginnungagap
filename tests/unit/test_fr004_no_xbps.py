# SPDX-License-Identifier: Apache-2.0
"""FR-004: the xbps PATH shim is in force, fails when called, and no xbps-* call was recorded."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHIM_DIR = REPO / "tests" / "shims" / "xbps-fail"


@pytest.mark.fr("FR-004")
def test_xbps_install_resolves_to_the_shim() -> None:
    found = shutil.which("xbps-install")
    assert found is not None, "xbps-install is not on PATH; the shim is not installed"
    assert Path(found).resolve().parent == SHIM_DIR.resolve()


@pytest.mark.fr("FR-004")
def test_shim_fails_with_97_and_logs_one_line(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    env = {**os.environ, "GINN_XBPS_SHIM_LOG": str(log)}
    # Fixed argv list with an absolute path to the shim; no shell.
    result = subprocess.run(  # noqa: S603
        [str(SHIM_DIR / "xbps-install"), "-S", "ginn"],
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 97
    assert b"xbps shim: ginn must never invoke" in result.stderr
    assert b"(FR-004)" in result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == ["xbps-install -S ginn"]


@pytest.mark.fr("FR-004")
def test_session_log_is_empty(xbps_shim: Path) -> None:
    assert os.environ.get("GINN_XBPS_SHIM_LOG") == str(xbps_shim)
    assert xbps_shim.read_text(encoding="utf-8") == ""
