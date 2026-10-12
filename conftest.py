# SPDX-License-Identifier: Apache-2.0
"""Root pytest configuration: SRS marker registration and import path."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"

if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_configure(config):
    config.addinivalue_line("markers", "fr(id): SRS requirement id")
