# SPDX-License-Identifier: Apache-2.0
"""Job runner of the S-11 host spike (spikes/s11/s11.py). It runs as a runit service in the
supervisor leaf, because a session process cannot move into a services-on realm (F2).

A job file is JSON, written by s11.py: `out` (the result directory), `env` (the environment of every
step and of the tail), `steps` (name, argv and optional `required` and `timeout`, argv lists only,
never a shell) and `tail` (the argv the service ends with, `sleep infinity` when absent). The runner
writes `<name>.out` and `<name>.rc` for each step, then `done` (`ok` or `failed`). A failed required
step ends the steps, and the service then keeps the sleep, not the tail. Then it execs the tail with
the job environment, so runsv supervises the tail (ginn-wait, or the sleep of an exec job).
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
from pathlib import Path
from typing import Any

KEEP: list[str] = ["/usr/bin/sleep", "infinity"]
"""What the service does after its steps when there is no tail, or the tail did not start."""


def run(job_path: str) -> None:
    job: dict[str, Any] = json.loads(Path(job_path).read_text(encoding="utf-8"))
    out = Path(job["out"])
    env = {str(k): str(v) for k, v in dict(job["env"]).items()}
    failed = False
    for step in job["steps"]:
        name = str(step["name"])
        rc = 125
        with (out / f"{name}.out").open("wb") as sink:
            try:
                done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    list(step["argv"]),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    timeout=float(step.get("timeout", 600)),
                    check=False,
                )
                rc = done.returncode
            except subprocess.TimeoutExpired:
                rc = 124
        (out / f"{name}.rc").write_text(f"{rc}\n", encoding="utf-8")
        if rc != 0 and step.get("required", False):
            failed = True
            break
    (out / "done").write_text("failed\n" if failed else "ok\n", encoding="utf-8")
    tail = list(KEEP) if failed or not job.get("tail") else list(job["tail"])
    os.execve(tail[0], tail, env)  # noqa: S606 - the tail is fixed argv, runsv supervises it
