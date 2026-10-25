# SPDX-License-Identifier: Apache-2.0
"""The network classifier and the FR-120 retry (T8.1, ops/retry.py).

Covered: the classifier table (network text, and text that must never be retried), the mirror
pattern of each distribution's TOML file, the refusals of signature and stale-metadata failures,
the retry timing (5, 15 and 45 s after four attempts), success on a later attempt, the immediate
raise of a failure that is not network, the details of GINN-E-NETWORK after the last retry, and the
RetryHook that the podman adapter calls. No command runs and nothing sleeps: every wait goes
through a recording `sleep`.
"""

import re

import pytest

from ginn.distros import load as load_distro
from ginn.errors import GinnError, make
from ginn.ops.retry import (
    NETWORK_PATTERNS,
    RETRY_DELAYS,
    TAIL_CHARS,
    is_network,
    is_network_error,
    network_retry_hook,
    with_retry,
)
from ginn.podman.adapter import with_network_retry

pytestmark = pytest.mark.fr("FR-120")

NETWORK_TEXT = (
    "dial tcp: lookup registry-1.docker.io: no such host",
    "net/http: TLS handshake timeout",
    "connect: no route to host",
    "lookup registry-1.docker.io: temporary failure in name resolution",
    "dial tcp 52.1.2.3:443: connect: connection refused",
    "read tcp 10.0.2.15:41622->52.1.2.3:443: i/o timeout",
    "Connection reset by peer",
    "Connection timed out [IP: 151.101.2.132 80]",
    "Could not resolve host: mirror.example.org",
    "unexpected EOF",
    "pinging container registry: received unexpected HTTP status: 503 Service Unavailable",
    "toomanyrequests: 429 Too Many Requests",
)
NOT_NETWORK_TEXT = (
    "Error: manifest unknown",
    "Error: digest did not match",
    "no space left on device",
    "permission denied",
    "E: Unable to locate package nosuch",
    "error: target not found: nosuch",
    "Unexpected EOF in archive",
    "Need to get 500 MB of archives.",
    "pinging container registry: received unexpected HTTP status: 404 Not Found",
)


@pytest.mark.parametrize("text", NETWORK_TEXT)
def test_transport_text_is_a_network_failure(text: str) -> None:
    assert is_network(text, exit_code=125)
    assert is_network(text, exit_code=1)


@pytest.mark.parametrize("text", NOT_NETWORK_TEXT)
def test_other_text_is_not_a_network_failure(text: str) -> None:
    assert not is_network(text, exit_code=125)
    assert not is_network(text, exit_code=1)


def test_the_transport_table_has_no_empty_or_duplicate_entries() -> None:
    sources = [pattern.pattern for pattern in NETWORK_PATTERNS]
    assert len(sources) == len(set(sources))
    assert all(source for source in sources)


def test_exit_status_zero_is_never_a_failure() -> None:
    assert not is_network("dial tcp: i/o timeout", exit_code=0)


def test_a_signature_match_wins_over_network_text() -> None:
    text = "GPG error: the signature is invalid. Connection timed out"
    assert not is_network(text, signature=re.compile(r"signature"))
    assert is_network(text)


MIRROR_FAILURES = (
    (
        "debian",
        "Err:1 http://deb.debian.org/debian bookworm InRelease\n"
        "  Temporary failure resolving 'deb.debian.org'",
    ),
    (
        "kali",
        "Err:1 http://http.kali.org/kali kali-rolling InRelease\n"
        "  Could not resolve 'http.kali.org'",
    ),
    (
        "fedora",
        "Curl error (28): Timeout was reached for "
        "https://mirror.example.org/fedora/repodata/repomd.xml",
    ),
    (
        "arch",
        "error: failed retrieving file 'core.db' from mirror.example.org : "
        "Operation too slow. Less than 1 bytes/sec transferred the last 10 seconds",
    ),
)


@pytest.mark.parametrize(("distro", "sample"), MIRROR_FAILURES)
def test_a_mirror_failure_is_network_only_with_its_distribution_pattern(
    distro: str, sample: str
) -> None:
    pattern = load_distro(distro).network_error
    assert is_network(sample, exit_code=1, extra=pattern)
    assert not is_network(sample, exit_code=1)


def test_a_package_manager_failure_with_network_text_is_network() -> None:
    err = make("GINN-E-PKG-MANAGER", realm="r")
    err.details.update({"exit": 100, "output": "E: Failed to fetch x  Connection timed out"})
    assert is_network_error(err)


def test_a_stale_metadata_failure_is_not_network() -> None:
    err = make("GINN-E-PKG-MANAGER", realm="r")
    err.details.update({"exit": 1, "detail": "Connection timed out", "stale_metadata": True})
    assert not is_network_error(err)


def test_a_signature_failure_is_never_retried_even_with_network_text() -> None:
    err = make("GINN-E-PKG-SIGNATURE", realm="r")
    err.details.update({"detail": "Connection timed out"})
    assert not is_network_error(err)


def test_an_image_digest_failure_is_never_retried() -> None:
    err = make("GINN-E-IMAGE-UNVERIFIED", distro="debian")
    err.details.update({"stderr": "dial tcp: i/o timeout"})
    assert not is_network_error(err)


def test_an_internal_failure_without_network_text_is_not_retried() -> None:
    err = make("GINN-E-INTERNAL")
    err.details.update({"stderr": "Error: manifest unknown"})
    assert not is_network_error(err)


def test_a_network_code_is_network_whatever_its_text() -> None:
    assert is_network_error(make("GINN-E-NETWORK"))


class _Script:
    """An operation that raises each queued error in turn, then returns `value`."""

    def __init__(self, *errors: GinnError, value: str = "done") -> None:
        self._errors = list(errors)
        self.value = value
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self.value


def _net(stderr: str = "dial tcp: i/o timeout") -> GinnError:
    err = make("GINN-E-NETWORK")
    err.details["stderr"] = stderr
    return err


def _pkg(detail: str) -> GinnError:
    err = make("GINN-E-PKG-MANAGER", realm="r")
    err.details.update({"exit": 100, "detail": detail, "output": f"x\n{detail}"})
    return err


def test_a_success_runs_once_and_waits_never() -> None:
    sleeps: list[float] = []
    op = _Script(value="ok")
    assert with_retry(op, sleep=sleeps.append) == "ok"
    assert op.calls == 1
    assert sleeps == []


def test_a_network_failure_is_retried_after_five_seconds_and_then_succeeds() -> None:
    sleeps: list[float] = []
    op = _Script(_net(), value="ok")
    assert with_retry(op, sleep=sleeps.append) == "ok"
    assert op.calls == 2
    assert sleeps == [5.0]


def test_four_network_failures_wait_five_fifteen_and_forty_five_seconds_then_raise() -> None:
    sleeps: list[float] = []
    op = _Script(_net(), _net(), _net(), _net())
    with pytest.raises(GinnError) as caught:
        with_retry(op, sleep=sleeps.append)
    assert caught.value.code == "GINN-E-NETWORK"
    assert caught.value.exit_code == 69
    assert op.calls == 4
    assert sleeps == [5.0, 15.0, 45.0]
    assert sleeps == list(RETRY_DELAYS)


def test_a_success_on_the_third_try_stops_the_waits_there() -> None:
    sleeps: list[float] = []
    op = _Script(_net(), _net(), value="ok")
    assert with_retry(op, sleep=sleeps.append) == "ok"
    assert sleeps == [5.0, 15.0]


def test_the_error_after_the_last_retry_keeps_the_last_stderr_and_the_attempt_count() -> None:
    op = _Script(_net("attempt 1"), _net("attempt 2"), _net("attempt 3"), _net("attempt 4"))
    with pytest.raises(GinnError) as caught:
        with_retry(op, sleep=lambda _: None)
    assert caught.value.code == "GINN-E-NETWORK"
    assert caught.value.details["attempts"] == 4
    assert caught.value.details["stderr"] == "attempt 4"


def test_a_package_manager_network_text_ends_as_network_with_its_output() -> None:
    op = _Script(
        _pkg("Connection timed out"),
        _pkg("Connection timed out"),
        _pkg("Connection timed out"),
        _pkg("Could not resolve host: mirror.example.org"),
    )
    with pytest.raises(GinnError) as caught:
        with_retry(op, sleep=lambda _: None)
    err = caught.value
    assert err.code == "GINN-E-NETWORK"
    assert err.details["last_code"] == "GINN-E-PKG-MANAGER"
    assert err.details["attempts"] == 4
    assert err.details["exit"] == 100
    assert "Could not resolve host: mirror.example.org" in str(err.details["stderr"])
    assert err.__cause__ is not None
    assert err.__cause__.code == "GINN-E-PKG-MANAGER"


def test_a_failure_that_is_not_network_is_raised_at_once_and_unchanged() -> None:
    sleeps: list[float] = []
    failure = _pkg("E: Unable to locate package nosuch")
    op = _Script(failure)
    with pytest.raises(GinnError) as caught:
        with_retry(op, sleep=sleeps.append)
    assert caught.value is failure
    assert op.calls == 1
    assert sleeps == []


def test_a_non_network_failure_after_a_network_retry_stops_the_retries() -> None:
    sleeps: list[float] = []
    signature = make("GINN-E-PKG-SIGNATURE", realm="r")
    op = _Script(_net(), signature)
    with pytest.raises(GinnError) as caught:
        with_retry(op, sleep=sleeps.append)
    assert caught.value is signature
    assert op.calls == 2
    assert sleeps == [5.0]


def test_notify_runs_before_each_wait_and_counts_the_retries_from_one() -> None:
    events: list[tuple[str, int | float]] = []

    def notify(retry: int, delay: float, err: GinnError) -> None:
        assert err.code == "GINN-E-NETWORK"
        events.append(("notify", retry))
        events.append(("delay", delay))

    op = _Script(_net(), _net(), _net(), _net())
    with pytest.raises(GinnError):
        with_retry(op, sleep=lambda delay: events.append(("sleep", delay)), notify=notify)
    assert events == [
        ("notify", 1),
        ("delay", 5.0),
        ("sleep", 5.0),
        ("notify", 2),
        ("delay", 15.0),
        ("sleep", 15.0),
        ("notify", 3),
        ("delay", 45.0),
        ("sleep", 45.0),
    ]


def test_custom_delays_set_the_waits_and_the_attempt_count() -> None:
    sleeps: list[float] = []
    op = _Script(_net(), _net(), _net())
    with pytest.raises(GinnError) as caught:
        with_retry(op, delays=(1.0,), sleep=sleeps.append)
    assert op.calls == 2
    assert sleeps == [1.0]
    assert caught.value.details["attempts"] == 2


def test_no_delays_means_one_attempt_and_no_wait() -> None:
    sleeps: list[float] = []
    op = _Script(_net())
    with pytest.raises(GinnError) as caught:
        with_retry(op, delays=(), sleep=sleeps.append)
    assert caught.value.code == "GINN-E-NETWORK"
    assert op.calls == 1
    assert sleeps == []


def test_a_classifier_that_refuses_the_network_text_stops_the_retry() -> None:
    sleeps: list[float] = []
    op = _Script(_net())
    with pytest.raises(GinnError) as caught:
        with_retry(op, classify=lambda err: False, sleep=sleeps.append)
    assert caught.value.code == "GINN-E-NETWORK"
    assert op.calls == 1
    assert sleeps == []


def test_an_exception_that_is_not_a_ginn_error_is_not_retried() -> None:
    sleeps: list[float] = []
    calls: list[int] = []

    def op() -> str:
        calls.append(1)
        raise RuntimeError("not a command failure")

    with pytest.raises(RuntimeError):
        with_retry(op, sleep=sleeps.append)
    assert calls == [1]
    assert sleeps == []


def test_the_kept_stderr_is_the_tail_of_a_long_output() -> None:
    long_text = "x" * (TAIL_CHARS * 2) + "Connection refused"
    op = _Script(_pkg(long_text), _pkg(long_text), _pkg(long_text), _pkg(long_text))
    with pytest.raises(GinnError) as caught:
        with_retry(op, sleep=lambda _: None)
    stderr = str(caught.value.details["stderr"])
    assert len(stderr) <= TAIL_CHARS
    assert stderr.endswith("Connection refused")


def test_the_hook_waits_the_delay_of_each_failed_attempt_then_refuses() -> None:
    sleeps: list[float] = []
    notified: list[int] = []
    hook = network_retry_hook(
        sleeps.append, notify=lambda retry, delay, err: notified.append(retry)
    )
    err = _net()
    assert hook(1, err) is True
    assert hook(2, err) is True
    assert hook(3, err) is True
    assert hook(4, err) is False
    assert sleeps == [5.0, 15.0, 45.0]
    assert notified == [1, 2, 3]


def test_the_hook_drives_the_adapter_retry_with_the_fr120_waits() -> None:
    sleeps: list[float] = []
    calls: list[int] = []

    def action() -> str:
        calls.append(1)
        raise _net()

    with pytest.raises(GinnError) as caught:
        with_network_retry(action, network_retry_hook(sleeps.append))
    assert caught.value.code == "GINN-E-NETWORK"
    assert len(calls) == 4
    assert sleeps == [5.0, 15.0, 45.0]
