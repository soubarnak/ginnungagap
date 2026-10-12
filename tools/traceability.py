#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Traceability for GINN-SRS-001: parse the SRS, collect `fr` markers, check and render.

The SRS supplies the requirements: FR headings (`#### FR-xxx · Title · Priority`) and NFR table
rows (`| NFR-xxx | Requirement | ... | Priority |`, priority in the last cell). The other ID
families (DR, IR, CR, AS, DEP) are valid marker targets with no priority; their IDs come from
table first cells or prose definitions. A mixed priority such as "Must (arch, debian, fedora) /
Should (kali)" counts as Must. Prefixes outside ID_FAMILIES (Q-, R-, OOS-, REF-) are not IDs here.

Tests name the requirement they prove with `@pytest.mark.fr("ID")`, on a test function, on a
test class (applied to its test methods), in a module-level `pytestmark`, or on a
`pytest.param(..., marks=...)` case. Only functions named `test*` and classes named `Test*` count
as tests. Every test must carry at least one `fr` marker (AGENTS.md).

Coverage: a marker counts only when its test can run. An unconditional `pytest.mark.skip`, or an
`xfail` with no condition, on the test, its class, the module or a param case removes that case's
markers from coverage. `skipif` and an `xfail` with a condition still count. A marked test that
does not count leaves its requirement MISSING or deferred.

AST limits (best effort): the scan follows `pytest.mark.<name>`, any
`import pytest as <alias>`, and `from pytest import mark [as <alias>]` and `param`. Aliases apply to
the whole module, without scope. It does not see marks added by hooks or `request.applymarker`,
marks held in variables, computed parameter lists, or `pytest.skip()` called inside a body. A
test whose marks the scan cannot see is reported as unmarked, so `--check` fails.

Modes:
  --write (default)  render docs/traceability.md
  --check            exit 1 on: a marker ID outside the ID families or not defined by the SRS;
                     a test with no fr marker; a Must FR or NFR with no counted test and no entry
                     in docs/traceability-deferred.toml; a deferred entry that has a counted test
                     unless it sets `partial = true`; a `partial = true` entry with no counted
                     test; a deferred entry that lacks a milestone, has a non-boolean `partial`,
                     or names an ID that is not a Must FR or NFR; a docs/traceability.md that is
                     not the current render
  --root DIR         repository root (default: the parent of tools/)

A deferred entry with `partial = true` is an ID whose unit-level tests can run (their markers
count) while its host-only or end-to-end acceptance proof is still deferred. Its status in
docs/traceability.md is `partial (Mx)`, and it is also counted in the deferred total.

Run as `python3 -I tools/traceability.py`. Standard library only.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import NamedTuple

SRS_NAME = "GINN-SRS-001-v1.0.md"
TESTS_DIR = "tests"
DEFERRED_NAME = "docs/traceability-deferred.toml"
DOC_NAME = "docs/traceability.md"
REPO_ROOT = Path(__file__).resolve().parents[1]

ID_FAMILIES = ("FR", "NFR", "NFR-SEC", "DR", "IR", "CR", "AS", "DEP")
ID_SHAPE = re.compile(r"^([A-Z]+(?:-[A-Z]+)?)-\d+$")

FR_HEADING = re.compile(r"^#### (FR-\d{3}) · (.+?) · (.+?)\s*$")
TABLE_ID = re.compile(r"^([A-Z]+(?:-[A-Z]+)?-\d+)$")
# Non-FR/NFR IDs defined in prose: a line that starts with "ID:", or a line that starts with a
# bold label naming the ID, such as "**Realm state model (DR-020)**".
PROSE_ID = r"((?!FR-|NFR-)[A-Z]+(?:-[A-Z]+)?-\d{3})"
PROSE_LEAD = re.compile(rf"^{PROSE_ID}:\s*(.*)$")
PROSE_BOLD = re.compile(r"^\*\*(.+?)\*\*")
PROSE_IN_LABEL = re.compile(rf"\b{PROSE_ID}\b")
MILESTONE = re.compile(r"M\d+")
TRACKED_PREFIXES = ("FR-", "NFR-")


class Requirement(NamedTuple):
    id: str
    title: str
    priority: str | None


class Deferral(NamedTuple):
    """A deferred entry: the milestone that owns the proof, and whether some tests already exist."""

    milestone: str
    partial: bool


class Model(NamedTuple):
    reqs: dict[str, Requirement]
    markers: dict[str, list[str]]
    deferred: dict[str, Deferral]
    unmarked: list[str]


class Counts(NamedTuple):
    """Must FR and NFR by status. `deferred` includes `partial`, which is a subset of it."""

    total: int
    covered: int
    partial: int
    deferred: int
    missing: int


class Scan(NamedTuple):
    """What the test modules say. `markers` counts only tests that can run."""

    markers: dict[str, list[str]]  # ID -> sorted `path::test` items whose marker counts
    all_markers: dict[str, list[str]]  # ID -> every item that names it, counted or not
    unmarked: list[str]  # test items that carry no fr marker at all


class SrsError(ValueError):
    """The SRS does not have the heading or table shape this tool reads."""


class MarkerError(ValueError):
    """A `fr` marker is not called with exactly one string literal."""


def family_of(req_id: str) -> str | None:
    """Return the ID family of `req_id` when it is one of ID_FAMILIES, else None."""
    shape = ID_SHAPE.match(req_id)
    if shape is None or shape.group(1) not in ID_FAMILIES:
        return None
    return shape.group(1)


def id_problem(req_id: str, reqs: dict[str, Requirement]) -> str | None:
    """Return why `req_id` cannot be a marker target, or None when the SRS defines it."""
    if family_of(req_id) is None:
        return f"{req_id} is not an ID of a family ({', '.join(ID_FAMILIES)})"
    if req_id not in reqs:
        return f"{req_id} is not defined in {SRS_NAME}"
    return None


def _priority(text: str, where: str) -> str:
    if "Must" in text:
        return "Must"
    if "Should" in text:
        return "Should"
    raise SrsError(f"{where}: priority must name Must or Should, got {text!r}")


def _cells(line: str) -> list[str]:
    inner = line.strip()[1:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", inner)]


def _add(reqs: dict[str, Requirement], req: Requirement, where: str) -> None:
    """Add an FR or NFR. Each must be defined once: a duplicate is an SRS error."""
    if req.id in reqs:
        raise SrsError(f"{where}: {req.id} is defined more than once")
    reqs[req.id] = req


def _prose_definitions(line: str) -> list[tuple[str, str]]:
    """Return (ID, title) for in-family, non-FR/NFR IDs that a prose line defines."""
    lead = PROSE_LEAD.match(line)
    if lead is not None:
        found = [(lead.group(1), lead.group(2).strip())]
    else:
        bold = PROSE_BOLD.match(line)
        if bold is None:
            return []
        label = bold.group(1)
        found = [
            (m.group(1), label.strip())
            for m in PROSE_IN_LABEL.finditer(label)
            if family_of(m.group(1)) is not None
        ][:1]
    return [(req_id, title) for req_id, title in found if family_of(req_id) is not None]


def parse_srs(path: Path) -> dict[str, Requirement]:
    """Return every requirement ID the SRS defines, keyed by ID.

    FR comes from `####` headings and NFR from table rows, each defined once. Other IDs of the
    ID_FAMILIES (DR, IR, CR, AS, DEP) come from table first cells or prose definitions, have no
    priority and may be mentioned more than once. IDs outside ID_FAMILIES are ignored.
    """
    reqs: dict[str, Requirement] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        where = f"{path.name}:{lineno}"
        if line.startswith("#### FR-"):
            match = FR_HEADING.match(line)
            if match is None:
                raise SrsError(f"{where}: malformed FR heading {line!r}")
            req_id, title, priority = match.groups()
            _add(reqs, Requirement(req_id, title, _priority(priority, where)), where)
        elif line.startswith("|") and line.rstrip().endswith("|"):
            cells = _cells(line)
            if not cells or TABLE_ID.match(cells[0]) is None:
                continue
            req_id = cells[0]
            if req_id.startswith("FR-"):
                continue  # Appendix C lists FR IDs with empty cells; headings define FR
            if family_of(req_id) is None:
                continue
            title = cells[1] if len(cells) > 1 else ""
            if req_id.startswith("NFR-"):
                if len(cells) < 3:
                    raise SrsError(f"{where}: NFR row {req_id} has no priority cell")
                _add(reqs, Requirement(req_id, title, _priority(cells[-1], where)), where)
            else:
                reqs.setdefault(req_id, Requirement(req_id, title, None))
        else:
            for req_id, title in _prose_definitions(line):
                reqs.setdefault(req_id, Requirement(req_id, title, None))
    return reqs


class _Names(NamedTuple):
    """Names that a test module binds to pytest, pytest.mark and pytest.param (best effort)."""

    pytest: frozenset[str]
    mark: frozenset[str]
    param: frozenset[str]


class _Use(NamedTuple):
    """A mark applied to a test: `pytest.mark.<name>`, used bare or called."""

    name: str
    args: list[ast.expr]
    keywords: dict[str, ast.expr]
    lineno: int


def _names(tree: ast.Module) -> _Names:
    pytest_names = {"pytest"}
    mark_names: set[str] = set()
    param_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    pytest_names.add(alias.asname or "pytest")
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "mark":
                    mark_names.add(bound)
                elif alias.name == "param":
                    param_names.add(bound)
    return _Names(frozenset(pytest_names), frozenset(mark_names), frozenset(param_names))


def _keywords(call: ast.Call) -> dict[str, ast.expr]:
    found: dict[str, ast.expr] = {}
    for keyword in call.keywords:
        name = keyword.arg
        if name is not None:
            found[name] = keyword.value
    return found


def _use(node: ast.expr, names: _Names) -> _Use | None:
    """Return the mark that `node` applies (`pytest.mark.x` or `pytest.mark.x(...)`), else None."""
    call = node if isinstance(node, ast.Call) else None
    func = call.func if call is not None else node
    if not isinstance(func, ast.Attribute):
        return None
    base = func.value
    via_pytest = (
        isinstance(base, ast.Attribute)
        and base.attr == "mark"
        and isinstance(base.value, ast.Name)
        and base.value.id in names.pytest
    )
    via_alias = isinstance(base, ast.Name) and base.id in names.mark
    if not (via_pytest or via_alias):
        return None
    args: list[ast.expr] = list(call.args) if call is not None else []
    keywords = _keywords(call) if call is not None else {}
    return _Use(func.attr, args, keywords, node.lineno)


def _uses(exprs: Iterable[ast.expr], names: _Names) -> list[_Use]:
    return [use for use in (_use(expr, names) for expr in exprs) if use is not None]


def _is_param(call: ast.Call, names: _Names) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in names.param
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "param"
        and isinstance(func.value, ast.Name)
        and func.value.id in names.pytest
    )


def _fr_ids(uses: Iterable[_Use], rel: str) -> list[str]:
    ids: list[str] = []
    for use in uses:
        if use.name != "fr":
            continue
        arg = use.args[0] if len(use.args) == 1 and not use.keywords else None
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            raise MarkerError(f"{rel}:{use.lineno}: pytest.mark.fr takes one string literal ID")
        ids.append(arg.value)
    return ids


def _disabled(uses: Iterable[_Use]) -> bool:
    """True when a mark stops the test proving anything: skip, or an xfail that always holds."""
    for use in uses:
        if use.name == "skip":
            return True
        if use.name == "xfail":
            condition = use.args[0] if use.args else use.keywords.get("condition")
            if condition is None:
                return True
            if isinstance(condition, ast.Constant) and condition.value is True:
                return True
    return False


def _module_uses(tree: ast.Module, names: _Names) -> list[_Use]:
    """Return the marks of a module-level `pytestmark` (single value or list or tuple)."""
    uses: list[_Use] = []
    for stmt in tree.body:
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            value = stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "pytestmark"
        ):
            value = stmt.value
        if value is None:
            continue
        items = list(value.elts) if isinstance(value, (ast.List, ast.Tuple)) else [value]
        uses.extend(_uses(items, names))
    return uses


def _param_cases(decorators: Iterable[ast.expr], names: _Names) -> list[list[_Use]]:
    """Return the marks of each `pytest.param(...)` case found in the decorators, one list each."""
    cases: list[list[_Use]] = []
    for decorator in decorators:
        for node in ast.walk(decorator):
            if not (isinstance(node, ast.Call) and _is_param(node, names)):
                continue
            marks = _keywords(node).get("marks")
            items: list[ast.expr]
            if marks is None:
                items = []
            elif isinstance(marks, (ast.List, ast.Tuple)):
                items = list(marks.elts)
            else:
                items = [marks]
            cases.append(_uses(items, names))
    return cases


def _tests(body: list[ast.stmt]) -> Iterator[tuple[str, list[ast.expr]]]:
    """Yield (name, decorators) for each test, with the decorators of enclosing classes."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                yield node.name, node.decorator_list
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for name, decorators in _tests(node.body):
                yield f"{node.name}::{name}", [*node.decorator_list, *decorators]


def scan_tests(tests_dir: Path) -> Scan:
    """Scan every test module under `tests_dir`. The module docstring says what counts."""
    root = tests_dir.parent
    counted: dict[str, set[str]] = {}
    mentioned: dict[str, set[str]] = {}
    unmarked: list[str] = []
    for path in sorted(tests_dir.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        names = _names(tree)
        module_uses = _module_uses(tree, names)
        module_ids = _fr_ids(module_uses, rel)
        for name, decorators in _tests(tree.body):
            item = f"{rel}::{name}"
            uses = _uses(decorators, names)
            decorator_ids = _fr_ids(uses, rel)
            cases = _param_cases(decorators, names)
            case_ids = [_fr_ids(case, rel) for case in cases]
            ids = {*module_ids, *decorator_ids, *(i for ids_ in case_ids for i in ids_)}
            for req_id in ids:
                mentioned.setdefault(req_id, set()).add(item)
            if not ids:
                unmarked.append(item)
            if _disabled([*module_uses, *uses]):
                continue
            live = {*module_ids, *decorator_ids}
            for case, ids_ in zip(cases, case_ids, strict=True):
                if not _disabled(case):
                    live.update(ids_)
            for req_id in live:
                counted.setdefault(req_id, set()).add(item)
    return Scan(
        {k: sorted(v) for k, v in sorted(counted.items())},
        {k: sorted(v) for k, v in sorted(mentioned.items())},
        unmarked,
    )


def collect_markers(tests_dir: Path) -> dict[str, list[str]]:
    """Map each requirement ID to the sorted `path::test` items whose marker counts."""
    return scan_tests(tests_dir).markers


def _is_must(req: Requirement | None) -> bool:
    return req is not None and req.priority == "Must" and req.id.startswith(TRACKED_PREFIXES)


def load_deferred(
    path: Path, reqs: dict[str, Requirement]
) -> tuple[dict[str, Deferral], list[str]]:
    """Return {requirement ID: Deferral} from the deferred file, and its structural problems."""
    if not path.exists():
        return {}, []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries: object = data.get("deferred", [])
    if not isinstance(entries, list):
        return {}, [f"{DEFERRED_NAME}: 'deferred' must be a list of [[deferred]] tables"]
    errors: list[str] = []
    deferred: dict[str, Deferral] = {}
    for number, entry in enumerate(entries, start=1):
        label = f"{DEFERRED_NAME} entry {number}"
        if not isinstance(entry, dict):
            errors.append(f"{label}: not a [[deferred]] table")
            continue
        req_id = entry.get("id")
        milestone = entry.get("milestone")
        partial = entry.get("partial", False)
        if not isinstance(req_id, str) or not _is_must(reqs.get(req_id)):
            errors.append(f"{label}: {req_id!r} is not a Must FR or NFR in {SRS_NAME}")
        if not isinstance(milestone, str) or MILESTONE.fullmatch(milestone) is None:
            errors.append(f"{label} ({req_id}): milestone is missing or not of the form M<n>")
        if not isinstance(partial, bool):
            errors.append(f"{label} ({req_id}): partial must be true or false")
        if not isinstance(req_id, str) or not isinstance(milestone, str):
            continue
        if req_id in deferred:
            errors.append(f"{label}: {req_id} is listed more than once")
        deferred[req_id] = Deferral(milestone, partial is True)
    return deferred, errors


def load(root: Path) -> tuple[Model, list[str]]:
    """Read the SRS, the markers and the deferred file. Return the model and structural errors."""
    reqs = parse_srs(root / SRS_NAME)
    scan = scan_tests(root / TESTS_DIR)
    deferred, errors = load_deferred(root / DEFERRED_NAME, reqs)
    for req_id, items in scan.all_markers.items():
        problem = id_problem(req_id, reqs)
        if problem is not None:
            errors.append(f"marker {problem}: {', '.join(items)}")
    return Model(reqs, scan.markers, deferred, scan.unmarked), errors


def _status(req_id: str, model: Model) -> str:
    deferral = model.deferred.get(req_id)
    if model.markers.get(req_id):
        if deferral is not None and deferral.partial:
            return f"partial ({deferral.milestone})"
        return "covered"
    if deferral is not None:
        return f"deferred ({deferral.milestone})"
    return "MISSING"


def _counts(model: Model) -> Counts:
    """Return the Must FR and NFR totals by status."""
    statuses = [_status(r.id, model) for r in model.reqs.values() if _is_must(r)]
    covered = statuses.count("covered")
    missing = statuses.count("MISSING")
    partial = sum(1 for status in statuses if status.startswith("partial"))
    return Counts(len(statuses), covered, partial, len(statuses) - covered - missing, missing)


def _tests_cell(items: list[str]) -> str:
    if not items:
        return "0"
    per_file = Counter(item.split("::", 1)[0] for item in items)
    return ", ".join(f"`{path}` ({count})" for path, count in sorted(per_file.items()))


def render(model: Model) -> str:
    """Return the text of docs/traceability.md. Deterministic: no timestamps, sorted by ID."""
    counts = _counts(model)
    lines = [
        "<!-- Generated by tools/traceability.py --write. Do not edit by hand. -->",
        "",
        "# Traceability",
        "",
        "Every FR and NFR in `GINN-SRS-001-v1.0.md`, the tests whose `fr` marker names it, and its "
        "status (NFR-061, Appendix C).",
        "",
        "- `covered`: at least one test that can run carries the marker.",
        "- `partial (Mx)`: such tests exist, but the acceptance proof that needs a host or an "
        "end-to-end run is still deferred to Mx (`partial = true` in "
        "`docs/traceability-deferred.toml`).",
        "- `deferred (Mx)`: no such test yet; `docs/traceability-deferred.toml` names the "
        "milestone.",
        "- `MISSING`: no such test and not deferred. Must items with this status fail `--check`.",
        "",
        f"Must requirements: {counts.total}. Covered: {counts.covered}. "
        f"Deferred: {counts.deferred} (partial: {counts.partial}). Missing: {counts.missing}.",
        "",
        "| ID | Title | Priority | Tests | Status |",
        "|---|---|---|---|---|",
    ]
    tracked = sorted(
        (r for r in model.reqs.values() if r.id.startswith(TRACKED_PREFIXES)),
        key=lambda r: r.id,
    )
    for req in tracked:
        title = req.title.replace("|", "\\|")
        tests = _tests_cell(model.markers.get(req.id, []))
        status = _status(req.id, model)
        lines.append(f"| {req.id} | {title} | {req.priority} | {tests} | {status} |")
    return "\n".join(lines) + "\n"


def check(root: Path) -> tuple[Model, list[str]]:
    """Return the model and every problem that makes `--check` fail."""
    model, errors = load(root)
    for req in model.reqs.values():
        if not _is_must(req):
            continue
        has_test = bool(model.markers.get(req.id))
        deferral = model.deferred.get(req.id)
        if deferral is None:
            if not has_test:
                errors.append(f"{req.id} is Must but has no test and is not in {DEFERRED_NAME}")
        elif has_test and not deferral.partial:
            errors.append(
                f"{req.id} has tests but is still deferred for {deferral.milestone}: "
                f"remove its entry from {DEFERRED_NAME}, or set partial = true if only part of "
                "its proof is deferred"
            )
        elif deferral.partial and not has_test:
            errors.append(
                f"{req.id} is marked partial but has no test: "
                f"remove partial = true from {DEFERRED_NAME}"
            )
    for item in model.unmarked:
        errors.append(f"{item} has no fr marker: name the requirement it proves (AGENTS.md)")
    doc = root / DOC_NAME
    current = doc.read_text(encoding="utf-8") if doc.exists() else None
    if current != render(model):
        errors.append(f"{DOC_NAME} is missing or out of date: run tools/traceability.py --write")
    return model, errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check and render the SRS traceability matrix (NFR-061, Appendix C)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 on any traceability gap")
    mode.add_argument("--write", action="store_true", help="render docs/traceability.md (default)")
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root (default: parent of tools/)"
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            model, errors = check(args.root)
            if errors:
                for error in errors:
                    print(f"error: {error}", file=sys.stderr)
                print(f"traceability: {len(errors)} problem(s)", file=sys.stderr)
                return 1
            counts = _counts(model)
            print(
                f"traceability: ok. Must: {counts.total}, covered: {counts.covered}, "
                f"deferred: {counts.deferred} (partial: {counts.partial}), "
                f"missing: {counts.missing}"
            )
            return 0
        model, errors = load(args.root)
        if errors:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
            return 1
        doc = args.root / DOC_NAME
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(render(model), encoding="utf-8")
        print(f"wrote {doc}")
        return 0
    except (OSError, SyntaxError, SrsError, MarkerError, tomllib.TOMLDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
