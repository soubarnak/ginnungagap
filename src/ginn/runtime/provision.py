# SPDX-License-Identifier: Apache-2.0
"""Provisioning argv for the init set (ADR-015 Decision 2f, 2g and 4; T7.18).

This module owns the argv that the provision stage runs: the provisioning
container create, the reset create from the stage image, the two commit
``--change`` lists, and the install/clean/diff/remove argv. Every function
returns an argv list and never a shell string (NFR-SEC-005). The provisioning
container has no Realm Home, device or passwd entry (CR-007), so a host
path never reaches the provision step.

The caller (ops/provision.py, T7.21) sets TMPDIR and containers.conf's
image_copy_tmp_dir (Open question 10) and owns the rollback.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from ginn.podman.adapter import CreateSpec

# ADR-015 Decision 4: the reset create uses the committed stage image and a
# cleared entrypoint. The provision container is named ginn_provision_<realm>.
PROVISION_PREFIX: Final = "ginn_provision_"
RESET_ENTRYPOINT: Final = "[]"

# Commit --change lists. The stage image is an intermediate, the final image
# is the one the realm keeps. Both are committed with --pause=true.
STAGE_COMMIT_CHANGES: Final[tuple[str, ...]] = (
    'LABEL org.opencontainers.image.title="ginn-stage"',
)
FINAL_COMMIT_CHANGES: Final[tuple[str, ...]] = (
    'LABEL org.opencontainers.image.title="ginn"',
    "LABEL io.ginn.managed=true",
)


def provisioning_spec(
    name: str,
    image_digest_ref: str,
    *,
    distro_id: str = "debian",
) -> CreateSpec:
    """The CreateSpec for the provisioning container (CR-007).

    No Realm Home, no device, no passwd entry, no tier mounts. The container
    runs as the host user with keep-id and host network, so the provision
    step never sees a realm path.
    """
    return CreateSpec(
        name=f"{PROVISION_PREFIX}{name}",
        image_digest_ref=image_digest_ref,
        network="host",
        user="0:0",
        userns="keep-id",
        http_proxy=False,
        init=False,
        entrypoint_argv=["/bin/sh"],
        command=["-c", "sleep infinity"],
        labels={"io.ginn.provision": name},
    )


def reset_spec(
    name: str,
    image_id: str,
    image_digest_ref: str,
) -> CreateSpec:
    """The reset create from the stage image (ADR-015 Decision 4).

    Uses the stage image_id with --entrypoint '[]' and no command, as
    CreateSpec.to_argv emits for ginn_provision_ names. The image_digest_ref
    is kept for validation but the image is the ID.
    """
    return CreateSpec(
        name=f"{PROVISION_PREFIX}{name}",
        image_digest_ref=image_digest_ref,
        image_id=image_id,
        network="host",
        user="0:0",
        userns="keep-id",
        http_proxy=False,
        init=False,
        entrypoint_argv=[],
        command=[],
        labels={"io.ginn.provision": name},
    )


def stage_commit_argv(container_id: str, image_ref: str) -> list[str]:
    """Argv for ``podman commit`` of the stage image."""
    args: list[str] = ["commit", "--pause=true"]
    for change in STAGE_COMMIT_CHANGES:
        args += ["--change", change]
    args += [container_id, image_ref]
    return args


def final_commit_argv(container_id: str, image_ref: str) -> list[str]:
    """Argv for ``podman commit`` of the final image."""
    args: list[str] = ["commit", "--pause=true"]
    for change in FINAL_COMMIT_CHANGES:
        args += ["--change", change]
    args += [container_id, image_ref]
    return args


def install_argv(distro_id: str, packages: Sequence[str]) -> list[str]:
    """Argv for installing packages inside the provision container."""
    # Minimal: use distro's package manager via the provision container's shell.
    # Real work is in ops/provision.py which runs the distro's install verb inside.
    if distro_id in ("debian", "kali"):
        return ["apt-get", "update", "&&", "apt-get", "install", "-y", *packages]
    if distro_id == "fedora":
        return ["dnf", "install", "-y", *packages]
    return ["pacman", "-S", "--noconfirm", *packages]


def clean_argv(distro_id: str) -> list[list[str]]:
    """Argv steps for cleaning the package cache after install (ADR-015)."""
    if distro_id in ("debian", "kali"):
        return [
            ["apt-get", "clean"],
            ["rm", "-rf", "/var/lib/apt/lists"],
            ["mkdir", "-p", "/var/lib/apt/lists/partial"],
        ]
    if distro_id == "fedora":
        return [["dnf", "clean", "all"]]
    return [["find", "/var/cache/pacman/pkg", "-mindepth", "1", "-delete"]]


def diff_argv(container_id: str) -> list[str]:
    """Argv for ``podman diff`` of the provision container."""
    return ["diff", container_id]


def remove_argv(container_id: str, force: bool = False) -> list[str]:
    """Argv for ``podman rm`` of the provision container."""
    args = ["rm"]
    if force:
        args.append("--force")
    args.append(container_id)
    return args
