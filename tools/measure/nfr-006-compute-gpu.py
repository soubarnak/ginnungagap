#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""NFR-006 (T10.4): compute and GPU throughput of a realm is within 3% of native.

The method is the SRS one: the 7-Zip built-in benchmark (CPU) and glmark2 (GPU), median of 5 runs,
the realm against the same build on the host. Each run on the host is followed by one run in the
realm through `ginn exec`, so a thermal or load drift hits both sides alike. The score of 7-Zip is
the rating of its `Tot:` line (MIPS). The score of glmark2 is its `glmark2 Score:` line.

The shortfall is (host median - realm median) / host median, in percent. A realm that is faster
than the host has a negative shortfall. The verdict is PASS when the shortfall is at most the
tolerance (3%). The two legs print one JSON line each (tools/measure/emit.py).

A leg is NOT RUN when its benchmark fails or prints no score on either side (the GPU leg needs a
display and a GPU in the realm), when either side prints no version banner or the banners differ,
when the host median is not positive, or when fewer than 5 runs were made (as in emit.sweep_line).
"Same build" is the SRS condition, so a banner mismatch is not judged. The script cannot see
whether the realm image and the host use the same compiler flags; that stays with the operator.

It starts no realm. The realm must be running (`ginn start REALM`), have `7z` and `glmark2`
installed, and the host must have both too. ginn is run only as `ginn exec REALM -- CMD`, with
argv lists. Podman is not called here.

Usage: python3 -I tools/measure/nfr-006-compute-gpu.py --realm REALM [--runs 5] [--tolerance 3]
           [--ginn "ginn"] [--sevenzip "7z"] [--glmark2 "glmark2"] [--skip-gpu]
Exit status: see tools/measure/emit.py.
"""

from __future__ import annotations

import argparse
import re
import shlex
import statistics
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emit  # noqa: E402

TOLERANCE_PERCENT = 3.0
RUNS_REQUIRED = 5
COMMAND_TIMEOUT_S = 900

_SEVENZIP_TOTAL = re.compile(r"^\s*Tot:\s+\d+\s+\d+\s+(\d+)\s*$", re.MULTILINE)
_SEVENZIP_BANNER = re.compile(r"^\s*(7-Zip|p7zip)\b.*$", re.MULTILINE)
_GLMARK2_SCORE = re.compile(r"glmark2 Score:\s*(\d+)")
_GLMARK2_BANNER = re.compile(r"^\s*glmark2 \d\S*\s*$", re.MULTILINE)


class LegError(RuntimeError):
    """A leg could not produce a score. The message becomes the note of its NOT RUN line."""


def _run(argv: Sequence[str]) -> str:
    """Run one benchmark and return its stdout. Any failure is a LegError."""
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise LegError(f"{argv[0]} did not run: {err}") from err
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
        raise LegError(f"{' '.join(argv)} exited {done.returncode}: {tail[0]}")
    return done.stdout


def score_sevenzip(output: str) -> float | None:
    """The total rating (MIPS) of a `7z b` run: the last field of the last `Tot:` line."""
    found = _SEVENZIP_TOTAL.findall(output)
    return float(found[-1]) if found else None


def score_glmark2(output: str) -> float | None:
    """The `glmark2 Score` of a run."""
    found = _GLMARK2_SCORE.findall(output)
    return float(found[-1]) if found else None


def banner(pattern: re.Pattern[str], output: str) -> str | None:
    match = pattern.search(output)
    return None if match is None else match.group(0).strip()


def measure_leg(
    host_argv: Sequence[str],
    realm_argv: Sequence[str],
    score: Callable[[str], float | None],
    version: re.Pattern[str],
    runs: int,
) -> tuple[list[float], list[float], str | None, str | None]:
    """`runs` pairs of runs, host first. Returns the host scores, the realm scores and banners."""
    host_scores: list[float] = []
    realm_scores: list[float] = []
    host_banner: str | None = None
    realm_banner: str | None = None
    for _ in range(runs):
        for argv, scores, side in (
            (host_argv, host_scores, "host"),
            (realm_argv, realm_scores, "realm"),
        ):
            output = _run(argv)
            value = score(output)
            if value is None:
                raise LegError(f"the {side} run printed no score")
            scores.append(value)
            found = banner(version, output)
            if side == "host":
                host_banner = host_banner or found
            else:
                realm_banner = realm_banner or found
    return host_scores, realm_scores, host_banner, realm_banner


def leg_line(
    nfr_metric: str,
    host_argv: Sequence[str],
    realm_argv: Sequence[str],
    score: Callable[[str], float | None],
    version: re.Pattern[str],
    runs: int,
    tolerance: float,
    unit_note: str,
) -> dict[str, Any]:
    target = f"shortfall <= {tolerance:g}% (realm vs same build on host), median of {RUNS_REQUIRED}"
    try:
        host, realm, host_banner, realm_banner = measure_leg(
            host_argv, realm_argv, score, version, runs
        )
    except LegError as err:
        return emit.line("NFR-006", nfr_metric, None, "%", target, emit.NOT_RUN, str(err))
    if host_banner is None or realm_banner is None:
        side = "host" if host_banner is None else "realm"
        note = f"the {side} run printed no version banner, so the build cannot be compared"
        return emit.line("NFR-006", nfr_metric, None, "%", target, emit.NOT_RUN, note)
    if host_banner != realm_banner:
        note = f"not the same build: host {host_banner!r}, realm {realm_banner!r}"
        return emit.line("NFR-006", nfr_metric, None, "%", target, emit.NOT_RUN, note)
    host_median = statistics.median(host)
    realm_median = statistics.median(realm)
    if host_median <= 0:
        note = f"{unit_note}: host median {host_median:g} is not positive"
        return emit.line("NFR-006", nfr_metric, None, "%", target, emit.NOT_RUN, note)
    shortfall = (host_median - realm_median) / host_median * 100
    figures = f"{unit_note}: host median {host_median:g}, realm median {realm_median:g}"
    if runs < RUNS_REQUIRED:
        note = (
            f"{figures}; {runs} run(s) of the {RUNS_REQUIRED} the SRS asks for; "
            f"shortfall {shortfall:.3f}% is not judged"
        )
        return emit.line("NFR-006", nfr_metric, None, "%", target, emit.NOT_RUN, note)
    return emit.line(
        "NFR-006",
        f"{nfr_metric}; median of {runs} runs",
        shortfall,
        "%",
        target,
        emit.verdict_at_most(shortfall, tolerance),
        figures,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NFR-006: compute and GPU throughput vs native.")
    parser.add_argument("--realm", required=True, help="a running realm with 7z and glmark2")
    parser.add_argument("--runs", type=int, default=RUNS_REQUIRED, help="pairs of runs per leg")
    parser.add_argument("--tolerance", type=float, default=TOLERANCE_PERCENT, help="percent")
    parser.add_argument("--ginn", default="ginn", help="the ginn program (split like a shell word)")
    parser.add_argument("--sevenzip", default="7z", help="the 7-Zip command, host and realm")
    parser.add_argument("--glmark2", default="glmark2", help="the glmark2 command, host and realm")
    parser.add_argument("--skip-gpu", action="store_true", help="measure the CPU leg only")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    ginn = shlex.split(args.ginn)
    if not ginn:
        parser.error("--ginn names no program")

    codes: list[int] = []
    legs = [
        (
            "CPU throughput (7-Zip built-in benchmark)",
            [*shlex.split(args.sevenzip), "b"],
            score_sevenzip,
            _SEVENZIP_BANNER,
            "total rating, MIPS",
        ),
    ]
    if not args.skip_gpu:
        legs.append(
            (
                "GPU throughput (glmark2)",
                shlex.split(args.glmark2),
                score_glmark2,
                _GLMARK2_BANNER,
                "glmark2 score",
            )
        )
    for metric, command, score, version, unit_note in legs:
        entry = leg_line(
            metric,
            command,
            [*ginn, "exec", args.realm, "--", *command],
            score,
            version,
            args.runs,
            args.tolerance,
            unit_note,
        )
        codes.append(emit.emit(entry))
    if args.skip_gpu:
        target = f"shortfall <= {args.tolerance:g}% (realm vs same build on host)"
        skipped = emit.line(
            "NFR-006",
            "GPU throughput (glmark2)",
            None,
            "%",
            target,
            emit.NOT_RUN,
            "skipped with --skip-gpu",
        )
        codes.append(emit.emit(skipped))
    return emit.worst(codes)


if __name__ == "__main__":
    raise SystemExit(main())
