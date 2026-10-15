# SPDX-License-Identifier: Apache-2.0
"""The provisioning stage of a services-on realm: names, podman argv and commit configuration
(ADR-015 Decisions 2, 3 and 10).

A services-on realm is built in two stages. A provisioning container, created from the pinned
base, runs the package steps of the distribution data as root. Its commit gives the derived image
that the services-on container runs from (ADR-015 Decision 1). This module holds the pure builders
for that stage. It runs no podman and writes nothing:

- the names and labels of each provisioning object, which the sweeps match (Decision 10);
- the provisioning create (Decision 2b) and the reset create from the stage image (Decision 2g);
- the stage and final commit `--change` lists (Decisions 2f and 2g);
- the in-container steps, as argv lists built from the distribution data (Decision 3);
- the removal order of Decision 2h, as data.

The argv goes to the adapter (`Podman.create`, `Podman.commit`, `Podman.exec`), which adds the
global flags. Package names reach the install argv as separate list elements, never as a shell
string (NFR-SEC-005).
"""

import hashlib
import re
from collections.abc import Sequence
from typing import Final, Literal

from ginn.distros.loader import DistroDef, IntegrationSet, Verb
from ginn.domain.names import RealmName
from ginn.podman.adapter import CreateSpec, require_digest_ref, require_image_id_ref
from ginn.runtime.spec import HOST_NETWORK, KEEP_ALIVE_COMMAND, KEEP_ALIVE_ENTRYPOINT

LABEL_PROVISION: Final = "io.ginn.provision"
"""The provisioning label. A provisioning container carries `<realm>:<gen>`, and each commit empties
it on its image, so no derived image keeps a realm identity (ADR-015 Decisions 2f, 2g)."""
LABEL_INIT_SET: Final = "io.ginn.init-set"
"""The init set's hash on the derived and stage images (ADR-015 Decision 2f). `init_set_hash`
computes it."""
LABEL_BASE_DIGEST: Final = "io.ginn.base-digest"
"""The pinned base's manifest digest on the derived and stage images (ADR-015 Decisions 2f, 13)."""
PROVISION_NAME_PREFIX: Final = "ginn_provision_"
"""The name prefix of every provisioning container. A realm name has no `_`, so the prefix with the
realm and its `_` is unique to one realm (ADR-015 Decision 10)."""
DERIVED_IMAGE_PREFIX: Final = "localhost/ginn-init-"
"""The repository prefix of every derived and stage image, in ginn's local storage (ADR-015 Decision
10). A realm name has no `:`, so the prefix with the realm and its `:` is unique to one realm."""
ROOT_USER: Final = "0:0"
"""The provisioning container runs its steps as root, under keep-id (ADR-015 Decision 2b)."""
_ROOT_UID: Final = "0"
"""The USER of a stage image whose pinned image names none (ADR-015 Decision 2f)."""
_MANIFEST_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
_HASH: Final = re.compile(r"[0-9a-f]{64}")
_IMAGE_USER: Final = re.compile(r"[A-Za-z0-9_.:-]+")
_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONTROL: Final = frozenset("\0\n\r")
_QUOTES: Final = ('"', "'")


def _check_generation(generation: int) -> None:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError(f"generation must be an int of at least 1, not {generation!r}")


def provision_name(realm: RealmName, generation: int) -> str:
    """The provisioning container's name, `ginn_provision_<realm>_g<gen>` (ADR-015 Decision 10)."""
    _check_generation(generation)
    return f"{PROVISION_NAME_PREFIX}{realm}_g{generation}"


def reset_name(realm: RealmName, generation: int) -> str:
    """The reset container's name, `ginn_provision_<realm>_g<gen>_reset` (ADR-015 Decision 2g)."""
    return f"{provision_name(realm, generation)}_reset"


def provision_label(realm: RealmName, generation: int) -> str:
    """The value of `io.ginn.provision` on a provisioning or reset container: `<realm>:<gen>`."""
    _check_generation(generation)
    return f"{realm}:{generation}"


def provision_name_prefix(realm: RealmName) -> str:
    """The sweep prefix of a realm's provisioning containers: `ginn_provision_<realm>_`."""
    return f"{PROVISION_NAME_PREFIX}{realm}_"


def derived_image_prefix(realm: RealmName) -> str:
    """The sweep prefix of a realm's derived and stage images: `localhost/ginn-init-<realm>:`."""
    return f"{DERIVED_IMAGE_PREFIX}{realm}:"


def derived_image_name(realm: RealmName, generation: int) -> str:
    """The derived image's name, `localhost/ginn-init-<realm>:g<gen>` (ADR-015 Decision 10)."""
    _check_generation(generation)
    return f"{DERIVED_IMAGE_PREFIX}{realm}:g{generation}"


def stage_image_name(realm: RealmName, generation: int) -> str:
    """The stage image's name, `localhost/ginn-init-<realm>:g<gen>-stage` (ADR-015 Decision 2f). It
    is a tag of the derived image's repository, so the prefix sweep finds it."""
    return f"{derived_image_name(realm, generation)}-stage"


def manifest_digest(image_digest_ref: str) -> str:
    """The `sha256:<hex>` manifest digest of a pinned `repo@sha256:<hex>` reference (FR-011).

    It is the form that `ImageInfo.digest` holds, so the `io.ginn.base-digest` label compares equal
    with it (ADR-015 Decision 13). It is not an image ID, so it keeps its `sha256:` prefix.
    """
    require_digest_ref(image_digest_ref)
    return image_digest_ref.split("@", 1)[1]


def init_set_hash(packages: Sequence[str]) -> str:
    """The `io.ginn.init-set` value: SHA-256 of the init set's distinct package names, sorted and
    joined by newlines (ADR-015 Decision 2f). An empty init set (Arch) hashes the empty text."""
    return hashlib.sha256("\n".join(sorted(set(packages))).encode()).hexdigest()


def provision_create_spec(realm: RealmName, generation: int, image_digest_ref: str) -> CreateSpec:
    """The provisioning container's create (ADR-015 Decision 2b), from the verified base digest.

    It is the services-off base form: `--init`, the keep-alive `sleep infinity`, keep-id and no
    passwd entry. Its user is root (`0:0`). It has host networking, and no mount, device, tmpfs,
    capability or security option: no Realm Home, no fonts, no runtime directory, no device and
    no mitigation mount (CR-007). Its only label is the provisioning label.
    """
    require_digest_ref(image_digest_ref)
    return CreateSpec(
        name=provision_name(realm, generation),
        image_digest_ref=image_digest_ref,
        network=HOST_NETWORK,
        user=ROOT_USER,
        userns="keep-id",
        http_proxy=False,
        init=True,
        entrypoint_argv=list(KEEP_ALIVE_ENTRYPOINT),
        command=list(KEEP_ALIVE_COMMAND),
        labels={LABEL_PROVISION: provision_label(realm, generation)},
    )


def reset_create_spec(
    realm: RealmName, generation: int, image_digest_ref: str, stage_image_id: str
) -> CreateSpec:
    """The reset container's create (ADR-015 Decision 2g), from the stage image by its ID.

    `--entrypoint '[]'` with no command clears the entrypoint, so the commit of this container has a
    null Entrypoint and a null Cmd. It is created and never started. It sets no user, no
    environment and no init: the commit keeps the stage image's user and environment, and an init
    would become the committed entrypoint. It passes `--http-proxy=false`, as every create of a
    provisioning stage does, so the host's proxy variables stay out of the container config that
    the commit bakes into the derived image (ADR-004 amendment). The digest stays, because
    `CreateSpec` always carries it.
    """
    require_digest_ref(image_digest_ref)
    require_image_id_ref(stage_image_id)
    return CreateSpec(
        name=reset_name(realm, generation),
        image_digest_ref=image_digest_ref,
        image_id=stage_image_id,
        network=HOST_NETWORK,
        http_proxy=False,
        clear_entrypoint=True,
        labels={LABEL_PROVISION: provision_label(realm, generation)},
    )


def stage_commit_changes(
    *,
    image_user: str | None,
    image_env: Sequence[str],
    init_set: str,
    base_digest: str,
) -> list[str]:
    """The `--change` list of the stage commit (ADR-015 Decision 2f).

    It sets the USER of the pinned image (`0` when the image names none), and each of its `Env`
    entries as `ENV KEY=VALUE`. Then it empties `io.ginn.provision`, which removes the realm
    identity, and sets `io.ginn.init-set` and `io.ginn.base-digest`. It sets no ENTRYPOINT and no
    CMD: those changes do nothing on podman 5.8.8 (Decision 2g), and the reset does the work.

    `init_set` is `init_set_hash` of the init set, and `base_digest` is `manifest_digest` of the
    pinned reference. `image_user` and `image_env` come from the pinned image's inspect.
    """
    user = _ROOT_UID if not image_user else image_user
    if _IMAGE_USER.fullmatch(user) is None:
        raise ValueError(f"ADR-015 Decision 2f: an image user must be a name or uid:gid: {user!r}")
    return [
        f"USER {user}",
        *(f"ENV {_checked_env(entry)}" for entry in image_env),
        f"LABEL {LABEL_PROVISION}=",
        *_init_and_base_labels(init_set, base_digest),
    ]


def final_commit_changes(*, init_set: str, base_digest: str) -> list[str]:
    """The `--change` list of the final commit, from the reset container (ADR-015 Decision 2g).

    The reset keeps the stage image's user, environment and labels, so the list needs only to clear
    `io.ginn.provision`, which the reset container's own create set to `<realm>:<gen>`. It also
    restates the init-set and base-digest labels, so the final image holds all three ginn labels
    whatever the reset kept (ADR-015 Decision 2g; T7.27 item 11 checks the result on the host).
    """
    return [*_init_and_base_labels(init_set, base_digest), f"LABEL {LABEL_PROVISION}="]


def refresh_argv(distro: DistroDef) -> list[str]:
    """The refresh step of the distribution data, which the create runs first (CL-9, ADR-015 2c).

    On Arch this is the full upgrade (ADR-015 Decision 2c). It never upgrades on any other
    distribution.
    """
    return distro.argv(Verb.REFRESH)


def install_argv(distro: DistroDef, packages: Sequence[str]) -> list[str] | None:
    """The install argv of `packages`, from the distribution data, with signature checks on
    (FR-012). It is None when `packages` is empty, so the caller skips the step (ADR-015 Decision
    2c): Arch's init set is empty, and Arch still installs its Integration Package Set.

    The names are separate argv elements, each checked against the data's name pattern (DR-003,
    NFR-SEC-005). A bad name raises GINN-E-PKG-NAME-INVALID before any argv is built.
    """
    if not packages:
        return None
    return distro.argv(Verb.INSTALL, packages)


def integration_step_argvs(integration: IntegrationSet) -> list[list[str]]:
    """The locale steps of the Integration Package Set, in order, as argv lists (FR-013)."""
    return [list(step) for step in integration.steps]


def clean_argvs(distro: DistroDef) -> list[list[str]]:
    """The clean steps of the distribution data, in order, as argv lists (ADR-015 Decision 3).

    They empty the package cache and keep the directories that the next install needs. The list is
    empty when the data defines no clean verb.
    """
    return [list(step) for step in distro.clean_steps]


def removal_steps(
    realm: RealmName, generation: int, stage_image_id: str
) -> tuple[tuple[Literal["container", "image"], str], ...]:
    """The removals of a finished provisioning stage, in order (ADR-015 Decision 2h).

    The reset container goes first, by name, because it was created from the stage image. The stage
    image goes next, by its ID (Decision 13). The provisioning container goes last. Each step is
    `(kind, ref)`: `container` with a name, or `image` with an image ID.
    """
    require_image_id_ref(stage_image_id)
    return (
        ("container", reset_name(realm, generation)),
        ("image", stage_image_id),
        ("container", provision_name(realm, generation)),
    )


def _checked_env(entry: str) -> str:
    name, sep, value = entry.partition("=")
    if not sep or _ENV_NAME.fullmatch(name) is None or _CONTROL & set(entry):
        raise ValueError(f"ADR-015 Decision 2f: an image environment entry is KEY=VALUE: {entry!r}")
    # A value that holds a comma, surrounding space or a quote at either end would not reach the
    # committed ENV unchanged, so it is refused rather than rewritten.
    if (
        "," in value
        or value != value.strip()
        or value.startswith(_QUOTES)
        or value.endswith(_QUOTES)
    ):
        raise ValueError(
            "ADR-015 Decision 2f: an image environment entry is KEY=VALUE, and its VALUE holds no "
            f"comma, no surrounding space and no quote at either end: {entry!r}"
        )
    return entry


def _init_and_base_labels(init_set: str, base_digest: str) -> list[str]:
    if _HASH.fullmatch(init_set) is None:
        raise ValueError(f"io.ginn.init-set must be 64 lowercase hex digits: {init_set!r}")
    if _MANIFEST_DIGEST.fullmatch(base_digest) is None:
        raise ValueError(f"io.ginn.base-digest must be sha256:<hex>: {base_digest!r}")
    return [f"LABEL {LABEL_INIT_SET}={init_set}", f"LABEL {LABEL_BASE_DIGEST}={base_digest}"]


__all__ = [
    "DERIVED_IMAGE_PREFIX",
    "LABEL_BASE_DIGEST",
    "LABEL_INIT_SET",
    "LABEL_PROVISION",
    "PROVISION_NAME_PREFIX",
    "ROOT_USER",
    "clean_argvs",
    "derived_image_name",
    "derived_image_prefix",
    "final_commit_changes",
    "init_set_hash",
    "install_argv",
    "integration_step_argvs",
    "manifest_digest",
    "provision_create_spec",
    "provision_label",
    "provision_name",
    "provision_name_prefix",
    "refresh_argv",
    "removal_steps",
    "reset_create_spec",
    "reset_name",
    "stage_commit_changes",
    "stage_image_name",
]
