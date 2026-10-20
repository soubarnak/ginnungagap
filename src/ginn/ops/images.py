# SPDX-License-Identifier: Apache-2.0
"""The podman rules for a distribution's base image and for the leftovers of a Kali estimate.

A distribution's pinned base image is shared by every realm of that distribution, and by the
estimate that reads its everything set (ops/estimate.py). A create, a delete, a rebuild, a resume
and an estimate of the same distribution each hold its image lock (store/locks.py image_lock), so
no two of them touch the image at once. The callers of this module hold that lock:

- `remove_image` removes an image unless a container uses it, or a derived image of ginn is built
  on it (ADR-015 Decision 13). delete, resume and the estimate's recovery sweep (ops/recovery.py)
  remove an image by this one rule.
- `uses_image` is the alias-aware match of a container's image string against the image's names.
  Podman shows an image by name, digest or id, so a plain equality would miss a container. The
  create's rollback (ops/create.py) matches the same way with `image_names` and `uses_image`, and
  keeps its own rmi, so a failed rmi is raised there.
- `remove_estimate_containers` removes the throwaway containers of a killed estimate, found by
  their label (LABEL_ESTIMATE). An estimate holds the image lock while it runs, so under the lock
  such a container is dead, and it is not a user of the image.

This module makes no podman call of its own. It only calls the adapter it is given.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

from ginn.errors import GinnError
from ginn.podman.adapter import Podman
from ginn.podman.models import ImageInfo
from ginn.runtime.provision import DERIVED_IMAGE_PREFIX, LABEL_BASE_DIGEST
from ginn.runtime.spec import LABEL_ESTIMATE


def image_names(podman: Podman, ref: str) -> tuple[str, ...]:
    """The strings by which podman may show this image: its reference, digest, id and repo digests.
    When the image cannot be inspected, only the reference is known."""
    return _names_of(ref, _inspected(podman, ref))


def uses_image(listed: str, names: Sequence[str]) -> bool:
    """True when a container's image string and one of the image's names contain one another.

    Podman shows an image by name, digest or id, so a plain equality would miss a container. A
    doubtful match keeps the image, which is the safe reading. An empty string is never a match:
    the caller skips it and notes that it did.
    """
    return any(name != "" and (name in listed or listed in name) for name in names)


def remove_image(podman: Podman, ref: str) -> tuple[str, str | None, list[str]]:
    """Remove the image unless a container uses it, or a derived image is built on it. Return the
    outcome, the code of a failed rmi, and the notes that say why the image was kept or a container
    skipped.

    A base image is kept while a derived image of ginn is built on it (ADR-015 Decision 13, Open
    question 13). The base goes when the last such derived image has gone. A failed rmi is not
    fatal: the image stays, and the delete goes on. A resume that replaces an older pin uses this
    too (ops/resume.py), so both remove an image by one rule.
    """
    if not podman.image_exists(ref):
        return "absent", None, []
    info = _inspected(podman, ref)
    names = _names_of(ref, info)
    notes: list[str] = []
    for container in podman.ps():
        if container.image == "":
            notes.append(f"container {container.id[:12]} shows no image name; it is not compared")
            continue
        if uses_image(container.image, names):
            notes.append(f"image kept: container {container.id[:12]} uses it")
            return "kept", None, notes
    held = _held_by_derived(podman, ref, info)
    if held is not None:
        notes.append(held)
        return "kept", None, notes
    try:
        podman.rmi(ref)
    except GinnError as exc:
        notes.append(f"image kept: rmi failed with {exc.code}")
        return "kept", exc.code, notes
    return "removed", None, notes


def remove_estimate_containers(podman: Podman, distro: str) -> None:
    """Stop and force-remove the estimate containers of `distro`. Absent is a no-op.

    The caller holds the image lock of `distro`, so the containers with the label are leftovers of
    an estimate that was killed. A container that cannot be stopped is still removed with force.
    Each listed container must carry the label itself, as delete's realm listing does (DR-010), so
    a listing that holds another container never acts on it.
    """
    for info in podman.ps({LABEL_ESTIMATE: distro}):
        if info.labels.get(LABEL_ESTIMATE) != distro:
            continue
        with contextlib.suppress(GinnError):
            podman.stop(info.id, 0)  # the keep-alive is idle, so there is no grace period
        podman.rm(info.id, force=True)


def _inspected(podman: Podman, ref: str) -> ImageInfo | None:
    """The image's inspect, or None when podman cannot inspect it."""
    try:
        return podman.image_inspect(ref)
    except GinnError:
        return None


def _names_of(ref: str, info: ImageInfo | None) -> tuple[str, ...]:
    """The names of an image: its reference, and, when it was inspected, its digest, id and repo
    digests. Without an inspect only the reference is known."""
    if info is None:
        return (ref,)
    return (ref, info.digest, info.id, *info.repo_digests)


def _held_by_derived(podman: Podman, ref: str, info: ImageInfo | None) -> str | None:
    """The note for a base that a derived image is built on, or None when none is (ADR-015 D13).

    A derived image carries `io.ginn.base-digest`, the manifest digest of the pinned base that it
    was built from. When that label equals one of the base's manifest digests, the base stays:
    podman removes the base's record under a child (S-11), and the next create of the distribution
    would pull it again. The base's manifest digests are its `ImageInfo.digest` and the digest part
    of each of its repo digests. The derived images are listed by the prefix that every realm
    shares, and the image being removed is never its own holder. A base whose digest cannot be read
    is kept, because no derived image can then be ruled out. An empty value is never a digest, so an
    empty label never counts.
    """
    if info is None:
        return (
            "image kept: its digest cannot be read, so a derived image built on it is not ruled out"
        )
    digests = _manifest_digests(info)
    if not digests:
        return None
    for name, derived in podman.images_by_name(DERIVED_IMAGE_PREFIX).items():
        if name == ref or derived.id == info.id:
            continue
        if derived.labels.get(LABEL_BASE_DIGEST, "") in digests:
            realm = name.removeprefix(DERIVED_IMAGE_PREFIX).partition(":")[0]
            return f"image kept: realm {realm}'s derived image {name} is built on this base"
    return None


def _manifest_digests(info: ImageInfo) -> set[str]:
    """The manifest digests that name this base: `ImageInfo.digest`, and the `sha256:` part of each
    repo digest (`repo@sha256:<hex>`). An empty value is dropped, so it names no image."""
    values = {info.digest, *(entry.partition("@")[2] for entry in info.repo_digests)}
    return {value for value in values if value != ""}


__all__ = [
    "image_names",
    "remove_estimate_containers",
    "remove_image",
    "uses_image",
]
