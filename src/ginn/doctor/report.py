# SPDX-License-Identifier: Apache-2.0
"""The `ginn doctor` report: text for people, JSON for scripts (FR-007 AC-1, AC-2, NFR-041).

JSON schema, version 1. The object has these keys, and every key is always present:

- "schema": 1, the version of this layout. A change to a key bumps it.
- "user": the account name that ran doctor. "uid": its numeric id.
- "ok": true when no check has status "FAIL", false otherwise.
- "counts": the number of checks per status, with the keys "PASS", "WARN", "FAIL" and "INFO".
- "checks": one object per check, in report order, with "name", "status" ("PASS", "WARN", "FAIL"
  or "INFO"), "message", and "fixes": a list of shell commands. A FAIL has at least one fix. Other
  statuses have an empty list.

The text report prints one line per check, `STATUS  name: message`, then one `  fix: <command>` line
per fix, then a summary line. The exit status is decided by the caller: `ginn doctor` exits non-zero
when `Report.failed` is above zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ginn.doctor.checks import CheckResult, Identity, Status

SCHEMA_VERSION: Final = 1


@dataclass(frozen=True)
class Report:
    """The results of one doctor run, for one account."""

    identity: Identity
    results: tuple[CheckResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status is Status.FAIL)

    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(1 for result in self.results if result.status is status)
            for status in Status
        }

    def as_dict(self) -> dict[str, object]:
        """The JSON object of schema version 1 (see the module docstring)."""
        return {
            "schema": SCHEMA_VERSION,
            "user": self.identity.user,
            "uid": self.identity.uid,
            "ok": self.failed == 0,
            "counts": self.counts(),
            "checks": [
                {
                    "name": result.name,
                    "status": result.status.value,
                    "message": result.message,
                    "fixes": list(result.fixes),
                }
                for result in self.results
            ],
        }

    def text(self) -> str:
        """The text report: one line per check, its fix lines, then the summary."""
        lines = [f"ginn doctor for {self.identity.user} (uid {self.identity.uid})"]
        for result in self.results:
            lines.append(f"{result.status.value:<4}  {result.name}: {result.message}")
            lines.extend(f"  fix: {fix}" for fix in result.fixes)
        counts = self.counts()
        summary = ", ".join(f"{counts[status.value]} {status.value}" for status in Status)
        lines.append(f"summary: {summary}")
        return "\n".join(lines)


def build(identity: Identity, results: Sequence[CheckResult]) -> Report:
    """Wrap the results of `checks.run_checks` in a report."""
    return Report(identity=identity, results=tuple(results))
