#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""NFR-008 (T10.4): a new realm (no --with) uses at most the official image size plus 200 MiB.

The SRS method is storage accounting after create. This script is blocked: ginn reports no figure
that covers what a realm adds to the podman storage, so it cannot judge the target. It always
prints NOT RUN, with the figures it can read and the reason.

What a realm adds is more than its Realm Home. A keep-id container gets its own copy of the layers
it runs from, and the realm runs from a flattened derived image, so the added storage is the
derived image, the keep-id copy and the writable layer (src/ginn/ops/provision.py, T7.33). The
image does not cancel out of the comparison. What `ginn --json status REALM` reports is:

  - `disk.realm_home_bytes`, the allocated size of the Realm Home. This is read and shown.
  - `disk.container_bytes`, the writable layer. It is always null: the podman adapter never passes
    `--size` (src/ginn/ops/lifecycle.py). Blocked: status never reports container_bytes.
  - nothing for the derived image or the keep-id copy.

The `space-checked` step of create (FR-121) carries a storage figure, but it is an estimate of the
peak a create needs, not accounting of what the realm uses, so it is not used as a measurement.

The script becomes useful when status reports the derived image, the keep-id copy and the
container layer. Until then the host run of NFR-008 needs `podman system df` or `du` against the
ginn storage by hand, with --root, --runroot and --tmpdir set (AGENTS.md).

Usage: python3 -I tools/measure/nfr-008-disk-accounting.py --realm REALM [--ginn "ginn"]
Exit status: 2 (NOT RUN), see tools/measure/emit.py.
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
BLOCKED = (
    "blocked: status never reports container_bytes, and reports nothing for the flattened "
    "derived image or the keep-id copy; the space-checked figure is an estimate"
)


def read_status(ginn: list[str], realm: str) -> dict[str, Any]:
    """Run `ginn --json status REALM` and return its object. Raises ValueError when unusable."""
    argv = [*ginn, "--json", "status", realm]
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


def describe(status: dict[str, Any]) -> str:
    """What the status reports of disk use, for the note."""
    disk = status.get("disk")
    if not isinstance(disk, dict):
        return "the status has no disk object"
    home = disk.get("realm_home_bytes")
    text = "realm home not reported"
    if isinstance(home, int) and not isinstance(home, bool):
        partial = " (partial)" if disk.get("realm_home_partial") else ""
        text = f"realm home {home / MIB:.3f} MiB{partial}"
    container = disk.get("container_bytes")
    if container is None:
        return f"{text}, container_bytes null"
    return f"{text}, container_bytes {container}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NFR-008: disk use of a new realm (blocked).")
    parser.add_argument("--realm", required=True, help="a realm created without --with")
    parser.add_argument("--ginn", default="ginn", help="the ginn program (split like a shell word)")
    args = parser.parse_args(argv)
    ginn = shlex.split(args.ginn)
    if not ginn:
        parser.error("--ginn names no program")
    try:
        seen = describe(read_status(ginn, args.realm))
    except ValueError as err:
        seen = str(err)
    return emit.emit(
        emit.line("NFR-008", METRIC, None, "MiB", TARGET, emit.NOT_RUN, f"{BLOCKED}; {seen}")
    )


if __name__ == "__main__":
    raise SystemExit(main())
