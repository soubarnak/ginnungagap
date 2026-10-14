#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""NFR-001 (T10.4): no ginn process runs while no realm runs. A read-only /proc scan, no podman.

Counts the processes of the invoking user whose command name starts with `ginn`: the CLI and the
ginn-wait, ginn-watch and ginn-rebind helpers. The runit user supervisor (a `runsvdir` whose
command line names ginn) is not in that count. It has its own bound, 1 MiB RSS. The rootless
podman pause process (`catatonit`) is shared by all of the user's containers, so it is reported on
stderr and not counted. Run the script after every realm has stopped.

Usage: python3 -I tools/measure/nfr-001-idle-processes.py [--proc DIR]
  --proc DIR   the proc filesystem to read (default /proc; tests pass a fake tree)
Exit status: see tools/measure/emit.py.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emit  # noqa: E402

SUPERVISOR_LIMIT_MIB = 1.0
GINN_PREFIX = "ginn"
SUPERVISOR_COMM = "runsvdir"
PAUSE_COMM = "catatonit"


def _status_fields(process: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw in (process / "status").read_text(encoding="utf-8", errors="replace").splitlines():
        key, _, value = raw.partition(":")
        fields[key] = value.strip()
    return fields


def scan(proc: Path, uid: int) -> list[dict[str, Any]]:
    """The processes of `uid` under `proc`: pid, command name, command line and resident KiB."""
    found: list[dict[str, Any]] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = _status_fields(entry)
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue  # the process ended during the scan
        owner = fields.get("Uid", "").split()
        if not owner or owner[0] != str(uid):
            continue
        rss_fields = fields.get("VmRSS", "0 kB").split()
        rss_kib = int(rss_fields[0]) if rss_fields and rss_fields[0].isdigit() else 0
        found.append(
            {
                "pid": int(entry.name),
                "comm": comm,
                "cmdline": cmdline.decode("utf-8", errors="replace").strip(),
                "rss_kib": rss_kib,
            }
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NFR-001: ginn processes with no realm running.")
    parser.add_argument("--proc", type=Path, default=Path("/proc"))
    args = parser.parse_args(argv)

    processes = scan(args.proc, os.getuid())
    ginn = [p for p in processes if p["comm"].startswith(GINN_PREFIX)]
    supervisors = [
        p for p in processes if p["comm"] == SUPERVISOR_COMM and GINN_PREFIX in p["cmdline"]
    ]
    pauses = [p for p in processes if p["comm"] == PAUSE_COMM]

    for process in ginn:
        print(f"ginn process {process['pid']}: {process['comm']}", file=sys.stderr)
    if pauses:
        print(
            f"note: {len(pauses)} catatonit (podman pause) process(es), not counted",
            file=sys.stderr,
        )

    count_metric = "processes from ginn executables with no realm running (supervisor excluded)"
    entries = [
        emit.line(
            "NFR-001",
            count_metric,
            float(len(ginn)),
            "processes",
            "0",
            emit.verdict_at_most(float(len(ginn)), 0.0),
        )
    ]
    if supervisors:
        rss_mib = sum(p["rss_kib"] for p in supervisors) / emit.KIB_PER_MIB
        entries.append(
            emit.line(
                "NFR-001",
                "runit user supervisor (runsvdir) resident memory",
                rss_mib,
                "MiB",
                f"<= {SUPERVISOR_LIMIT_MIB:g} MiB",
                emit.verdict_at_most(rss_mib, SUPERVISOR_LIMIT_MIB),
            )
        )
    else:
        entries.append(
            emit.line(
                "NFR-001",
                "runit user supervisor (runsvdir) resident memory",
                None,
                "MiB",
                f"<= {SUPERVISOR_LIMIT_MIB:g} MiB",
                emit.NOT_RUN,
                "no runsvdir with ginn in its command line was found",
            )
        )
    return emit.worst([emit.emit(entry) for entry in entries])


if __name__ == "__main__":
    raise SystemExit(main())
