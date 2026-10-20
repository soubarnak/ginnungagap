# SPDX-License-Identifier: Apache-2.0
"""Shared output of tools/measure (T10.4): one JSON line per NFR metric, with its verdict.

Each NFR script prints JSON lines with the keys nfr, metric, value, unit, target and verdict. The
verdict is PASS, FAIL or NOT RUN, the vocabulary of the host gate (tests/integration/conftest.py).
A value of null means NOT RUN. The exit status of a script is 0 when every line is PASS, 1 when
one line is FAIL, and 2 when one line is NOT RUN or the input is unusable.

The optional `note` key says what a verdict does not cover, such as a cycle count below the
SRS figure. The scripts never change a verdict to hide such a gap.

Run as `python3 -I tools/measure/emit.py <command> ...`, or import it from a script in this
directory. Standard library only.

Commands:
  gate   the last record of one item in a GINN_T49_RESULTS file (the T4.9 method)
  sweep  the median of one configuration of one S-05r sweep in runs.tsv (spikes/s05_idle_rss.sh)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

PASS = "PASS"  # noqa: S105 - a verdict name, not a password
FAIL = "FAIL"
NOT_RUN = "NOT RUN"

EXIT_CODES = {PASS: 0, FAIL: 1, NOT_RUN: 2}

SWEEP_RUNS_REQUIRED = 10
"""NFR-002 and NFR-003 take the median of 10 runs. Fewer counted runs is NOT RUN."""

KIB_PER_MIB = 1024


def verdict_at_most(value: float | None, limit: float) -> str:
    """PASS when `value` is at most `limit`, FAIL when it is above, NOT RUN when there is none."""
    if value is None:
        return NOT_RUN
    return PASS if value <= limit else FAIL


def line(
    nfr: str,
    metric: str,
    value: float | None,
    unit: str,
    target: str,
    verdict: str,
    note: str | None = None,
) -> dict[str, Any]:
    """One result, with the keys in the order the scripts print them."""
    entry: dict[str, Any] = {
        "nfr": nfr,
        "metric": metric,
        "value": None if value is None else round(value, 3),
        "unit": unit,
        "target": target,
        "verdict": verdict,
    }
    if note:
        entry["note"] = note
    return entry


def emit(entry: dict[str, Any]) -> int:
    """Print one result as a JSON line on stdout and return its exit status."""
    print(json.dumps(entry, ensure_ascii=False))
    return EXIT_CODES[entry["verdict"]]


def worst(codes: Iterable[int]) -> int:
    """The exit status of a script with several lines: FAIL (1) wins over NOT RUN (2)."""
    values = list(codes)
    if 1 in values:
        return 1
    if 2 in values:
        return 2
    return 0


def nearest_rank(values: Sequence[float], fraction: float) -> float:
    """The nearest-rank percentile, the method of S-02 and of the host gate (conftest.p95)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def p95_line(
    nfr: str,
    metric: str,
    values: Sequence[float],
    *,
    unit: str,
    target: str,
    limit: float,
    runs_required: int,
    timeouts: int = 0,
    note: str | None = None,
) -> dict[str, Any]:
    """The p95 (nearest rank) of timed runs against `limit`. A timed-out run counts at its cap.

    No values is NOT RUN. Fewer runs than the SRS asks for keep the verdict and add a note, because
    the p95 of a short series is still a measurement. A timeout is a lower bound, so it can only
    push the p95 up.
    """
    if not values:
        return line(nfr, metric, None, unit, target, NOT_RUN, note or "no timed run completed")
    p95 = nearest_rank(values, 0.95)
    text = f"{metric}, p95 of {len(values)} runs"
    parts = [f"median {statistics.median(values):.3f}, max {max(values):.3f} {unit}"]
    if len(values) < runs_required:
        parts.append(f"{len(values)} run(s) of the {runs_required} the SRS asks for")
    if timeouts:
        parts.append(f"{timeouts} run(s) timed out and count at the timeout")
    if note:
        parts.append(note)
    return line(nfr, text, p95, unit, target, verdict_at_most(p95, limit), "; ".join(parts))


def cap_at_not_run(entry: dict[str, Any], reason: str) -> dict[str, Any]:
    """A PASS that rests on an incomplete series is NOT RUN. A FAIL stays a FAIL.

    The value becomes null, as for every NOT RUN line, and the figure moves into the note.
    """
    if entry["verdict"] != PASS:
        return entry
    figure = f"{entry['metric']}: {entry['value']} {entry['unit']}"
    entry = dict(entry)
    entry["value"] = None
    entry["verdict"] = NOT_RUN
    entry["note"] = "; ".join(filter(None, [reason, f"not judged ({figure})", entry.get("note")]))
    return entry


def last_record(results: Path, item: str) -> dict[str, Any] | None:
    """The last record named `item` in a GINN_T49_RESULTS file, or None. Bad lines are skipped."""
    try:
        text = results.read_text(encoding="utf-8")
    except OSError:
        return None
    found: dict[str, Any] | None = None
    for raw in text.splitlines():
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("item") == item:
            found = record
    return found


def gate_line(
    results: Path,
    *,
    item: str,
    field: str,
    count_field: str | None,
    nfr: str,
    metric: str,
    unit: str,
    target: str,
    limit: float,
    note: str | None = None,
) -> dict[str, Any]:
    """The result of one host-gate measurement: `field` of the last record of `item`."""
    record = last_record(results, item)
    value: float | None = None
    if record is not None and field in record:
        value = float(record[field])
    if record is not None and count_field and count_field in record:
        metric = f"{metric}, n={record[count_field]}"
    return line(nfr, metric, value, unit, target, verdict_at_most(value, limit), note)


def sweep_values(runs: Path, sweep_id: str, cfg: str) -> list[float]:
    """Total RSS in MiB of every counted run (status ok) of one configuration of one sweep."""
    with runs.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        values: list[float] = []
        for row in reader:
            if row.get("sweep_id") != sweep_id or row.get("cfg") != cfg:
                continue
            if row.get("status") != "ok":
                continue
            try:
                values.append(int(row["total_rss_kib"]) / KIB_PER_MIB)
            except (KeyError, TypeError, ValueError):
                continue
    return values


def sweep_line(
    runs: Path,
    *,
    sweep_id: str,
    cfg: str,
    nfr: str,
    metric: str,
    target: str,
    limit_mib: float,
    note: str | None = None,
) -> dict[str, Any]:
    """The median total RSS of one configuration of an S-05r sweep, against the NFR limit."""
    if not sweep_id:
        return line(nfr, metric, None, "MiB", target, NOT_RUN, "no sweep id: run the spike first")
    try:
        values = sweep_values(runs, sweep_id, cfg)
    except OSError:
        return line(nfr, metric, None, "MiB", target, NOT_RUN, f"no runs file at {runs}")
    if len(values) < SWEEP_RUNS_REQUIRED:
        note_text = f"{len(values)} counted run(s) of {cfg}; {SWEEP_RUNS_REQUIRED} required"
        return line(nfr, metric, None, "MiB", target, NOT_RUN, note_text)
    median = statistics.median(values)
    text = f"{metric}; median of {len(values)} runs, min {min(values):.2f}, max {max(values):.2f}"
    return line(nfr, text, median, "MiB", target, verdict_at_most(median, limit_mib), note)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="emit.py", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    gate = commands.add_parser("gate", help="a result of a GINN_T49_RESULTS file")
    gate.add_argument("--results", type=Path, required=True)
    gate.add_argument("--item", required=True)
    gate.add_argument("--field", required=True)
    gate.add_argument("--count-field")
    gate.add_argument("--nfr", required=True)
    gate.add_argument("--metric", required=True)
    gate.add_argument("--unit", required=True)
    gate.add_argument("--target", required=True)
    gate.add_argument("--max", type=float, required=True)
    gate.add_argument("--note")

    sweep = commands.add_parser("sweep", help="a configuration of an S-05r sweep")
    sweep.add_argument("--runs", type=Path, required=True)
    sweep.add_argument("--sweep", default="")
    sweep.add_argument("--cfg", required=True)
    sweep.add_argument("--nfr", required=True)
    sweep.add_argument("--metric", required=True)
    sweep.add_argument("--target", required=True)
    sweep.add_argument("--max", type=float, required=True)
    sweep.add_argument("--note")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "gate":
        entry = gate_line(
            args.results,
            item=args.item,
            field=args.field,
            count_field=args.count_field,
            nfr=args.nfr,
            metric=args.metric,
            unit=args.unit,
            target=args.target,
            limit=args.max,
            note=args.note,
        )
    else:
        entry = sweep_line(
            args.runs,
            sweep_id=args.sweep,
            cfg=args.cfg,
            nfr=args.nfr,
            metric=args.metric,
            target=args.target,
            limit_mib=args.max,
            note=args.note,
        )
    return emit(entry)


if __name__ == "__main__":
    sys.exit(main())
