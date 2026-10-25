#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""NFR-011 (T10.4): the host integrity check costs at most 1 s per mutating operation.

One run is one operation as the integrity check sees it: a capture of the scope, one file
written inside the scope, a second capture, and the diff. The scope is the five roots of AS-020(b)
(ginn.integrity.fingerprint.SCOPE_ROOTS). The synthetic files sit under usr/local, one of those
roots, and the other four are absent. So the scope holds the number of files asked for, plus 50
directories. The timing covers the library calls only, not the CLI start-up, and the first run is
a warm-up that is not counted.

This is the 5,000-file case that NFR-011 names. It is not the host run: the development host has
about 44,600 entries in scope, which is above the limit. Run it on a tree of the size you want to
prove. The verdict uses the p95 of the runs (nearest rank), as the other NFRs do.

Usage: python3 -I tools/measure/nfr-011-integrity-scan.py [--files N] [--runs N]
Exit status: see tools/measure/emit.py.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import emit  # noqa: E402

from ginn.integrity.fingerprint import capture, diff  # noqa: E402

LIMIT_S = 1.0
DIRECTORIES = 50


def build_tree(root: Path, files: int) -> list[Path]:
    """Write `files` small files under root/usr/local, spread over DIRECTORIES directories."""
    created: list[Path] = []
    for index in range(files):
        directory = root / "usr" / "local" / f"d{index % DIRECTORIES:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"f{index:05d}"
        path.write_text("x", encoding="utf-8")
        created.append(path)
    return created


def one_operation(root: Path, target: Path, stamp: int) -> float:
    """Time one before capture, one change, one after capture and the diff. Fail if no change."""
    start = time.perf_counter()
    before = capture(root)
    target.write_text(f"changed {stamp}", encoding="utf-8")
    after = capture(root)
    changed = diff(before, after)
    elapsed = time.perf_counter() - start
    host_path = "/" + target.relative_to(root).as_posix()  # diff names host paths
    if host_path not in changed:
        raise RuntimeError(f"the integrity check did not report the change to {host_path}")
    return elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NFR-011: integrity check overhead, timed.")
    parser.add_argument("--files", type=int, default=5000, help="files in the scope")
    parser.add_argument("--runs", type=int, default=20, help="counted operations")
    args = parser.parse_args(argv)
    if args.files < 1 or args.runs < 1:
        parser.error("--files and --runs must be at least 1")

    work = Path(tempfile.mkdtemp(prefix="ginn-nfr011-"))
    try:
        files = build_tree(work, args.files)
        one_operation(work, files[0], 0)  # warm-up, not counted
        times = [one_operation(work, files[run % len(files)], run + 1) for run in range(args.runs)]
    finally:
        shutil.rmtree(work, ignore_errors=True)

    p95 = emit.nearest_rank(times, 0.95)
    median = statistics.median(times)
    entry = emit.line(
        "NFR-011",
        f"integrity check per operation (capture, change, capture, diff), p95 of {args.runs} "
        f"runs, {args.files} files in scope",
        p95,
        "s",
        f"<= {LIMIT_S:g} s",
        emit.verdict_at_most(p95, LIMIT_S),
        note=f"median {median:.3f} s, max {max(times):.3f} s; library calls on a synthetic tree",
    )
    return emit.emit(entry)


if __name__ == "__main__":
    raise SystemExit(main())
