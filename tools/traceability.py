#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Traceability for GINN-SRS-001: parse the SRS, collect `fr` markers, check and render.

The SRS supplies the requirements: FR headings (`#### FR-xxx · Title · Priority`) and NFR table
rows (`| NFR-xxx | Requirement | ... | Priority |`, priority in the last cell). Any other ID
that starts a table row (DR, CR, AS, IR, DEP and so on) is a valid marker target with no priority.
A mixed priority such as "Must (arch, debian, fedora) / Should (kali)" counts as Must.

Tests name the requirement they prove with `@pytest.mark.fr("ID")`, on a test function, on a
test class (applied to its test methods) or in a module-level `pytestmark`. Only functions named
`test*` and classes named `Test*` count as tests.

Modes:
  --write (default)  render docs/traceability.md
  --check            exit 1 on: a marker ID the SRS does not define; a Must FR or NFR with no
                     test and no entry in docs/traceability-deferred.toml; a deferred entry that
                     has a test, lacks a milestone or names an ID that is not a Must FR or NFR;
                     a docs/traceability.md that is not the current render
  --root DIR         repository root (default: the parent of tools/)

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


class Model(NamedTuple):
    reqs: dict[str, Requirement]
    markers: dict[str, list[str]]
    deferred: dict[str, str]


class SrsError(ValueError):
    """The SRS does not have the heading or table shape this tool reads."""


class MarkerError(ValueError):
    """A `fr` marker is not called with exactly one string literal."""


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
    """Return (ID, title) for non-FR/NFR IDs that a prose line defines."""
    lead = PROSE_LEAD.match(line)
    if lead is not None:
        return [(lead.group(1), lead.group(2).strip())]
    bold = PROSE_BOLD.match(line)
    if bold is None:
        return []
    label = bold.group(1)
    return [(m.group(1), label.strip()) for m in PROSE_IN_LABEL.finditer(label)][:1]


def parse_srs(path: Path) -> dict[str, Requirement]:
    """Return every requirement ID the SRS defines, keyed by ID.

    FR comes from `####` headings and NFR from table rows, each defined once. Other IDs
    (DR, CR, AS, IR, DEP and so on) come from table first cells or prose definitions, have no
    priority and may be mentioned more than once.
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


def _is_fr_mark(node: ast.expr) -> ast.Call | None:
    """Return the call if `node` is `pytest.mark.fr(...)`, else None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "fr"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "mark"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "pytest"
    ):
        return node
    return None


def _fr_ids(exprs: Iterable[ast.expr], rel: str) -> list[str]:
    ids: list[str] = []
    for expr in exprs:
        call = _is_fr_mark(expr)
        if call is None:
            continue
        arg = call.args[0] if len(call.args) == 1 and not call.keywords else None
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            raise MarkerError(f"{rel}:{expr.lineno}: pytest.mark.fr takes one string literal ID")
        ids.append(arg.value)
    return ids


def _module_marks(tree: ast.Module, rel: str) -> list[str]:
    ids: list[str] = []
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
        ids.extend(_fr_ids(items, rel))
    return ids


def _tests(body: list[ast.stmt]) -> Iterator[tuple[str, list[ast.expr]]]:
    """Yield (name, decorators) for each test, with the decorators of enclosing classes."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                yield node.name, node.decorator_list
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for name, decorators in _tests(node.body):
                yield f"{node.name}::{name}", [*node.decorator_list, *decorators]


def collect_markers(tests_dir: Path) -> dict[str, list[str]]:
    """Map each requirement ID to the sorted `path::test` items that carry its marker."""
    root = tests_dir.parent
    found: dict[str, set[str]] = {}
    for path in sorted(tests_dir.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        module_ids = _module_marks(tree, rel)
        for name, decorators in _tests(tree.body):
            for req_id in {*module_ids, *_fr_ids(decorators, rel)}:
                found.setdefault(req_id, set()).add(f"{rel}::{name}")
    return {req_id: sorted(items) for req_id, items in sorted(found.items())}


def _is_must(req: Requirement | None) -> bool:
    return (
        req is not None and req.priority == "Must" and req.id.startswith(TRACKED_PREFIXES)
    )


def load_deferred(path: Path, reqs: dict[str, Requirement]) -> tuple[dict[str, str], list[str]]:
    """Return {requirement ID: milestone} from the deferred file, and its structural problems."""
    if not path.exists():
        return {}, []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries: object = data.get("deferred", [])
    if not isinstance(entries, list):
        return {}, [f"{DEFERRED_NAME}: 'deferred' must be a list of [[deferred]] tables"]
    errors: list[str] = []
    deferred: dict[str, str] = {}
    for number, entry in enumerate(entries, start=1):
        label = f"{DEFERRED_NAME} entry {number}"
        if not isinstance(entry, dict):
            errors.append(f"{label}: not a [[deferred]] table")
            continue
        req_id = entry.get("id")
        milestone = entry.get("milestone")
        if not isinstance(req_id, str) or not _is_must(reqs.get(req_id)):
            errors.append(f"{label}: {req_id!r} is not a Must FR or NFR in {SRS_NAME}")
        if not isinstance(milestone, str) or MILESTONE.fullmatch(milestone) is None:
            errors.append(f"{label} ({req_id}): milestone is missing or not of the form M<n>")
        if not isinstance(req_id, str) or not isinstance(milestone, str):
            continue
        if req_id in deferred:
            errors.append(f"{label}: {req_id} is listed more than once")
        deferred[req_id] = milestone
    return deferred, errors


def load(root: Path) -> tuple[Model, list[str]]:
    """Read the SRS, the markers and the deferred file. Return the model and structural errors."""
    reqs = parse_srs(root / SRS_NAME)
    markers = collect_markers(root / TESTS_DIR)
    deferred, errors = load_deferred(root / DEFERRED_NAME, reqs)
    for req_id, items in markers.items():
        if req_id not in reqs:
            errors.append(f"marker {req_id} is not defined in {SRS_NAME}: {', '.join(items)}")
    return Model(reqs, markers, deferred), errors


def _status(req_id: str, model: Model) -> str:
    if model.markers.get(req_id):
        return "covered"
    milestone = model.deferred.get(req_id)
    if milestone:
        return f"deferred ({milestone})"
    return "MISSING"


def _counts(model: Model) -> tuple[int, int, int, int]:
    """Return (Must total, covered, deferred, missing) over Must FR and NFR."""
    statuses = [_status(r.id, model) for r in model.reqs.values() if _is_must(r)]
    covered = statuses.count("covered")
    missing = statuses.count("MISSING")
    return len(statuses), covered, len(statuses) - covered - missing, missing


def _tests_cell(items: list[str]) -> str:
    if not items:
        return "0"
    per_file = Counter(item.split("::", 1)[0] for item in items)
    return ", ".join(f"`{path}` ({count})" for path, count in sorted(per_file.items()))


def render(model: Model) -> str:
    """Return the text of docs/traceability.md. Deterministic: no timestamps, sorted by ID."""
    total, covered, deferred, missing = _counts(model)
    lines = [
        "<!-- Generated by tools/traceability.py --write. Do not edit by hand. -->",
        "",
        "# Traceability",
        "",
        "Every FR and NFR in `GINN-SRS-001-v1.0.md`, the tests whose `fr` marker names it, and its "
        "status (NFR-061, Appendix C).",
        "",
        "- `covered`: at least one test carries the marker.",
        "- `deferred (Mx)`: no test yet; `docs/traceability-deferred.toml` names the milestone.",
        "- `MISSING`: no test and not deferred. Must items with this status fail `--check`.",
        "",
        f"Must requirements: {total}. Covered: {covered}. Deferred: {deferred}. "
        f"Missing: {missing}.",
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
        milestone = model.deferred.get(req.id)
        if has_test and milestone:
            errors.append(
                f"{req.id} has tests but is still deferred for {milestone}: "
                f"remove its entry from {DEFERRED_NAME}"
            )
        elif not has_test and not milestone:
            errors.append(f"{req.id} is Must but has no test and is not in {DEFERRED_NAME}")
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
            total, covered, deferred, missing = _counts(model)
            print(
                f"traceability: ok. Must: {total}, covered: {covered}, "
                f"deferred: {deferred}, missing: {missing}"
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
