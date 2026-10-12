# SPDX-License-Identifier: Apache-2.0
"""FakePodman self-tests: the digest rule, the container state machine and injected failures."""

from collections.abc import Callable
from pathlib import Path

import pytest

from ginn.errors import GinnError
from ginn.podman.adapter import CreateSpec, Podman
from ginn.podman.models import ContainerState, ExecResult
from tests.fakes.fake_podman import FakePodman

DIGEST = "sha256:d56c381f961d307a21b3ca004cf1e3910f106644aefb1f43e654c8a56c4fd395"
DIGEST_REF = f"docker.io/library/alpine@{DIGEST}"


def spec(name: str = "ginn-arch-g1", **overrides: object) -> CreateSpec:
    fields: dict[str, object] = {
        "name": name,
        "image_digest_ref": DIGEST_REF,
        "network": "host",
        "labels": {"ginn.realm": "arch"},
    }
    fields.update(overrides)
    return CreateSpec(**fields)  # type: ignore[arg-type]


def expect_code(code: str, call: Callable[[], object]) -> None:
    with pytest.raises(GinnError) as info:
        call()
    assert info.value.code == code


@pytest.mark.fr("FR-011")
def test_fake_refuses_a_plain_tag_and_stores_a_digest_pull() -> None:
    fake = FakePodman()
    with pytest.raises(ValueError, match="FR-011"):
        fake.pull("docker.io/library/alpine:3")
    image = fake.pull(DIGEST_REF)
    assert image.digest == DIGEST
    assert fake.image_exists(DIGEST_REF) is True
    assert fake.image_inspect(DIGEST_REF) == image


@pytest.mark.fr("NFR-061")
def test_fake_satisfies_the_adapter_protocol() -> None:
    adapter: Podman = FakePodman()
    assert adapter.info().version == "5.8.8"


@pytest.mark.fr("NFR-061")
def test_container_moves_from_created_to_running_to_exited() -> None:
    fake = FakePodman()
    container_id = fake.create(spec())
    assert fake.inspect(container_id).state is ContainerState.CREATED
    fake.start(container_id)
    running = fake.inspect(container_id)
    assert running.state is ContainerState.RUNNING
    assert running.pid != 0
    fake.stop(container_id, 30)
    exited = fake.inspect(container_id)
    assert exited.state is ContainerState.EXITED
    assert exited.pid == 0
    assert fake.ops() == ["create", "inspect", "start", "inspect", "stop", "inspect"]


@pytest.mark.fr("NFR-061")
def test_state_machine_rejects_invalid_transitions() -> None:
    fake = FakePodman()
    container_id = fake.create(spec())
    expect_code("GINN-E-INTERNAL", lambda: fake.exec(container_id, ["true"]))
    fake.start(container_id)
    expect_code("GINN-E-INTERNAL", lambda: fake.start(container_id))
    expect_code("GINN-E-INTERNAL", lambda: fake.rm(container_id, force=False))
    fake.rm(container_id, force=True)
    expect_code("GINN-E-INTERNAL", lambda: fake.inspect(container_id))


@pytest.mark.fr("NFR-061")
def test_duplicate_container_names_are_refused() -> None:
    fake = FakePodman()
    fake.create(spec("ginn-arch-g1"))
    expect_code("GINN-E-INTERNAL", lambda: fake.create(spec("ginn-arch-g1")))


@pytest.mark.fr("NFR-SEC-001")
def test_fake_create_refuses_a_privileged_spec() -> None:
    fake = FakePodman()
    with pytest.raises(ValueError, match="NFR-SEC-001"):
        fake.create(spec(security_opts=["--privileged"]))
    assert fake.containers == {}


@pytest.mark.fr("NFR-061")
def test_ps_filters_by_every_label_pair() -> None:
    fake = FakePodman()
    fake.create(spec("ginn-arch-g1", labels={"ginn.realm": "arch", "ginn.gen": "1"}))
    fake.create(spec("ginn-debian-g1", labels={"ginn.realm": "debian", "ginn.gen": "1"}))
    assert [c.name for c in fake.ps({"ginn.realm": "arch"})] == ["ginn-arch-g1"]
    assert fake.ps({"ginn.realm": "arch", "ginn.gen": "2"}) == []
    assert len(fake.ps()) == 2


@pytest.mark.fr("FR-120")
def test_network_failure_always_exhausts_three_retries() -> None:
    fake = FakePodman(fail_pull_with="GINN-E-NETWORK")
    calls: list[int] = []

    def hook(attempt: int, err: GinnError) -> bool:
        calls.append(attempt)
        return True

    with pytest.raises(GinnError) as info:
        fake.pull(DIGEST_REF, retry_hook=hook)
    assert info.value.code == "GINN-E-NETWORK"
    assert info.value.exit_code == 69
    assert calls == [1, 2, 3]
    assert fake.ops().count("pull") == 4
    assert fake.image_exists(DIGEST_REF) is False


@pytest.mark.fr("FR-120")
def test_network_failure_once_succeeds_on_the_retry() -> None:
    fake = FakePodman(fail_pull_with="GINN-E-NETWORK", fail_pull_times=1)
    image = fake.pull(DIGEST_REF, retry_hook=lambda attempt, err: True)
    assert image.digest == DIGEST
    assert fake.ops().count("pull") == 2


@pytest.mark.fr("FR-011")
def test_digest_mismatch_is_not_retried() -> None:
    fake = FakePodman(fail_pull_with="GINN-E-IMAGE-UNVERIFIED")
    seen: list[int] = []

    def hook(attempt: int, err: GinnError) -> bool:
        seen.append(attempt)
        return True

    with pytest.raises(GinnError) as info:
        fake.pull(DIGEST_REF, retry_hook=hook)
    assert info.value.code == "GINN-E-IMAGE-UNVERIFIED"
    assert info.value.exit_code == 65
    assert seen == []
    assert fake.ops().count("pull") == 1


@pytest.mark.fr("NFR-061")
def test_exec_returns_canned_results_keyed_by_argv() -> None:
    canned = ExecResult(exit_code=3, stdout="out\n", stderr="err\n")
    fake = FakePodman(exec_results={("sh", "-c", "exit 3"): canned})
    container_id = fake.create(spec())
    fake.start(container_id)
    assert fake.exec(container_id, ["sh", "-c", "exit 3"]) == canned
    assert fake.exec(container_id, ["true"]) == ExecResult(0, "", "")
    assert fake.exec(container_id, ["sh", "-c", "exit 3"], capture=False) == ExecResult(3, "", "")
    assert fake.exec_interactive(container_id, ["sh", "-c", "exit 3"]) == 3


@pytest.mark.fr("NFR-061")
def test_injected_exec_failure_raises_with_the_chosen_code() -> None:
    fake = FakePodman(fail_exec_with="GINN-E-NETWORK")
    container_id = fake.create(spec())
    fake.start(container_id)
    expect_code("GINN-E-NETWORK", lambda: fake.exec(container_id, ["true"]))


@pytest.mark.fr("NFR-061")
def test_unknown_injected_code_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        FakePodman(fail_pull_with="GINN-E-REALM-BUSY")


@pytest.mark.fr("NFR-061")
def test_cp_top_and_logs_use_in_memory_data(tmp_path: Path) -> None:
    fake = FakePodman()
    container_id = fake.create(spec())
    fake.start(container_id)
    fake.files["/etc/os-release"] = b"NAME=Alpine\n"
    fake.tops[container_id] = [(1, 1040, "sleep")]
    fake.log_text[container_id] = "one\ntwo\nthree\n"

    destination = tmp_path / "os-release"
    fake.cp_from(container_id, "/etc/os-release", destination)
    assert destination.read_bytes() == b"NAME=Alpine\n"
    expect_code("GINN-E-INTERNAL", lambda: fake.cp_from(container_id, "/missing", destination))
    assert fake.top(container_id) == [(1, 1040, "sleep")]
    assert fake.logs(container_id, 2) == "two\nthree\n"
    assert fake.logs(container_id, 0) == ""


@pytest.mark.fr("NFR-061")
def test_every_call_is_recorded_in_order() -> None:
    fake = FakePodman()
    container_id = fake.create(spec())
    fake.start(container_id)
    fake.stop(container_id, 5)
    fake.rm(container_id, force=False)
    assert fake.ops() == ["create", "start", "stop", "rm"]
    assert fake.calls[2].args == (container_id, 5)
