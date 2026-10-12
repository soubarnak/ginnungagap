# SPDX-License-Identifier: Apache-2.0
"""Error catalogue checks against GINN-SRS-001 and the generated docs/errors.md (NFR-040)."""

import importlib.util
import re
from pathlib import Path

import pytest

from ginn.errors import CATALOGUE, make

REPO = Path(__file__).resolve().parents[2]
SRS = REPO / "GINN-SRS-001-v1.0.md"
GENERATOR = REPO / "tools" / "gen_errors_doc.py"
CODE_PATTERN = re.compile(r"GINN-[EW]-[A-Z0-9-]+")


@pytest.mark.fr("NFR-040")
def test_every_srs_code_is_in_catalogue() -> None:
    codes = set(CODE_PATTERN.findall(SRS.read_text(encoding="utf-8")))
    assert codes, "no GINN-E-* or GINN-W-* codes found in the SRS"
    missing = sorted(codes - set(CATALOGUE))
    assert not missing, f"SRS codes missing from the catalogue: {missing}"


@pytest.mark.fr("NFR-040")
def test_every_entry_has_summary_remedy_and_source() -> None:
    for spec in CATALOGUE.values():
        assert spec.summary.strip(), spec.code
        assert spec.remedy.strip(), spec.code
        assert spec.source.strip(), spec.code
        assert "\n" not in spec.remedy, f"{spec.code}: remedy must be one line"


@pytest.mark.fr("NFR-040")
def test_warnings_exit_zero_and_errors_exit_non_zero() -> None:
    for spec in CATALOGUE.values():
        if spec.code.startswith("GINN-W-"):
            assert spec.exit_code == 0, spec.code
        else:
            assert spec.exit_code != 0, spec.code


@pytest.mark.fr("NFR-040")
def test_make_fills_placeholders() -> None:
    err = make("GINN-E-NAME-TAKEN", name="arch")
    assert err.code == "GINN-E-NAME-TAKEN"
    assert err.message == "realm name 'arch' is already taken"
    assert err.exit_code == 2


@pytest.mark.fr("NFR-040")
def test_generated_errors_doc_is_current() -> None:
    spec = importlib.util.spec_from_file_location("gen_errors_doc", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--check"]) == 0
