# SPDX-License-Identifier: Apache-2.0
"""Network-failure classification and the FR-120 retry wait (T8.1, FR-120, AS-023).

Only a network failure is retried, and every other failure is raised at once. The classifier reads
the output of a failed command, its stderr and stdout, and its exit status:

- NETWORK_PATTERNS holds the transport text that podman and the package managers print alike: DNS,
  connect, TLS and timeout failures, and HTTP 429 or 5xx from a registry.
- A distribution's own mirror text stays in its TOML file as `network_error_regex` (NFR-060). The
  caller passes that pattern as `extra`, so apt, dnf and pacman text is not written in Python.
- A signature failure is never a network failure (FR-012, ADR-011). Stale metadata is not retried,
  because a retry cannot fix it (ADR-011).

`with_retry` runs one operation. After a network failure it waits the next delay of RETRY_DELAYS
(5, 15 and 45 s) and runs again, so the default is four attempts and three waits. When the last
retry fails too, the error is GINN-E-NETWORK. It keeps the attempt count and the tail of the last
output. A failure that is not network is raised unchanged, with no wait.

`network_retry_hook` is the same rule as a RetryHook, for the adapter's `with_network_retry`.
"""

import re
import time
from collections.abc import Callable, Sequence
from typing import Final

from ginn.errors import GinnError, make
from ginn.podman.adapter import RetryHook

RETRY_DELAYS: Final[tuple[float, ...]] = (5.0, 15.0, 45.0)
"""Seconds before retries 1 to 3 of a network failure (FR-120, AS-023)."""
TAIL_CHARS: Final = 2000
"""The most output text a GINN-E-NETWORK error keeps: its last characters."""
NETWORK_CODE: Final = "GINN-E-NETWORK"
_TEXT_CODES: Final = frozenset({NETWORK_CODE, "GINN-E-PKG-MANAGER", "GINN-E-INTERNAL"})
"""The codes whose output is read for network text. A signature or digest code is never retried."""
_OUTPUT_KEYS: Final = ("output", "stderr", "detail")
"""The details that carry a failed command's output, in the order they are joined."""

_TRANSPORT: Final = (
    r"dial tcp",
    r"tls handshake timeout",
    r"no route to host",
    r"network is unreachable",
    r"temporary failure in name resolution",
    r"could not resolve host",
    r"couldn't resolve host",
    r"no such host",
    r"connection refused",
    r"connection reset by peer",
    r"connection timed out",
    r"failed to connect to",
    r"operation timed out",
    r"i/o timeout",
    r"toomanyrequests",
    r"HTTP (?:status|error)\D{0,12}(?:429|5\d\d)\b",
    r"(?:429|5\d\d) (?:Too Many Requests|Service Unavailable|Bad Gateway|Gateway Timeout)\b",
)
NETWORK_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(text, re.IGNORECASE) for text in _TRANSPORT
) + (
    # Go's io.ErrUnexpectedEOF. tar's "Unexpected EOF in archive" is a damaged archive, so the
    # match is case-sensitive and that line is not a network failure.
    re.compile(r"unexpected EOF"),
)


def is_network(
    text: str,
    *,
    exit_code: int | None = None,
    extra: re.Pattern[str] | None = None,
    signature: re.Pattern[str] | None = None,
) -> bool:
    """True when `text`, the output of a failed command, is a transient network failure.

    An exit status of 0 is never a failure. A `signature` match wins over every other text. A match
    of `extra` (the distribution's mirror pattern) or of NETWORK_PATTERNS is a network failure. The
    exit status is not otherwise read, because podman and the package managers use different codes.
    """
    if exit_code == 0:
        return False
    if signature is not None and signature.search(text) is not None:
        return False
    if extra is not None and extra.search(text) is not None:
        return True
    return any(pattern.search(text) is not None for pattern in NETWORK_PATTERNS)


def failure_text(err: GinnError) -> str:
    """The output that an error carries in its details, one part per key, joined by newlines."""
    parts = [err.details[key] for key in _OUTPUT_KEYS if isinstance(err.details.get(key), str)]
    return "\n".join(str(part) for part in parts)


def is_network_error(err: GinnError, *, extra: re.Pattern[str] | None = None) -> bool:
    """True when a GinnError is a transient network failure that a retry can fix (FR-120).

    GINN-E-NETWORK is one. A package-manager or internal failure whose output shows network text
    is one as well, which is the classification create.py and packages.py defer to T8.1. A failure
    marked stale metadata is not, and neither is any other code, such as a signature failure.
    """
    if err.code == NETWORK_CODE:
        return True
    if err.code not in _TEXT_CODES or err.details.get("stale_metadata") is True:
        return False
    exit_code = err.details.get("exit")
    return is_network(
        failure_text(err),
        exit_code=exit_code if isinstance(exit_code, int) else None,
        extra=extra,
    )


def with_retry[T](
    op: Callable[[], T],
    *,
    classify: Callable[[GinnError], bool] = is_network_error,
    delays: Sequence[float] = RETRY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
    notify: Callable[[int, float, GinnError], None] | None = None,
) -> T:
    """Run `op`, and retry a network failure after each wait in `delays` (FR-120, AS-023).

    `op` raises GinnError when it fails. A failure that `classify` does not call network is raised
    at once, the same object, and no wait happens. Before each wait, `notify(retry, delay, err)`
    runs, with `retry` counted from 1. When the last wait is spent and the next attempt fails too,
    the error is GINN-E-NETWORK. A network failure that already is GINN-E-NETWORK gets the attempt
    count in its details and is raised as it is. Any other one is wrapped, and the wrapper keeps the
    attempt count, the code of the last failure and the tail of its output in its details.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            return op()
        except GinnError as err:
            if not classify(err):
                raise
            if attempts > len(delays):
                if err.code == NETWORK_CODE:
                    err.details["attempts"] = attempts
                    raise
                raise _exhausted(err, attempts) from err
            delay = delays[attempts - 1]
            if notify is not None:
                notify(attempts, delay, err)
            sleep(delay)


def network_retry_hook(
    sleep: Callable[[float], None] = time.sleep,
    *,
    delays: Sequence[float] = RETRY_DELAYS,
    notify: Callable[[int, float, GinnError], None] | None = None,
) -> RetryHook:
    """The FR-120 wait as a RetryHook, for `podman/adapter.with_network_retry` (FR-120, AS-023).

    The hook is called after failed attempt `attempt`, counted from 1. It waits the delay of that
    attempt and returns True. Once the delays are spent it returns False, and the adapter raises.
    """

    def hook(attempt: int, err: GinnError) -> bool:
        if attempt > len(delays):
            return False
        delay = delays[attempt - 1]
        if notify is not None:
            notify(attempt, delay, err)
        sleep(delay)
        return True

    return hook


def _exhausted(err: GinnError, attempts: int) -> GinnError:
    """The GINN-E-NETWORK that ends the retries of a failure which was network only by its text."""
    network = make("GINN-E-NETWORK")
    network.details.update(
        {
            "attempts": attempts,
            "last_code": err.code,
            "stderr": failure_text(err)[-TAIL_CHARS:],
        }
    )
    exit_code = err.details.get("exit")
    if isinstance(exit_code, int):
        network.details["exit"] = exit_code
    return network
