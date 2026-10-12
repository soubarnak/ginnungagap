# SPDX-License-Identifier: Apache-2.0
"""tools/traceability.py: SRS parsing, marker collection, the deferred list and --check (T0.5).

Covers NFR-061 (every Must FR has an automated test; traceability table, Appendix C).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "traceability.py"
SRS = REPO / "GINN-SRS-001-v1.0.md"
SRS_NAME = "GINN-SRS-001-v1.0.md"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("traceability", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tr = _load_tool()

FR_RANGES = ((1, 7), (10, 27), (40, 49), (60, 69), (80, 85), (100, 107), (120, 125))
NFR_RANGES = ((1, 11), (20, 22), (30, 33), (40, 43), (50, 52), (60, 63))
EXPECTED_FR = {f"FR-{n:03d}" for lo, hi in FR_RANGES for n in range(lo, hi + 1)}
EXPECTED_NFR = {f"NFR-{n:03d}" for lo, hi in NFR_RANGES for n in range(lo, hi + 1)} | {
    f"NFR-SEC-{n:03d}" for n in range(1, 11)
}
EXPECTED_SHOULD_FR = {f"FR-{n:03d}" for n in (3, 14, 18, 24, 27, 47, 68, 84, 85, 104, 107, 125)}
EXPECTED_SHOULD_NFR = {"NFR-020", "NFR-021", "NFR-022", "NFR-032", "NFR-042", "NFR-050", "NFR-051"}

MINI_SRS = """\
# Mini SRS

#### FR-001 · First · Must

Body.

#### FR-002 · Second · Should

| ID | Requirement | Verification | Priority |
|---|---|---|---|
| NFR-001 | Thing | Check | Must |
| AS-001 | Assumption | Accepted |
"""

DECORATED = """\
import pytest


@pytest.mark.fr("FR-001")
def test_single() -> None:
    pass


@pytest.mark.fr("FR-002")
@pytest.mark.fr("NFR-001")
def test_stacked() -> None:
    pass


@pytest.mark.fr("FR-003")
class TestGroup:
    def test_method(self) -> None:
        pass

    def helper(self) -> None:
        pass


@pytest.mark.fr("FR-004")
def helper_not_a_test() -> None:
    pass
"""

MODULE_LIST = """\
import pytest

pytestmark = [pytest.mark.fr("DR-002"), pytest.mark.fr("DR-003")]


def test_inherits() -> None:
    pass
"""

MODULE_SINGLE = """\
import pytest

pytestmark = pytest.mark.fr("CR-004")


def test_inherits_single() -> None:
    pass
"""

NON_LITERAL = """\
import pytest

REQ = "FR-001"


@pytest.mark.fr(REQ)
def test_bad() -> None:
    pass
"""

COVERED = """\
import pytest


@pytest.mark.fr("FR-001")
def test_first() -> None:
    pass


@pytest.mark.fr("NFR-001")
def test_thing() -> None:
    pass
"""

NFR_ONLY = """\
import pytest


@pytest.mark.fr("NFR-001")
def test_thing() -> None:
    pass
"""

DEFERRED_FR001_WITH_TEST = """\
[[deferred]]
id = "FR-001"
milestone = "M3"
reason = "planned"
"""

DEFERRED_NO_MILESTONE = """\
[[deferred]]
id = "FR-001"
reason = "planned"
"""

DEFERRED_FR001_PARTIAL = """\
[[deferred]]
id = "FR-001"
milestone = "M3"
partial = true
reason = "unit tests run; the host run is deferred"
"""

DEFERRED_FR001_NOT_PARTIAL = """\
[[deferred]]
id = "FR-001"
milestone = "M3"
partial = false
reason = "planned"
"""

DEFERRED_PARTIAL_NOT_BOOLEAN = """\
[[deferred]]
id = "FR-001"
milestone = "M3"
partial = "yes"
reason = "planned"
"""


SKIPPED_AND_XFAIL = """\
import sys

import pytest


@pytest.mark.fr("FR-001")
@pytest.mark.skip(reason="not yet")
def test_skipped() -> None:
    pass


@pytest.mark.fr("NFR-001")
@pytest.mark.xfail(strict=True)
def test_expected_failure() -> None:
    pass


@pytest.mark.fr("CR-004")
@pytest.mark.skip
def test_bare_skip() -> None:
    pass


@pytest.mark.fr("FR-002")
@pytest.mark.skipif(sys.platform == "linux", reason="linux only")
def test_skipif_counts() -> None:
    pass


@pytest.mark.fr("DR-002")
@pytest.mark.xfail(sys.platform == "linux", strict=True)
def test_conditional_xfail_counts() -> None:
    pass
"""

DISABLED_MODULE = """\
import pytest

pytestmark = pytest.mark.skip(reason="module off")


@pytest.mark.fr("FR-001")
def test_module_skipped() -> None:
    pass
"""

DISABLED_CLASS = """\
import pytest


@pytest.mark.skip(reason="class off")
class TestOff:
    @pytest.mark.fr("FR-002")
    def test_method(self) -> None:
        pass
"""

PARAM_CASES = """\
import pytest


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1, marks=pytest.mark.fr("FR-001")),
        pytest.param(2, marks=[pytest.mark.fr("NFR-001")]),
        pytest.param(3, marks=[pytest.mark.skip, pytest.mark.fr("DR-002")]),
    ],
)
def test_cases(value: int) -> None:
    pass
"""

MARK_ALIASES = """\
import pytest as pt
from pytest import mark
from pytest import mark as m


@mark.fr("FR-001")
def test_from_import() -> None:
    pass


@m.fr("NFR-001")
def test_renamed_import() -> None:
    pass


@pt.mark.fr("DR-002")
def test_module_alias() -> None:
    pass
"""

UNMARKED = """\
import pytest


@pytest.mark.fr("FR-001")
def test_marked() -> None:
    pass


def test_bare() -> None:
    pass


class TestBare:
    @pytest.mark.fr("NFR-001")
    def test_marked_method(self) -> None:
        pass

    def test_unmarked_method(self) -> None:
        pass

    def helper(self) -> None:
        pass
"""

UNKNOWN_IDS = """\
import pytest


@pytest.mark.fr("REF-1")
def test_reference() -> None:
    pass


@pytest.mark.fr("Q-001")
@pytest.mark.skip(reason="off")
def test_skipped_unknown() -> None:
    pass
"""

SRS_OUTSIDE_FAMILIES = """\
#### FR-001 · First · Must

| ID | Requirement | Verification | Priority |
|---|---|---|---|
| NFR-001 | Thing | Check | Must |
| DR-020 | Realm state | Review |
| Q-001 | Open question | Decide |
| REF-1 | Reference | Read |
| OOS-3 | Out of scope | None |
| R-002 | Risk | Mitigate |

DR-021: a prose definition in the family.
Q-002: a prose line outside the families.
**Limit (OOS-4)** is out of scope.
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(root: Path, tests: str, deferred: str | None) -> Path:
    """Build a mini repository: a small SRS, one test module and an optional deferred file."""
    _write(root / SRS_NAME, MINI_SRS)
    _write(root / "tests" / "unit" / "test_mini.py", tests)
    if deferred is not None:
        _write(root / "docs" / "traceability-deferred.toml", deferred)
    return root


def _run(root: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    # Fixed argv list (interpreter, tool, flags); no shell.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(TOOL), *flags, "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.fr("NFR-061")
def test_srs_defines_exactly_the_expected_fr_and_nfr() -> None:
    reqs = tr.parse_srs(SRS)
    assert len(EXPECTED_FR) == 65
    assert {rid for rid in reqs if rid.startswith("FR-")} == EXPECTED_FR
    assert len(EXPECTED_NFR) == 39
    assert {rid for rid in reqs if rid.startswith("NFR-")} == EXPECTED_NFR


@pytest.mark.fr("NFR-061")
def test_must_and_should_priorities_follow_the_srs() -> None:
    reqs = tr.parse_srs(SRS)
    must_fr = {rid for rid, req in reqs.items() if rid.startswith("FR-") and req.priority == "Must"}
    must_nfr = {
        rid for rid, req in reqs.items() if rid.startswith("NFR-") and req.priority == "Must"
    }
    assert len(must_fr) == 53
    assert len(must_nfr) == 32
    assert must_fr == EXPECTED_FR - EXPECTED_SHOULD_FR
    assert must_nfr == EXPECTED_NFR - EXPECTED_SHOULD_NFR
    should = {rid for rid, req in reqs.items() if req.priority == "Should"}
    assert should == EXPECTED_SHOULD_FR | EXPECTED_SHOULD_NFR


@pytest.mark.fr("NFR-061")
def test_mixed_priority_is_parsed_as_must() -> None:
    reqs = tr.parse_srs(SRS)
    assert reqs["FR-010"] == tr.Requirement("FR-010", "Create realm", "Must")
    assert reqs["NFR-SEC-001"].priority == "Must"


@pytest.mark.fr("NFR-061")
def test_dr_cr_as_ir_dep_ids_are_valid_without_priority() -> None:
    reqs = tr.parse_srs(SRS)
    for rid in ("DR-020", "IR-010", "IR-011", "DEP-1", "AS-028", "CR-004"):
        assert rid in reqs
        assert reqs[rid].priority is None


@pytest.mark.fr("NFR-061")
def test_marker_decorators_stacked_and_on_classes(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "unit" / "test_decorated.py", DECORATED)
    markers = tr.collect_markers(tmp_path / "tests")
    rel = "tests/unit/test_decorated.py"
    assert markers["FR-001"] == [f"{rel}::test_single"]
    assert markers["FR-002"] == [f"{rel}::test_stacked"]
    assert markers["NFR-001"] == [f"{rel}::test_stacked"]
    assert markers["FR-003"] == [f"{rel}::TestGroup::test_method"]
    assert "FR-004" not in markers, "a helper that is not a test must not count"


@pytest.mark.fr("NFR-061")
def test_module_pytestmark_list_and_single_form(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "unit" / "test_module.py", MODULE_LIST)
    _write(tmp_path / "tests" / "unit" / "test_single.py", MODULE_SINGLE)
    markers = tr.collect_markers(tmp_path / "tests")
    assert markers["DR-002"] == ["tests/unit/test_module.py::test_inherits"]
    assert markers["DR-003"] == ["tests/unit/test_module.py::test_inherits"]
    assert markers["CR-004"] == ["tests/unit/test_single.py::test_inherits_single"]


@pytest.mark.fr("NFR-061")
def test_non_literal_marker_argument_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "unit" / "test_bad.py", NON_LITERAL)
    with pytest.raises(tr.MarkerError, match="string literal"):
        tr.collect_markers(tmp_path / "tests")


@pytest.mark.fr("NFR-061")
def test_check_passes_after_write_on_a_small_tree(tmp_path: Path) -> None:
    root = _tree(tmp_path, COVERED, deferred="")
    assert _run(root, "--write").returncode == 0
    result = _run(root, "--check")
    assert result.returncode == 0, result.stderr
    assert "traceability: ok" in result.stdout


@pytest.mark.fr("NFR-061")
def test_check_fails_when_a_deferred_id_gains_a_test(tmp_path: Path) -> None:
    root = _tree(tmp_path, COVERED, deferred=DEFERRED_FR001_WITH_TEST)
    assert _run(root, "--write").returncode == 0
    result = _run(root, "--check")
    assert result.returncode == 1
    assert "FR-001 has tests but is still deferred for M3" in result.stderr


@pytest.mark.fr("NFR-061")
def test_partial_deferred_entry_may_keep_its_tests(tmp_path: Path) -> None:
    root = _tree(tmp_path, COVERED, deferred=DEFERRED_FR001_PARTIAL)
    assert _run(root, "--write").returncode == 0
    result = _run(root, "--check")
    assert result.returncode == 0, result.stderr
    assert "traceability: ok" in result.stdout
    assert "(partial: 1)" in result.stdout
    rendered = (root / "docs" / "traceability.md").read_text(encoding="utf-8")
    assert "| FR-001 | First | Must | `tests/unit/test_mini.py` (1) | partial (M3) |" in rendered


@pytest.mark.fr("NFR-061")
def test_non_partial_deferred_entry_with_tests_fails_even_when_explicit(tmp_path: Path) -> None:
    root = _tree(tmp_path, COVERED, deferred=DEFERRED_FR001_NOT_PARTIAL)
    assert _run(root, "--write").returncode == 0
    result = _run(root, "--check")
    assert result.returncode == 1
    assert "FR-001 has tests but is still deferred for M3" in result.stderr


@pytest.mark.fr("NFR-061")
def test_partial_entry_without_a_test_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, NFR_ONLY, deferred=DEFERRED_FR001_PARTIAL)
    result = _run(root, "--check")
    assert result.returncode == 1
    assert "FR-001 is marked partial but has no test" in result.stderr


@pytest.mark.fr("NFR-061")
def test_partial_must_be_true_or_false(tmp_path: Path) -> None:
    root = _tree(tmp_path, COVERED, deferred=DEFERRED_PARTIAL_NOT_BOOLEAN)
    result = _run(root, "--check")
    assert result.returncode == 1
    assert "partial must be true or false" in result.stderr


@pytest.mark.fr("NFR-061")
def test_check_fails_when_a_deferred_entry_lacks_a_milestone(tmp_path: Path) -> None:
    root = _tree(tmp_path, NFR_ONLY, deferred=DEFERRED_NO_MILESTONE)
    result = _run(root, "--check")
    assert result.returncode == 1
    assert "milestone is missing or not of the form M<n>" in result.stderr


@pytest.mark.fr("NFR-061")
def test_check_fails_on_a_must_requirement_with_no_test(tmp_path: Path) -> None:
    root = _tree(tmp_path, NFR_ONLY, deferred="")
    result = _run(root, "--check")
    assert result.returncode == 1
    assert "FR-001 is Must but has no test and is not in" in result.stderr


@pytest.mark.fr("NFR-061")
def test_check_fails_on_a_marker_for_an_unknown_id(tmp_path: Path) -> None:
    ghost = '\n\n@pytest.mark.fr("FR-999")\ndef test_ghost() -> None:\n    pass\n'
    root = _tree(tmp_path, COVERED + ghost, deferred="")
    result = _run(root, "--check")
    assert result.returncode == 1
    assert "marker FR-999 is not defined" in result.stderr


@pytest.mark.fr("NFR-061")
def test_check_passes_when_the_doc_file_is_absent(tmp_path: Path) -> None:
    # docs/traceability.md is generated and not committed, so --check never needs it.
    root = _tree(tmp_path, COVERED, deferred="")
    result = _run(root, "--check")
    assert result.returncode == 0, result.stderr
    assert "traceability: ok" in result.stdout
    assert not (root / "docs" / "traceability.md").exists()


@pytest.mark.fr("NFR-061")
def test_check_passes_on_the_repository() -> None:
    # Default --root: the repository that contains this test.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(TOOL), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.fr("NFR-061")
def test_parse_srs_ignores_ids_outside_the_families(tmp_path: Path) -> None:
    path = tmp_path / SRS_NAME
    _write(path, SRS_OUTSIDE_FAMILIES)
    assert set(tr.parse_srs(path)) == {"FR-001", "NFR-001", "DR-020", "DR-021"}


@pytest.mark.fr("NFR-061")
def test_real_srs_does_not_define_q_r_oos_or_ref_ids() -> None:
    # These IDs occur in the real SRS, and the parser returned them before the family filter.
    reqs = tr.parse_srs(SRS)
    assert not {"Q-001", "R-002", "OOS-3", "REF-1"} & set(reqs)


@pytest.mark.fr("NFR-061")
def test_id_problem_names_the_family_or_the_missing_id() -> None:
    reqs = tr.parse_srs(SRS)
    assert tr.id_problem("FR-001", reqs) is None
    missing = tr.id_problem("FR-999", reqs)
    assert missing is not None
    assert "not defined" in missing
    outside = tr.id_problem("REF-1", reqs)
    assert outside is not None
    assert "not an ID of a family" in outside


@pytest.mark.fr("NFR-061")
def test_skip_and_unconditional_xfail_do_not_count(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "unit" / "test_skips.py", SKIPPED_AND_XFAIL)
    scan = tr.scan_tests(tmp_path / "tests")
    assert set(scan.markers) == {"FR-002", "DR-002"}
    assert set(scan.all_markers) == {"FR-001", "NFR-001", "CR-004", "FR-002", "DR-002"}
    assert scan.unmarked == []


@pytest.mark.fr("NFR-061")
def test_skip_on_a_module_or_a_class_disables_its_tests(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "unit" / "test_module_off.py", DISABLED_MODULE)
    _write(tmp_path / "tests" / "unit" / "test_class_off.py", DISABLED_CLASS)
    scan = tr.scan_tests(tmp_path / "tests")
    assert scan.markers == {}
    assert set(scan.all_markers) == {"FR-001", "FR-002"}


@pytest.mark.fr("NFR-061")
def test_param_case_marks_count_unless_that_case_is_skipped(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "unit" / "test_params.py", PARAM_CASES)
    scan = tr.scan_tests(tmp_path / "tests")
    item = "tests/unit/test_params.py::test_cases"
    assert scan.markers == {"FR-001": [item], "NFR-001": [item]}
    assert scan.all_markers["DR-002"] == [item]
    assert scan.unmarked == []


@pytest.mark.fr("NFR-061")
def test_from_pytest_import_mark_and_aliases_are_collected(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "unit" / "test_aliases.py", MARK_ALIASES)
    markers = tr.collect_markers(tmp_path / "tests")
    rel = "tests/unit/test_aliases.py"
    assert markers == {
        "DR-002": [f"{rel}::test_module_alias"],
        "FR-001": [f"{rel}::test_from_import"],
        "NFR-001": [f"{rel}::test_renamed_import"],
    }


@pytest.mark.fr("NFR-061")
def test_unmarked_test_is_a_check_error_but_write_still_renders(tmp_path: Path) -> None:
    root = _tree(tmp_path, UNMARKED, deferred="")
    assert _run(root, "--write").returncode == 0
    result = _run(root, "--check")
    assert result.returncode == 1
    prefix = "tests/unit/test_mini.py::"
    assert f"{prefix}test_bare has no fr marker" in result.stderr
    assert f"{prefix}TestBare::test_unmarked_method has no fr marker" in result.stderr
    assert "helper" not in result.stderr
    assert "test_marked_method has no fr marker" not in result.stderr


@pytest.mark.fr("NFR-061")
def test_unknown_or_outside_family_ids_fail_even_on_a_skipped_test(tmp_path: Path) -> None:
    root = _tree(tmp_path, UNKNOWN_IDS, deferred="")
    result = _run(root, "--check")
    assert result.returncode == 1
    assert "marker REF-1 is not an ID of a family" in result.stderr
    assert "marker Q-001 is not an ID of a family" in result.stderr
