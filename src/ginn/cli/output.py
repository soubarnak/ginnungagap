# SPDX-License-Identifier: Apache-2.0
"""Output channel for results, warnings, verbose lines and errors (AS-025, NFR-040, NFR-042).

Stream contract:
- stdout carries the result only. In text mode it is the rendered text. In JSON mode it is one
  JSON object. On failure stdout is empty. `--help` and `--version` are exempt: argparse writes
  their text to stdout even in JSON mode (IR-010). Output does not render them. `ginn doctor` is
  the one exception to "stdout is empty on failure": it prints its report and then fails with
  GINN-E-DOCTOR-FAILED, because FR-007 AC-1 needs the fix command in the output.
- stderr in text mode: a failure or warning is `CODE: message` then `  fix: remedy`; verbose
  lines are plain text.
- stderr in JSON mode: every line is one JSON object. Verbose lines are `{"verbose": "..."}`.
  Warnings that cannot ride in a result are `{"warning": {code, message, remedy}}`. On failure the
  error object `{"error": {code, message, remedy, exit_code, details}}` is always the last line.

Warnings (GINN-W-*) do not fail a command. In JSON mode `warn()` queues them and `emit()` adds
them to a dict result as `"warnings": [{code, message, remedy}]`. A non-dict result, or a failure,
writes the queued warnings to stderr as `{"warning": ...}` lines first, so the error object stays
last. In text mode `warn()` writes to stderr at once.
"""

import json
import sys
from collections.abc import Callable

from ginn.errors import GinnError
from ginn.errors import Warning as GinnWarning


def _line(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _warning_dict(w: GinnWarning) -> dict[str, str]:
    return {"code": w.code, "message": w.message, "remedy": w.remedy}


class Output:
    """Render command results, warnings, verbose lines and errors.

    Streams are looked up at each call so that stream capture in tests sees the output.
    """

    def __init__(self, *, json_mode: bool, verbose: bool) -> None:
        self.json_mode = json_mode
        self.verbose = verbose
        self._warnings: list[GinnWarning] = []

    def emit(self, obj: object, render: Callable[[], str] | None = None) -> None:
        """Write the result. JSON mode, or no `render`, writes `obj` as JSON; else `render()`."""
        if not (self.json_mode or render is None):
            sys.stdout.write(_line(render()))
            return
        payload = obj
        if self._warnings:
            if isinstance(obj, dict):
                if "warnings" in obj:
                    raise ValueError("result already has a 'warnings' key")
                payload = {**obj, "warnings": [_warning_dict(w) for w in self._warnings]}
                self._warnings.clear()
            else:
                self._flush_warnings()
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")

    def warn(self, w: GinnWarning) -> None:
        """Report a warning: queued in JSON mode, written to stderr now in text mode."""
        if self.json_mode:
            self._warnings.append(w)
        else:
            sys.stderr.write(f"{w.code}: {w.message}\n  fix: {w.remedy}\n")

    def error(self, err: GinnError) -> None:
        """Write a failure to stderr. In JSON mode, queued warnings come first, the error last."""
        if self.json_mode:
            self._flush_warnings()
            payload = {
                "error": {
                    "code": err.code,
                    "message": err.message,
                    "remedy": err.remedy,
                    "exit_code": err.exit_code,
                    "details": err.details,
                }
            }
            sys.stderr.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        else:
            sys.stderr.write(f"{err.code}: {err.message}\n  fix: {err.remedy}\n")

    def verbose_line(self, text: str) -> None:
        """Write `text` to stderr, only when verbose mode is on."""
        if not self.verbose:
            return
        if self.json_mode:
            sys.stderr.write(json.dumps({"verbose": text.rstrip("\n")}, sort_keys=True) + "\n")
        else:
            sys.stderr.write(_line(text))

    def _flush_warnings(self) -> None:
        for w in self._warnings:
            sys.stderr.write(json.dumps({"warning": _warning_dict(w)}, sort_keys=True) + "\n")
        self._warnings.clear()
