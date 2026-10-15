# SPDX-License-Identifier: Apache-2.0
"""Podman JSON models parse the real output captured from Podman 5.8.8 (IR-020)."""

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from ginn.podman.models import (
    ContainerInfo,
    ContainerState,
    ExecResult,
    ImageInfo,
    PodmanInfo,
    container_state,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "podman"
ALPINE_MANIFEST = "sha256:d56c381f961d307a21b3ca004cf1e3910f106644aefb1f43e654c8a56c4fd395"
ALPINE_ID = "320994c3b997e2ec6433f717f153e108023c5bec8fefa8d76b83451d16d05ea8"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.fr("IR-020")
def test_podman_info_fixture_parses_every_field() -> None:
    info = PodmanInfo.from_info(_load("info.json"))
    assert info == PodmanInfo(
        rootless=True,
        cgroup_manager="cgroupfs",
        cgroup_version="v2",
        cgroup_controllers=("cpuset", "cpu", "io", "memory", "pids"),
        oci_runtime="runc",
        network_backend="netavark",
        graph_driver="overlay",
        conmon_path="/usr/libexec/podman/conmon",
        pasta_path="/usr/sbin/pasta",
        version="5.8.8",
    )


@pytest.mark.fr("IR-020")
def test_image_inspect_fixture_parses_digest_and_size() -> None:
    (raw,) = _load("image-inspect-alpine.json")
    image = ImageInfo.from_inspect(raw)
    assert image.id == ALPINE_ID
    assert image.digest == ALPINE_MANIFEST
    assert f"docker.io/library/alpine@{ALPINE_MANIFEST}" in image.repo_digests
    assert image.size == 8715873
    assert image.created == "2026-09-17T20:37:20.889221879Z"


@pytest.mark.fr("IR-020")
def test_image_labels_come_from_the_config() -> None:
    raw = {"Id": ALPINE_ID, "Config": {"Labels": {"io.ginn.base-digest": ALPINE_MANIFEST}}}
    assert ImageInfo.from_inspect(raw).labels == {"io.ginn.base-digest": ALPINE_MANIFEST}


@pytest.mark.fr("IR-020")
def test_image_labels_fall_back_to_the_top_level_key_and_keep_only_text() -> None:
    top = ImageInfo.from_inspect({"Labels": {"io.ginn.provision": ""}, "Config": {"Labels": None}})
    assert top.labels == {"io.ginn.provision": ""}
    mixed = ImageInfo.from_inspect({"Config": {"Labels": {"a": 1, "b": "ok"}}})
    assert mixed.labels == {"b": "ok"}
    assert ImageInfo.from_inspect({}).labels == {}


@pytest.mark.fr("CR-007")
def test_image_user_and_env_come_from_the_config() -> None:
    raw = {"Config": {"User": "1000:1000", "Env": ["PATH=/usr/bin", "HOME=/root"]}}
    image = ImageInfo.from_inspect(raw)
    assert image.user == "1000:1000"
    assert image.env == ("PATH=/usr/bin", "HOME=/root")
    # A config that names no user, or holds a wrongly typed Env, reads as none (IR-020).
    bare = ImageInfo.from_inspect({"Config": {"User": None, "Env": "PATH=/usr/bin"}})
    assert bare.user == ""
    assert bare.env == ()
    assert ImageInfo.from_inspect({}).user == ""


@pytest.mark.fr("IR-020")
def test_container_inspect_fixture_parses_created_state() -> None:
    (raw,) = _load("container-inspect-alpine.json")
    container = ContainerInfo.from_inspect(raw)
    assert container.id == "01a08e08c6e7270161286e5d469a09bac2727b1abcd1a7761e236910be6ea4de"
    assert container.name == "t211-alpine"
    assert container.image == "docker.io/library/alpine:3"
    assert container.state is ContainerState.CREATED
    assert container.pid == 0
    assert container.exit_code == 0
    assert container.started_at == ""
    assert container.finished_at == ""
    assert container.labels == {}
    assert container.size_rw is None


@pytest.mark.fr("IR-020")
def test_ps_fixture_parses_both_containers_with_labels() -> None:
    created, exited = (ContainerInfo.from_ps(item) for item in _load("ps.json"))
    assert created.name == "t211-alpine"
    assert created.state is ContainerState.CREATED
    assert created.started_at == ""
    assert exited.name == "t211-run"
    assert exited.state is ContainerState.EXITED
    assert exited.exit_code == 137
    assert exited.labels == {"ginn.realm": "t211"}
    assert exited.started_at.startswith("2026-10-12T")
    assert exited.started_at.endswith("+00:00")
    assert exited.finished_at >= exited.started_at


@pytest.mark.fr("IR-020")
def test_missing_keys_give_empty_values_not_errors() -> None:
    info = PodmanInfo.from_info({})
    assert info.rootless is False
    assert info.cgroup_controllers == ()
    assert info.version == ""

    image = ImageInfo.from_inspect({"Size": "not a number"})
    assert image.size == 0
    assert image.repo_digests == ()

    container = ContainerInfo.from_inspect({})
    assert container.state is ContainerState.UNKNOWN
    assert container.pid == 0
    assert container.labels == {}
    assert container.size_rw is None

    minimal = ContainerInfo.from_ps({"Names": None, "Labels": None, "Size": {"rw": 1}})
    assert minimal.name == ""
    assert minimal.labels == {}
    assert minimal.size_rw is None


@pytest.mark.fr("IR-020")
@pytest.mark.fr("FR-122")
def test_ps_row_carries_the_bare_image_id() -> None:
    row = {"Id": "a" * 64, "Names": ["n"], "Image": "x", "ImageID": "sha256:" + "b" * 64}
    assert ContainerInfo.from_ps(row).image_id == "b" * 64
    assert ContainerInfo.from_ps({"ImageID": "b" * 64}).image_id == "b" * 64
    assert ContainerInfo.from_ps({"ImageID": "short"}).image_id == ""
    assert ContainerInfo.from_ps({"ImageID": 7}).image_id == ""
    assert ContainerInfo.from_ps({}).image_id == ""


@pytest.mark.fr("IR-020")
@pytest.mark.parametrize(
    ("podman_value", "expected"),
    [
        ("created", ContainerState.CREATED),
        ("running", ContainerState.RUNNING),
        ("paused", ContainerState.PAUSED),
        ("exited", ContainerState.EXITED),
        ("stopped", ContainerState.EXITED),
        ("stopping", ContainerState.STOPPING),
        ("removing", ContainerState.UNKNOWN),
        ("", ContainerState.UNKNOWN),
    ],
)
def test_container_state_maps_podman_strings(podman_value: str, expected: ContainerState) -> None:
    assert container_state(podman_value) is expected


@pytest.mark.fr("IR-020")
def test_models_are_frozen() -> None:
    result = ExecResult(exit_code=0, stdout="", stderr="")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.exit_code = 1  # type: ignore[misc]
