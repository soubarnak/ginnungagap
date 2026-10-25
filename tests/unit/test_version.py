# SPDX-License-Identifier: Apache-2.0
"""The fallback version in ginn/__init__.py matches pyproject.toml (IR-011)."""

import tomllib
from pathlib import Path

import pytest

from ginn import _FALLBACK_VERSION

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


@pytest.mark.fr("IR-011")
def test_fallback_version_matches_pyproject() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert _FALLBACK_VERSION == data["project"]["version"]
