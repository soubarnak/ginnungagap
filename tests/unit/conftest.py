# SPDX-License-Identifier: Apache-2.0
"""The helper seam of the unit tests (ADR-008 amendment, 2026-10-13).

ginn-rebind runs on the host now, not under `podman unshare`. The unit tests of the ops and runtime
layers were written against a fake podman that logs each helper call as `unshare` and answers it
from the host, and a test queues a failure in `unshare_results`. This fixture keeps that behaviour:
the default runner of `ginn.runtime.attach` is the fake's `unshare`, so their logs and queues are
the same. The attach tests pass their own runner, and the host gate runs the real helper.
"""

import pytest

import ginn.runtime.attach as attach_mod


@pytest.fixture(autouse=True)
def _helper_answers_through_the_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attach_mod, "run_on_host", lambda podman, argv: podman.unshare(list(argv)))
