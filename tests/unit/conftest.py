# SPDX-License-Identifier: Apache-2.0
"""The helper seam of the unit tests (ADR-008 amendment, 2026-10-13).

ginn-rebind runs on the host now, not under `podman unshare`. The unit tests of the ops and runtime
layers were written against a fake podman that logs each helper call as `unshare` and answers it
from the host, and a test queues a failure in `unshare_results`. This fixture keeps that behaviour:
the default runner of `ginn.runtime.attach` is the fake's `unshare`, so their logs and queues are
the same. The attach tests pass their own runner, and the host gate runs the real helper.

The host security tools (NFR-SEC-007). The default SecurityHooks look up `gpgconf` and
`xdg-dbus-proxy` on PATH, and a default hook can run them on the host. The fence below stands in for
the `shutil` module that ginn.runtime.realm_security uses. A unit test that reaches `gpgconf` fails
with a message that names the hook to pin. Every other tool is reported missing, so no unit test
finds xdg-dbus-proxy. A test that needs a tool passes its own hook, as the security tests do.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

import ginn.ops.delete as delete_mod
import ginn.runtime.attach as attach_mod
import ginn.runtime.realm_security as realm_security_mod
from tests.unit.realm_cli_support import World, build_world


@pytest.fixture(autouse=True)
def _helper_answers_through_the_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attach_mod, "run_on_host", lambda podman, argv: podman.unshare(list(argv)))


class _NoHostTools:
    """Stands in for the `shutil` module inside ginn.runtime.realm_security (NFR-SEC-007)."""

    @staticmethod
    def which(name: str, *args: object, **kwargs: object) -> str | None:
        if name == "gpgconf":
            raise RuntimeError(
                "a unit test reached the real gpgconf through the default gpg hook; "
                "pin SecurityHooks(gpg_extra=...) in the test"
            )
        return None


@pytest.fixture(autouse=True)
def _host_security_tools_are_fenced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realm_security_mod, "shutil", _NoHostTools)


@pytest.fixture(autouse=True)
def _cgroup_root_is_fenced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A delete's realm cgroup directory (F9) is looked up under a temporary root, never in the
    real /sys/fs/cgroup, which holds the user's own realms. A test that needs a directory passes
    `DeleteHooks(cgroup_root=...)`."""
    monkeypatch.setattr(delete_mod, "DEFAULT_CGROUP_ROOT", tmp_path / "sys-fs-cgroup")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """A pinned host for the CLI realm tests (tests/unit/realm_cli_support.py)."""
    yield build_world(tmp_path, monkeypatch)
