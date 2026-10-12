# SPDX-License-Identifier: Apache-2.0
"""Output channel: results on stdout, errors and verbose lines on stderr (AS-025, NFR-042)."""

import json
import sys
from collections.abc import Callable

from ginn.errors import GinnError


def _line(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


class Output:
    """Render command results and errors.

    Streams are looked up at each call so that stream capture in tests sees the output.
    """

    def __init__(self, *, json_mode: bool, verbose: bool) -> None:
        self.json_mode = json_mode
        self.verbose = verbose

    def emit(self, obj: object, render: Callable[[], str] | None = None) -> None:
        """Write `obj` as JSON in JSON mode; otherwise write `render()` when given."""
        if self.json_mode or render is None:
            sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
        else:
            sys.stdout.write(_line(render()))

    def error(self, err: GinnError) -> None:
        """Write a failure to stderr: one JSON object in JSON mode, else two plain lines."""
        if self.json_mode:
            payload = {
                "error": {
                    "code": err.code,
                    "message": err.message,
                    "remedy": err.remedy,
                    "exit_code": err.exit_code,
                }
            }
            sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")
        else:
            sys.stderr.write(f"{err.code}: {err.message}\n  fix: {err.remedy}\n")

    def verbose_line(self, text: str) -> None:
        """Write `text` to stderr, only when verbose mode is on."""
        if self.verbose:
            sys.stderr.write(_line(text))
