# SPDX-License-Identifier: Apache-2.0
"""Output channel for results, warnings, verbose lines and errors (AS-025, NFR-040, NFR-042).

Stream contract:
- stdout carries the result only. In text mode it is the rendered text. In JSON mode it is one
  JSON object. On failure stdout is empty. `--help` and `--version` are exempt: argparse writes
  their text to stdout even in JSON mode (IR-010). Output does not render them. `ginn doctor` is
  the one exception to "stdout is empty on failure": it prints its report and then fails with
  GINN-E-DOCTOR-FAILED, because FR-007 AC-1 needs the fix command in the output.
- stderr in text mode: a failure or warning is `CODE: message` then `  fix: remedy`; a failure
  then has one `  note: text` line for each note it collected; verbose lines are plain text. A
  progress step is a verbose line in text mode, so the default text run prints nothing extra.
  `note()` writes `note: text` to stderr for a command that succeeded, and `install` uses it.
  `delete` prints its notes as `note:` lines on stdout, inside its text result instead.
- stderr in JSON mode: every line is one JSON object. Success notes never go here: `install` and
  `delete` carry them in the result under `notes`, which both always include. Verbose lines are
  `{"verbose": "..."}`.
  Each finished step of a long operation is `{"progress": {"step", "message"}}` (FR-107), and it
  is written whether or not --verbose is on. The `space-checked` step adds `required_bytes`,
  `free_bytes` and `reason` to that object (FR-121, T7.35). Readers take `step` and `message`.
  Warnings that cannot ride in a result are
  `{"warning": {code, message, remedy}}`. On failure the error object
  `{"error": {code, message, remedy, exit_code, details, notes}}` is always the last line. `notes`
  is a list, empty when the failure collected none.

Warnings (GINN-W-*) do not fail a command. In JSON mode `warn()` queues them and `emit()` adds
them to a dict result as `"warnings": [{code, message, remedy}]`. A non-dict result, or a failure,
writes the queued warnings to stderr as `{"warning": ...}` lines first, so the error object stays
last. In text mode `warn()` writes to stderr at once.
"""

import json
import sys
from collections.abc import Callable, Mapping

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

    def note(self, text: str) -> None:
        """Report a note of a command that succeeded: `note: text` on stderr in text mode.

        In JSON mode nothing is written here. A command with notes puts them in its result, under
        `notes`, so stderr keeps only its own JSON lines. A note is not a warning: it has no code,
        and it does not depend on --verbose.
        """
        if not self.json_mode:
            sys.stderr.write(_line(f"note: {text}"))

    def error(self, err: GinnError) -> None:
        """Write a failure to stderr. In JSON mode, queued warnings come first, the error last.

        The notes that the failure collected on its way up (`add_note`) are printed too: one
        `  note: ...` line each in text mode, and the `notes` list of the error object in JSON mode.
        """
        notes = list(getattr(err, "__notes__", []))
        if self.json_mode:
            self._flush_warnings()
            payload = {
                "error": {
                    "code": err.code,
                    "message": err.message,
                    "remedy": err.remedy,
                    "exit_code": err.exit_code,
                    "details": err.details,
                    "notes": notes,
                }
            }
            sys.stderr.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        else:
            lines = [f"{err.code}: {err.message}", f"  fix: {err.remedy}"]
            lines.extend(f"  note: {note}" for note in notes)
            sys.stderr.write("\n".join(lines) + "\n")

    def verbose_line(self, text: str) -> None:
        """Write `text` to stderr, only when verbose mode is on."""
        if not self.verbose:
            return
        if self.json_mode:
            sys.stderr.write(json.dumps({"verbose": text.rstrip("\n")}, sort_keys=True) + "\n")
        else:
            sys.stderr.write(_line(text))

    def progress(self, step: str, message: str, detail: Mapping[str, object] | None = None) -> None:
        """Report one finished step of a long operation (FR-107).

        JSON mode writes `{"progress": {"step", "message"}}` to stderr at once, whether or not
        verbose is on, because the Control Plane reads these lines. `detail` adds its keys to that
        object, such as the figures of the `space-checked` step (FR-121). The Control Plane reads
        `message` only, so a new key breaks no reader. Text mode shows `message` as a verbose line
        only, so a text run stays quiet by default.
        """
        if self.json_mode:
            body: dict[str, object] = {"step": step, "message": message}
            if detail:
                body.update(detail)
            payload = {"progress": body}
            sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")
        else:
            self.verbose_line(message)

    def flush_warnings(self) -> None:
        """Write the queued warnings to stderr now. For a command whose stdout is not its result.

        `ginn exec` and `ginn enter` send the inner process's output to stdout, so no result is
        emitted to carry the warnings. In text mode nothing is queued, so this does nothing there.
        """
        self._flush_warnings()

    def _flush_warnings(self) -> None:
        for w in self._warnings:
            sys.stderr.write(json.dumps({"warning": _warning_dict(w)}, sort_keys=True) + "\n")
        self._warnings.clear()
