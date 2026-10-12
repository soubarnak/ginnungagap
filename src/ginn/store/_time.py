# SPDX-License-Identifier: Apache-2.0
"""UTC timestamps in one textual form for every store file (DR-050)."""

from collections.abc import Callable
from datetime import UTC, datetime


def utc_from_epoch(epoch_seconds: float) -> datetime:
    """Return the aware UTC datetime for a clock reading in epoch seconds."""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC)


def utc_iso(moment: datetime) -> str:
    """Format `moment` as UTC ISO-8601 with milliseconds and a trailing Z (DR-050)."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    text = moment.astimezone(UTC).isoformat(timespec="milliseconds")
    return text[: -len("+00:00")] + "Z"


def parse_utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp that carries a timezone. Naive values are rejected."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"timestamp {text!r} has no timezone")
    return moment.astimezone(UTC)


def now_iso(clock: Callable[[], float]) -> str:
    """Read `clock` (epoch seconds) and format the moment with `utc_iso`."""
    return utc_iso(utc_from_epoch(clock()))
