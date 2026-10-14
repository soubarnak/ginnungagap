# SPDX-License-Identifier: Apache-2.0
"""Host gate for the M6 tiers and their grants on real rootless Podman (T6.8).

Run once with `GINN_INTEGRATION=1 GINN_T49_RESULTS=<file>` and
`pytest tests/integration/test_tiers_real.py` on a Void host. The verdicts are in
spikes/results/T6.8-tiers-real.md. Each item writes one JSON line to the results file
(see conftest.py).

What it covers, and only this: the grants that need no package install inside a realm. Gate 3 is
not approved, so nothing here runs a package manager in a realm, and every create uses
`CreateHooks(integrate=False)`. The images are pulled by digest from the Trust Manifest, through the
product create.

Isolation. The host home is the throwaway `host.host_home` of conftest, so the user's real
~/Downloads, ~/Documents, ~/Music, ~/Projects and ~/.bashrc are never read or written. The real
mount roots are hidden from every probe, as in test_lifecycle_real.py. The host session runtime
directory is an empty directory of the test, never the user's /run/user/<uid>.

Not run here, and kept open in the toml: FR-062 AC-3 (nested `podman run` needs an image pull inside
the realm), FR-062 AC-1 (the SSH key round trip needs a key and a server), FR-067 AC-1/AC-2 (the
refused `ssh-add -D` needs an ssh client in the image, skipped when the image has none), FR-069
(NVIDIA: this host has a driver, but the CDI path on crun is pending T6.6, so the probe here reports
no driver), and FR-068 (the session bus proxy).
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - ssh-agent is started with a fixed argv, never a shell
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ginn.domain import RealmName, Tier
from ginn.domain.permissions import Toggle
from ginn.errors import GinnError
from ginn.ops.create import CreateHooks, CreateRequest, create_realm
from ginn.ops.delete import delete_realm
from ginn.ops.lifecycle import HostUser, LifecycleHooks, start_realm, stop_realm
from ginn.ops.permissions import set_permissions, set_tier
from ginn.ops.security_ops import read_agent_record
from ginn.runtime.realm_security import SecurityHooks
from ginn.security.agent import AgentRecord
from ginn.store.records import RealmRecord, RealmStore
from tests.integration.conftest import Host, gate_item

DISTRO = "debian"
HIDDEN_MOUNT_ROOTS = frozenset({"/media", "/mnt", f"/run/media/{os.environ.get('USER', '')}"})
"""The user's real mount roots. Every probe reads them as absent (see test_lifecycle_real.py)."""
REPO = Path(__file__).resolve().parents[2]
AGENT_HELPER = REPO / "native" / "build" / "ginn-agent-filter"
NAMES = {"basic": "tbasic", "custom": "tcustom", "dev": "tdev"}


def _exists(path: str) -> bool:
    """The probe of every hook here: the real mount roots read as absent."""
    return path not in HIDDEN_MOUNT_ROOTS and os.path.exists(path)


class _NoDriver:
    """The NVIDIA probe of this gate: no driver, so no CDI device is made (see the module doc)."""

    def read_text(self, path: str) -> str | None:
        return None

    def glob(self, pattern: str) -> list[str]:
        return []

    def realpath(self, path: str) -> str:
        return path

    def char_device(self, path: str) -> tuple[int, int] | None:
        return None

    def which(self, name: str) -> str | None:
        return None


def _security(*, agent: bool) -> SecurityHooks:
    if agent and not AGENT_HELPER.is_file():
        pytest.skip("native/build/ginn-agent-filter is not built: run make -C native first")
    return SecurityHooks(
        agent_helper=str(AGENT_HELPER),
        gpg_extra=lambda: None,
        nvidia_probe=_NoDriver(),
    )


def _lifecycle(host: Host, *, agent: bool = False) -> LifecycleHooks:
    return LifecycleHooks(helper=host.helper, exists=_exists, security=_security(agent=agent))


def _create(host: Host, name: str, tier: Tier) -> None:
    request = CreateRequest(
        name=RealmName(name),
        distro=DISTRO,
        uid=host.uid,
        gid=host.gid,
        host_home=host.host_home,
        host_env=host.session_env(),
        tier=tier,
    )
    create_realm(
        host.env,
        host.cli,
        request,
        hooks=CreateHooks(
            helper=host.helper,
            integrate=False,
            exists=_exists,
            security=_security(agent=False),
        ),
    )


def _start(host: Host, name: str, env: dict[str, str] | None = None) -> RealmRecord:
    session = HostUser(
        uid=host.uid,
        host_home=host.host_home,
        host_env=host.session_env() if env is None else env,
    )
    return start_realm(
        host.env,
        host.cli,
        RealmName(name),
        session=session,
        hooks=_lifecycle(host, agent=env is not None),
    )


def _stop(host: Host, name: str, *, agent: bool = False) -> None:
    stop_realm(host.env, host.cli, RealmName(name), hooks=_lifecycle(host, agent=agent))


def _load(host: Host, name: str) -> RealmRecord:
    return RealmStore(host.paths, time.time).load(RealmName(name))


def _run(host: Host, name: str, argv: list[str]) -> int:
    """Run argv in the realm as its user, and return the exit status (no shell is built here)."""
    container = _load(host, name).container_id or ""
    return host.cli.exec(container, argv).exit_code


def _host_files(host: Host) -> None:
    """The host's own tree under the throwaway home: the files each test checks for."""
    for directory in ("Downloads", "Documents", "Music"):
        (host.host_home / directory).mkdir(parents=True, exist_ok=True)
    (host.host_home / "Documents" / "host-only.txt").write_text("host\n", encoding="utf-8")
    (host.host_home / "Music" / "track.txt").write_text("track\n", encoding="utf-8")
    (host.host_home / ".bashrc").write_text("# host bashrc\n", encoding="utf-8")


def _drop(host: Host, name: str) -> None:
    """Delete a realm of this module through the product delete, which stops a running one.

    The session store is shared by every module of the gate, so a realm left here would keep its
    container on the image (the lifecycle gate removes the image) and its record in `ginn list`.
    """
    if RealmStore(host.paths, time.time).exists(RealmName(name)):
        delete_realm(host.env, host.cli, RealmName(name), purge_home=False, confirm=lambda _p: True)


@pytest.fixture(scope="module")
def realms(host: Host) -> Iterator[None]:
    """The three realms of the gate, created by the product create with integrate off (Gate 3)."""
    _host_files(host)
    _create(host, NAMES["basic"], Tier.BASIC)
    _create(host, NAMES["custom"], Tier.CUSTOM)
    _create(host, NAMES["dev"], Tier.DEVELOPMENT)
    yield
    for name in NAMES.values():
        _drop(host, name)


@pytest.mark.fr("FR-061")
def test_fr061_ac1_a_file_written_in_the_realm_downloads_appears_on_the_host(
    host: Host, realms: None
) -> None:
    with gate_item(host.results, "FR-061-AC1"):
        _start(host, NAMES["basic"])
        target = host.host_home / "Downloads" / "realm-write.txt"
        assert _run(host, NAMES["basic"], ["/usr/bin/touch", str(target)]) == 0
        assert target.exists()


@pytest.mark.fr("FR-061")
def test_fr061_ac2_the_host_documents_are_not_the_realm_documents(host: Host, realms: None) -> None:
    with gate_item(host.results, "FR-061-AC2"):
        host_only = host.host_home / "Documents" / "host-only.txt"
        assert host_only.exists()
        assert _run(host, NAMES["basic"], ["/usr/bin/test", "-e", str(host_only)]) != 0


@pytest.mark.fr("FR-062")
def test_fr062_ac2_the_shell_startup_file_is_read_only_in_a_development_realm(
    host: Host, realms: None
) -> None:
    with gate_item(host.results, "FR-062-AC2"):
        _start(host, NAMES["dev"])
        before = (host.host_home / ".bashrc").read_text(encoding="utf-8")
        script = ["/bin/sh", "-c", 'echo realm >> "$HOME/.bashrc"']
        assert _run(host, NAMES["dev"], script) != 0
        assert (host.host_home / ".bashrc").read_text(encoding="utf-8") == before


@pytest.mark.fr("FR-062")
def test_fr062_ac4_kvm_opens_read_write_inside_a_development_realm(
    host: Host, realms: None
) -> None:
    if not os.path.exists("/dev/kvm"):
        pytest.skip("this host has no /dev/kvm (IR-040)")
    with gate_item(host.results, "FR-062-AC4-open"):
        assert _run(host, NAMES["dev"], ["/bin/sh", "-c", "exec 3<>/dev/kvm"]) == 0


@pytest.mark.fr("FR-063")
def test_fr063_ac2_a_share_that_dr004_forbids_is_refused_and_nothing_changes(
    host: Host, realms: None
) -> None:
    with gate_item(host.results, "FR-063-AC2"):
        session = HostUser(uid=host.uid, host_home=host.host_home, host_env=host.session_env())
        set_tier(host.env, RealmName(NAMES["custom"]), Tier.CUSTOM)
        with pytest.raises(GinnError) as caught:
            set_permissions(
                host.env,
                RealmName(NAMES["custom"]),
                session,
                share=[(str(host.host_home / ".ssh"), "rw")],
            )
        assert caught.value.code == "GINN-E-PATH-FORBIDDEN"
        assert _load(host, NAMES["custom"]).permissions.shares == ()


@pytest.mark.fr("FR-063")
@pytest.mark.fr("FR-066")
def test_fr063_ac1_a_custom_share_is_read_only_inside_after_the_next_start(
    host: Host, realms: None
) -> None:
    with gate_item(host.results, "FR-063-AC1"):
        session = HostUser(uid=host.uid, host_home=host.host_home, host_env=host.session_env())
        _start(host, NAMES["custom"])
        set_permissions(
            host.env,
            RealmName(NAMES["custom"]),
            session,
            share=[(str(host.host_home / "Music"), "ro")],
        )
        assert _load(host, NAMES["custom"]).restart_required is True
        _stop(host, NAMES["custom"])
        _start(host, NAMES["custom"])
        assert _load(host, NAMES["custom"]).restart_required is False
        music = str(host.host_home / "Music")
        assert _run(host, NAMES["custom"], ["/usr/bin/test", "-e", f"{music}/track.txt"]) == 0
        assert _run(host, NAMES["custom"], ["/usr/bin/touch", f"{music}/new.txt"]) != 0


@pytest.mark.fr("FR-064")
def test_fr064_ac1_a_missing_projects_folder_is_not_created_and_the_status_says_so(
    host: Host, realms: None
) -> None:
    with gate_item(host.results, "FR-064-AC1"):
        projects = host.host_home / "Projects"
        assert not projects.exists()
        _start(host, NAMES["dev"])
        assert not projects.exists()
        assert "share skipped: ~/Projects (missing)" in _load(host, NAMES["dev"]).share_skipped


@pytest.mark.fr("FR-066")
def test_fr066_an_attach_only_change_is_flagged_on_a_running_realm_and_applies_at_the_next_start(
    host: Host, realms: None
) -> None:
    with gate_item(host.results, "FR-066-attach-only"):
        session = HostUser(uid=host.uid, host_home=host.host_home, host_env=host.session_env())
        set_permissions(
            host.env, RealmName(NAMES["custom"]), session, enable=[Toggle.PROJECTS.value]
        )
        assert _load(host, NAMES["custom"]).restart_required is True
        projects = host.host_home / "Projects"
        projects.mkdir(exist_ok=True)
        _stop(host, NAMES["custom"])
        _start(host, NAMES["custom"])
        assert _load(host, NAMES["custom"]).restart_required is False
        assert _run(host, NAMES["custom"], ["/usr/bin/test", "-d", str(projects)]) == 0


@pytest.mark.fr("FR-067")
def test_fr067_the_filter_socket_is_in_the_realm_and_stops_with_the_last_development_realm(
    host: Host, realms: None
) -> None:
    """The filter's socket appears inside the realm, and the filter stops with the last Development
    realm. The socket directory is short and private: a unix socket path is limited to 108 bytes,
    and the test base of pytest is longer than that. `ssh-add -D` is checked only when the image
    has an ssh client, and the check is recorded as a skip with its reason when it has none."""
    ssh_agent = shutil.which("ssh-agent")
    if ssh_agent is None:
        pytest.skip("this host has no ssh-agent to filter")
    _stop(host, NAMES["dev"])
    short = Path(tempfile.mkdtemp(prefix="ginn-t68-"))
    short.chmod(0o700)
    sock = short / "agent.sock"
    agent = subprocess.Popen(  # noqa: S603 - fixed argv
        [ssh_agent, "-D", "-a", str(sock)], start_new_session=True
    )
    try:
        with gate_item(host.results, "FR-067-socket-and-stop"):
            env = {"XDG_RUNTIME_DIR": str(short), "SSH_AUTH_SOCK": str(sock)}
            _start(host, NAMES["dev"], env=env)
            socket_in_realm = f"/run/user/{host.uid}/ginn-ssh-agent"
            assert _run(host, NAMES["dev"], ["/usr/bin/test", "-S", socket_in_realm]) == 0
            assert read_agent_record(host.paths).proc is not None
            if _run(host, NAMES["dev"], ["/bin/sh", "-c", "command -v ssh-add"]) != 0:
                host.results.record("FR-067-AC1", "SKIP", reason="the image has no ssh-add")
            _stop(host, NAMES["dev"], agent=True)
            assert read_agent_record(host.paths) == AgentRecord()
    finally:
        agent.kill()
        agent.wait()
        shutil.rmtree(short, ignore_errors=True)
