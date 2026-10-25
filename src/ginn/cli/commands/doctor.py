# SPDX-License-Identifier: Apache-2.0
"""`ginn doctor [--json]`: read-only host and user checks (FR-007, NFR-041, IR-010).

The command runs every check of `ginn.doctor.checks` for the account that runs it, prints the report
on stdout, and exits 1 with GINN-E-DOCTOR-FAILED when any check fails. The error line names the
count, and the fix line under each failing check says what to run. A warning or an informational
line does not fail the command. Doctor refuses to run as root, because its checks are about one
user's realms (GINN-E-USAGE). `--root-dir` is hidden from the help: the tests pass it, so they never
read the real host.
"""

from __future__ import annotations

import argparse
import os
import pwd
from pathlib import Path

from ginn.cli.commands import CommandSpec, Context
from ginn.doctor import report
from ginn.doctor.checks import Identity, Probe, run_checks
from ginn.errors import make


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root-dir", default="/", metavar="DIR", help=argparse.SUPPRESS)


def _identity() -> Identity:
    uid = os.getuid()
    try:
        entry = pwd.getpwuid(uid)
    except KeyError:
        return Identity(user=str(uid), uid=uid, gid=os.getgid(), home=os.environ.get("HOME", "/"))
    return Identity(user=entry.pw_name, uid=uid, gid=entry.pw_gid, home=entry.pw_dir)


def _run(args: argparse.Namespace, ctx: Context) -> int:
    identity = _identity()
    if identity.uid == 0:
        raise make(
            "GINN-E-USAGE",
            detail="ginn doctor checks one user's realms; run it as that user, not as root",
        )
    probe = Probe(
        root=Path(str(args.root_dir)),
        env=os.environ,
        identity=identity,
        verbose=ctx.output.verbose_line,
    )
    doctor_report = report.build(identity, run_checks(probe))
    ctx.output.emit(doctor_report.as_dict(), render=doctor_report.text)
    if doctor_report.failed:
        raise make(
            "GINN-E-DOCTOR-FAILED",
            failed=doctor_report.failed,
            total=doctor_report.total,
        )
    return 0


SPEC = CommandSpec(
    name="doctor",
    help="check that this host and this user can run rootless realms; read-only (FR-007)",
    example="ginn doctor --json",
    configure=_configure,
    run=_run,
)
