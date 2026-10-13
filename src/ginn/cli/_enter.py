# SPDX-License-Identifier: Apache-2.0
"""The run of `ginn enter` and `ginn exec` in a realm: the exit status rule (IR-011, FR-049).

`enter_realm` returns the status of the inner process. That status is the exit status of the
command, so a ginn-side failure must not reuse a status a command can return. A GinnError raised
here leaves the process with EXEC_GINN_FAILURE (125) instead of the catalogue status. Its error
line keeps the GINN-E code and the remedy, so the failure is still named on stderr. The status
that `enter_realm` returns, 125 included, is passed through unchanged (enter.py, the 125 note).
"""

from __future__ import annotations

import dataclasses
import traceback
from collections.abc import Callable, Sequence

from ginn.cli import _context
from ginn.cli.output import Output
from ginn.domain import RealmName
from ginn.errors import EXEC_GINN_FAILURE, GinnError, make
from ginn.errors import Warning as GinnWarning
from ginn.runtime.enter import enter_realm


def enter_or_exec(
    output: Output,
    realm: str,
    *,
    root: bool,
    argv: Sequence[str] | None,
) -> int:
    """Run `argv` in realm `realm`, or its login shell when `argv` is None. Return its status.

    The realm name is checked by the caller, before this function, as an argument error (exit 2).
    `root` runs as container root inside the realm (FR-042). A warning from a re-attach is written
    before the command runs, so it comes before the command's output.

    Every other exception of the run becomes GINN-E-INTERNAL with the exit status 125, and its
    traceback goes to stderr in verbose mode. The two exceptions to the 125 rule are the argparse
    refusals (exit 2), which come before this function, and Ctrl-C (130), which is not an
    Exception and reaches main().
    """
    name = RealmName(realm)
    try:
        realms = _context.realms(output)
        _context.settle_interrupted(realms, output)
        output.flush_warnings()  # before the inner command writes, so the warnings come first
        hooks = dataclasses.replace(_context.enter_hooks(), notify=_notifier(output))
        status = enter_realm(
            realms.env,
            realms.podman,
            name,
            session=realms.host,
            gid=realms.gid,
            argv=None if argv is None else list(argv),
            root=root,
            hooks=hooks,
        )
    except GinnError as err:
        err.exit_code = EXEC_GINN_FAILURE
        raise
    except Exception as exc:
        output.verbose_line(traceback.format_exc())
        internal = make("GINN-E-INTERNAL")
        internal.exit_code = EXEC_GINN_FAILURE
        internal.add_note(f"{type(exc).__name__}: {exc}")
        raise internal from exc
    _context.finish(realms, output)
    return status


def _notifier(output: Output) -> Callable[[GinnWarning], None]:
    """Write one warning now: a re-attach happens before the command, so its warning goes first."""

    def notify(notice: GinnWarning) -> None:
        output.warn(notice)
        output.flush_warnings()

    return notify
