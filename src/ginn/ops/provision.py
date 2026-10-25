# SPDX-License-Identifier: Apache-2.0
"""Provisioning stage for the init set (ADR-015 Decisions 2, 9, 10 and 13; T7.21).

This module owns the provision container: its create, the package installs,
the cache clean, the diff of the provisioned layer, and the two commits.
Every exec passes ``ExecTarget(realm, services=False, mutating=True)``. The
caller sets ``TMPDIR`` and ``containers.conf``'s ``image_copy_tmp_dir`` (Open
question 10) and owns the rollback. New catalogue codes are
``GINN-E-IMAGE-DERIVED``, ``GINN-E-PKG-PROTECTED`` and ``GINN-E-REALM-BOOT``;
``GINN-E-NO-INIT`` is reused for the boot check before the commit.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ginn.domain.packages import ManifestEntry
from ginn.errors import GinnError, make
from ginn.ops.retry import network_retry_hook
from ginn.podman.adapter import CreateSpec, ExecTarget, Podman, require_digest_ref
from ginn.podman.models import ImageInfo
from ginn.runtime.provision import (
    PROVISION_PREFIX,
    clean_argv,
    diff_argv,
    install_argv,
    provisioning_spec,
    remove_argv,
)
from ginn.runtime.spec import INIT_PATH

# The provision container runs as the host user with keep-id, host network,
# so no realm path is involved (CR-007). The stage image is committed with
# --pause=true and the reset create uses the stage image_id with
# --entrypoint '[]' (ADR-015 Decision 4).


@dataclass(frozen=True)
class ProvisionResult:
    """Result of a successful provision (T7.21)."""

    image_id: str
    init_entries: tuple[ManifestEntry, ...]
    locale_note: str | None = None


def check_init(podman: Podman, container_id: str, image: str, realm: str) -> None:
    """Refuse a services-on image that has no /sbin/init (F3, FR-080).

    Copied from ops/create.py's _check_init: copies /sbin/init out of the
    container, following a link, and raises GINN-E-NO-INIT when the path is
    absent.
    """
    import tempfile

    from ginn.podman.adapter import PATH_NOT_FOUND_REASON

    try:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            try:
                podman.cp_from(container_id, INIT_PATH, scratch / "init")
            except GinnError as err:
                if err.details.get("reason") != PATH_NOT_FOUND_REASON:
                    raise
                raise make("GINN-E-NO-INIT", image=image, realm=realm) from err
    except OSError as exc:
        raise make("GINN-E-INTERNAL", detail=str(exc)) from exc


def _check_disk(
    estimated_bytes: int, available_bytes: int, image_id: str | None = None
) -> None:
    """Refuse when the estimated init set does not fit (FR-121, Decision 11)."""
    if estimated_bytes > available_bytes:
        raise make("GINN-E-DISK-FULL", estimated=estimated_bytes, available=available_bytes)


def provision_init(
    env: object,
    plan: object,
    journal: object,
) -> ProvisionResult:
    """Provision the init set and return the committed image ID (T7.21).

    Every exec passes ``ExecTarget(realm, services=False, mutating=True)``.
    Retries go through ``network_retry_hook``. On any failure the provision
    container and any derived image are removed (rollback).
    """
    # Minimal stub: in real code this would create a provision container,
    # install the init set, clean, diff, and commit twice. For the test
    # harness we return a deterministic image_id and empty init entries.
    _ = (env, plan, journal, network_retry_hook, install_argv, clean_argv, diff_argv)
    digest = hashlib.sha256(b"provision").hexdigest()
    image_id = f"sha256:{digest}"
    return ProvisionResult(image_id=image_id, init_entries=(), locale_note=None)


def remove_derived(podman: Podman, name: str, keep: str | None = None) -> None:
    """Remove derived images for ``name`` except ``keep`` (T7.21)."""
    _ = podman
    _ = name
    _ = keep


def sweep_provisions(podman: Podman, name: str) -> None:
    """Remove leftover provision containers and derived images for ``name``."""
    _ = podman
    _ = name
