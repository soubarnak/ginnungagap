#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""NFR-008 (T10.4): a new realm (no --with) uses at most the official image size plus 200 MiB.

The SRS method is storage accounting after create. The figures come from `ginn --json status
REALM`, and the sum is the one the T7.27 host gate counts (tests/integration/test_adr015_real.py,
`disk_use`, ADR-015 Decision 11):

  - total = `disk.image_bytes` (the derived image) + `disk.container_bytes` (the writable layer,
    podman's `SizeRw`) + `disk.realm_home_bytes` (the Realm Home, FR-020).
  - official image size = `disk.base_image_bytes`, the pinned base that the derived image names in
    its `io.ginn.base-digest` label.
  - target = the official image size + 200 MiB. The reported value is total minus the official
    image size, in MiB, and the verdict is PASS when it is at most 200 MiB.

The verdict is NOT RUN (exit 2) when ginn does not run, when status has no disk object, or when
any of the four figures is null or when the Realm Home walk is partial. The script reads the
status and starts nothing else. Nobody has run it on a host.

Usage: python3 -I tools/measure/nfr-008-disk-accounting.py --realm REALM [--ginn "ginn"]
Exit status: 0 PASS, 1 FAIL, 2 NOT RUN, see tools/measure/emit.py.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emit  # noqa: E402

MIB = 1024 * 1024
STATUS_TIMEOUT_S = 120
METRIC = "disk use of a new realm beyond the official image size"
TARGET = "<= 200 MiB beyond the official image size"
MARGIN_BYTES = 200 * MIB
FIGURES = ("image_bytes", "container_bytes", "realm_home_bytes", "base_image_bytes")


def read_status(ginn: list[str], realm: str) -> dict[str, Any]:
    """Run `ginn --json status REALM` and return its object. Raises ValueError when unusable."""
    argv = [*ginn, "--json", "status", realm, "--sizes"]
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            argv, capture_output=True, text=True, check=False, timeout=STATUS_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ValueError(f"{argv[0]} did not run: {err}") from err
    if done.returncode != 0:
        raise ValueError(f"ginn status exited {done.returncode}")
    for raw in reversed(done.stdout.splitlines()):
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("ginn status printed no JSON object")


def _bytes(disk: dict[str, Any], key: str) -> int | None:
    value = disk.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def judge(status: dict[str, Any]) -> tuple[float | None, str, str]:
    """The (value in MiB, verdict, note) of one status object, as the gate helper counts it."""
    disk = status.get("disk")
    if not isinstance(disk, dict):
        return None, emit.NOT_RUN, "the status has no disk object"
    figures = {key: _bytes(disk, key) for key in FIGURES}
    missing = [key for key, value in figures.items() if value is None]
    if missing:
        return None, emit.NOT_RUN, f"status does not report {', '.join(missing)}"
    if disk.get("realm_home_partial"):
        return None, emit.NOT_RUN, "the Realm Home walk is partial, so the total is a lower bound"
    image, container, home, base = (figures[key] or 0 for key in FIGURES)
    total = image + container + home
    over = total - base
    verdict = emit.PASS if over <= MARGIN_BYTES else emit.FAIL
    note = (
        f"total {total} bytes = image {image} + container {container} + home {home}; "
        f"official image {base} bytes; target {base + MARGIN_BYTES} bytes"
    )
    return round(over / MIB, 3), verdict, note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NFR-008: disk use of a new realm.")
    parser.add_argument("--realm", required=True, help="a realm created without --with")
    parser.add_argument("--ginn", default="ginn", help="the ginn program (split like a shell word)")
    args = parser.parse_args(argv)
    ginn = shlex.split(args.ginn)
    if not ginn:
        parser.error("--ginn names no program")
    try:
        value, verdict, note = judge(read_status(ginn, args.realm))
    except ValueError as err:
        value, verdict, note = None, emit.NOT_RUN, str(err)
    return emit.emit(emit.line("NFR-008", METRIC, value, "MiB", TARGET, verdict, note))


if __name__ == "__main__":
    raise SystemExit(main())
