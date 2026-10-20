# SPDX-License-Identifier: Apache-2.0
"""Subsets of Podman JSON that ginn reads (IR-020, ADR-002).

Only this module and cli.py read Podman output. Each parser accepts a missing or wrongly typed
key: text falls back to "", numbers to 0 or None, lists to (). A Podman release that adds or
drops a field therefore does not break the parse.
"""

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class ContainerState(StrEnum):
    """Container state as ginn names it. Podman states that ginn does not model map to UNKNOWN."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    EXITED = "exited"
    STOPPING = "stopping"
    UNKNOWN = "unknown"


_STATE_BY_PODMAN: Final[Mapping[str, ContainerState]] = {
    "created": ContainerState.CREATED,
    "configured": ContainerState.CREATED,
    "running": ContainerState.RUNNING,
    "paused": ContainerState.PAUSED,
    "exited": ContainerState.EXITED,
    "stopped": ContainerState.EXITED,
    "stopping": ContainerState.STOPPING,
}

_ZERO_TIME: Final = "0001-01-01T00:00:00Z"
_IMAGE_ID: Final = re.compile(r"[0-9a-f]{64}")


def container_state(value: str) -> ContainerState:
    """Map a Podman state string to ContainerState. Unknown or empty values give UNKNOWN."""
    return _STATE_BY_PODMAN.get(value.strip().lower(), ContainerState.UNKNOWN)


def _sub(obj: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = obj.get(key)
    return value if isinstance(value, Mapping) else {}


def _text(obj: Mapping[str, object], key: str) -> str:
    value = obj.get(key)
    return value if isinstance(value, str) else ""


def _int(obj: Mapping[str, object], key: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _optional_int(obj: Mapping[str, object], key: str) -> int | None:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _texts(obj: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = obj.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _labels(obj: Mapping[str, object], key: str) -> dict[str, str]:
    value = obj.get(key)
    if not isinstance(value, Mapping):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def _bare_image_id(value: str) -> str:
    """The image ID as 64 lowercase hex digits, without `sha256:`, or "" if it is not one."""
    bare = value.removeprefix("sha256:")
    return bare if _IMAGE_ID.fullmatch(bare) is not None else ""


def _timestamp(value: object) -> str:
    """Return a timestamp as text. Podman `ps` gives epoch seconds; zero times become ""."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        if value <= 0:
            return ""
        return dt.datetime.fromtimestamp(value, tz=dt.UTC).isoformat()
    if isinstance(value, str) and not value.startswith("0001-01-01"):
        return value
    return ""


@dataclass(frozen=True)
class ImageInfo:
    """One local image, from `podman image inspect`. `digest` is the manifest digest.

    `id` is 64 lowercase hex digits, as podman prints it. `Podman.image_id` and `Podman.commit`
    return the same text, the form that `require_image_id_ref` accepts. `labels` are the image's
    labels. The ops layer reads the base digest label of a derived image from them (ADR-015
    Decision 13). `user` and `env` are the image's `User` and `Env` config, which the provisioning
    stage carries onto its commit (ADR-015 Decision 2f). `user` is "" when the image names none.
    """

    id: str
    digest: str
    repo_digests: tuple[str, ...]
    size: int
    created: str
    labels: Mapping[str, str] = field(default_factory=dict)
    user: str = ""
    env: tuple[str, ...] = ()

    @classmethod
    def from_inspect(cls, obj: Mapping[str, object]) -> "ImageInfo":
        # The labels sit in the image config. Podman may also print them at the top level, which
        # is read only when the config holds none.
        config = _sub(obj, "Config")
        return cls(
            id=_text(obj, "Id"),
            digest=_text(obj, "Digest"),
            repo_digests=_texts(obj, "RepoDigests"),
            size=_int(obj, "Size"),
            created=_text(obj, "Created"),
            labels=_labels(config, "Labels") or _labels(obj, "Labels"),
            user=_text(config, "User"),
            env=_texts(config, "Env"),
        )


@dataclass(frozen=True)
class ContainerInfo:
    """One container. `pid` is 0 and `started_at` or `finished_at` are "" when not applicable."""

    id: str
    name: str
    image: str
    state: ContainerState
    pid: int
    exit_code: int
    started_at: str
    finished_at: str
    labels: Mapping[str, str] = field(default_factory=dict)
    size_rw: int | None = None
    image_id: str = ""
    """The ID of the image the container runs from, as 64 lowercase hex digits, or "" when podman
    gives none. A container is matched to an image by this ID alone, never by a name (ADR-015
    Decision 13)."""

    @classmethod
    def from_inspect(cls, obj: Mapping[str, object]) -> "ContainerInfo":
        """Parse `podman container inspect` output (one object of the JSON list)."""
        state = _sub(obj, "State")
        config = _sub(obj, "Config")
        return cls(
            id=_text(obj, "Id"),
            name=_text(obj, "Name"),
            image=_text(obj, "ImageName") or _text(config, "Image"),
            state=container_state(_text(state, "Status")),
            pid=_int(state, "Pid"),
            exit_code=_int(state, "ExitCode"),
            started_at=_timestamp(state.get("StartedAt")),
            finished_at=_timestamp(state.get("FinishedAt")),
            labels=_labels(config, "Labels"),
            size_rw=_optional_int(obj, "SizeRw"),
            # The top-level `Image` is the image ID; `ImageName` is the name (ADR-015 Decision 9).
            image_id=_bare_image_id(_text(obj, "Image")),
        )

    @classmethod
    def from_ps(cls, obj: Mapping[str, object]) -> "ContainerInfo":
        """Parse one object of `podman ps --all --format json`, which has a different shape."""
        names = _texts(obj, "Names")
        return cls(
            id=_text(obj, "Id"),
            name=names[0] if names else "",
            image=_text(obj, "Image"),
            state=container_state(_text(obj, "State")),
            pid=_int(obj, "Pid"),
            exit_code=_int(obj, "ExitCode"),
            started_at=_timestamp(obj.get("StartedAt")),
            finished_at=_timestamp(obj.get("ExitedAt")),
            labels=_labels(obj, "Labels"),
            size_rw=_optional_int(obj, "Size"),
            image_id=_bare_image_id(_text(obj, "ImageID")),
        )


@dataclass(frozen=True)
class ExecResult:
    """Result of one `exec`. `exit_code` is the inner command's status."""

    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class PodmanInfo:
    """Subset of `podman info --format json` that ginn reads (FR-007 doctor checks)."""

    rootless: bool
    cgroup_manager: str
    cgroup_version: str
    cgroup_controllers: tuple[str, ...]
    oci_runtime: str
    network_backend: str
    graph_driver: str
    conmon_path: str
    pasta_path: str
    version: str

    @classmethod
    def from_info(cls, obj: Mapping[str, object]) -> "PodmanInfo":
        host = _sub(obj, "host")
        store = _sub(obj, "store")
        security = _sub(host, "security")
        return cls(
            rootless=security.get("rootless") is True,
            cgroup_manager=_text(host, "cgroupManager"),
            cgroup_version=_text(host, "cgroupVersion"),
            cgroup_controllers=_texts(host, "cgroupControllers"),
            oci_runtime=_text(_sub(host, "ociRuntime"), "name"),
            network_backend=_text(host, "networkBackend"),
            graph_driver=_text(store, "graphDriverName"),
            conmon_path=_text(_sub(host, "conmon"), "path"),
            pasta_path=_text(_sub(host, "pasta"), "executable"),
            version=_text(_sub(obj, "version"), "Version"),
        )
