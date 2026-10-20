# SPDX-License-Identifier: Apache-2.0
"""Image IDs in CreateSpec, the reset create's empty entrypoint, and FakePodman's commit (ADR-015).

These tests build argv and drive FakePodman. None of them runs podman.
"""

import pytest

from ginn.errors import GinnError
from ginn.podman.adapter import CreateSpec, require_image_id_ref
from tests.fakes.fake_podman import FakePodman

DIGEST = (
    "docker.io/library/alpine@sha256:"
    "d56c381f961d307a21b3ca004cf1e3910f106644aefb1f43e654c8a56c4fd395"
)
HEX = "320994c3b997e2ec6433f717f153e108023c5bec8fefa8d76b83451d16d05ea8"
IMAGE_ID = HEX
PREFIXED_ID = f"sha256:{HEX}"
STAGE = "localhost/ginn-init-arch:g1-stage"


@pytest.mark.fr("FR-011")
@pytest.mark.parametrize("ref", ["a" * 64, IMAGE_ID])
def test_an_image_id_is_64_lowercase_hex_digits(ref: str) -> None:
    require_image_id_ref(ref)


@pytest.mark.fr("FR-011")
@pytest.mark.parametrize(
    "ref",
    [
        PREFIXED_ID,
        "sha512:" + HEX,
        HEX.upper(),
        HEX[:-1],
        HEX + "0",
        IMAGE_ID + "\n",
        STAGE,
        "",
    ],
)
def test_a_bare_tagged_or_short_image_id_is_refused(ref: str) -> None:
    with pytest.raises(ValueError, match="ADR-015"):
        require_image_id_ref(ref)


@pytest.mark.fr("FR-011")
def test_the_image_argument_is_the_image_id_when_it_is_set() -> None:
    spec = CreateSpec(
        name="ginn-arch-g1", image_digest_ref=DIGEST, network="host", image_id=IMAGE_ID
    )
    argv = spec.to_argv()
    assert argv[-1] == IMAGE_ID
    assert DIGEST not in argv


@pytest.mark.fr("FR-011")
def test_the_image_argument_is_the_digest_reference_when_no_id_is_set() -> None:
    argv = CreateSpec(name="ginn-arch-g1", image_digest_ref=DIGEST, network="host").to_argv()
    assert argv[-1] == DIGEST


@pytest.mark.fr("FR-011")
def test_the_digest_reference_stays_required_beside_an_image_id() -> None:
    spec = CreateSpec(
        name="ginn-arch-g1", image_digest_ref="alpine:3", network="host", image_id=IMAGE_ID
    )
    with pytest.raises(ValueError, match="FR-011"):
        spec.to_argv()


@pytest.mark.fr("FR-011")
def test_a_prefixed_image_id_is_refused_before_argv_is_built() -> None:
    spec = CreateSpec(
        name="ginn-arch-g1", image_digest_ref=DIGEST, network="host", image_id=PREFIXED_ID
    )
    with pytest.raises(ValueError, match="ADR-015"):
        spec.to_argv()


@pytest.mark.fr("FR-011")
def test_the_reset_create_clears_the_entrypoint_and_names_no_command() -> None:
    spec = CreateSpec(
        name="ginn_provision_arch_g1_reset",
        image_digest_ref=DIGEST,
        network="host",
        image_id=IMAGE_ID,
        labels={"io.ginn.provision": "arch:1"},
        clear_entrypoint=True,
    )
    assert spec.to_argv() == [
        "create",
        "--name",
        "ginn_provision_arch_g1_reset",
        "--network",
        "host",
        "--label",
        "io.ginn.provision=arch:1",
        "--entrypoint",
        "[]",
        IMAGE_ID,
    ]


@pytest.mark.fr("FR-011")
def test_clear_entrypoint_refuses_an_entrypoint_argv() -> None:
    spec = CreateSpec(
        name="ginn_provision_arch_g1_reset",
        image_digest_ref=DIGEST,
        network="host",
        clear_entrypoint=True,
        entrypoint_argv=["sleep"],
    )
    with pytest.raises(ValueError, match="clear_entrypoint"):
        spec.to_argv()


@pytest.mark.fr("FR-011")
def test_clear_entrypoint_refuses_a_command() -> None:
    spec = CreateSpec(
        name="ginn_provision_arch_g1_reset",
        image_digest_ref=DIGEST,
        network="host",
        clear_entrypoint=True,
        command=["infinity"],
    )
    with pytest.raises(ValueError, match="clear_entrypoint"):
        spec.to_argv()


@pytest.mark.fr("FR-011")
def test_an_empty_entrypoint_list_still_emits_no_entrypoint() -> None:
    spec = CreateSpec(
        name="ginn-arch-g1", image_digest_ref=DIGEST, network="host", entrypoint_argv=[]
    )
    assert "--entrypoint" not in spec.to_argv()


@pytest.mark.fr("FR-011")
@pytest.mark.parametrize("name", ["ginn_provision_arch_g1", "ginn_provision_arch_g12_reset"])
def test_a_provisioning_name_passes_the_name_rule(name: str) -> None:
    argv = CreateSpec(name=name, image_digest_ref=DIGEST, network="host").to_argv()
    assert argv[1:3] == ["--name", name]


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("name", ["-ginn_provision_arch_g1", "ginn provision", "x\n"])
def test_a_name_podman_could_read_as_an_option_or_split_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="not a valid podman name"):
        CreateSpec(name=name, image_digest_ref=DIGEST, network="host").to_argv()


@pytest.mark.fr("FR-011")
def test_the_fake_commit_stores_the_image_under_its_name_with_the_label_changes() -> None:
    fake = FakePodman()
    cid = fake.create(
        CreateSpec(name="ginn_provision_arch_g1", image_digest_ref=DIGEST, network="host")
    )
    image_id = fake.commit(
        cid,
        STAGE,
        changes=["LABEL io.ginn.provision=", "LABEL io.ginn.base-digest=sha256:abc"],
    )
    assert len(image_id) == 64
    require_image_id_ref(image_id)
    assert fake.image_id(STAGE) == image_id
    stored = fake.images_by_name("localhost/ginn-init-arch:")
    assert list(stored) == [STAGE]
    assert stored[STAGE].labels == {"io.ginn.provision": "", "io.ginn.base-digest": "sha256:abc"}
    assert fake.images_by_name("localhost/other:") == {}


@pytest.mark.fr("FR-011")
def test_the_fake_refuses_an_empty_prefix_and_an_unknown_image_id() -> None:
    fake = FakePodman()
    with pytest.raises(ValueError, match="prefix"):
        fake.images_by_name("")
    with pytest.raises(GinnError) as info:
        fake.image_id(IMAGE_ID)
    assert info.value.code == "GINN-E-INTERNAL"


@pytest.mark.fr("FR-011")
def test_the_fake_creates_from_an_image_id_it_holds_and_refuses_one_it_does_not() -> None:
    fake = FakePodman()
    with pytest.raises(GinnError) as refused:
        fake.create(
            CreateSpec(
                name="ginn-arch-g1", image_digest_ref=DIGEST, network="host", image_id=IMAGE_ID
            )
        )
    assert refused.value.code == "GINN-E-INTERNAL"
    source = fake.create(
        CreateSpec(name="ginn_provision_arch_g1", image_digest_ref=DIGEST, network="host")
    )
    image_id = fake.commit(source, STAGE)
    cid = fake.create(
        CreateSpec(name="ginn-arch-g1", image_digest_ref=DIGEST, network="host", image_id=image_id)
    )
    assert fake.inspect(cid).image == image_id


@pytest.mark.fr("FR-011")
def test_the_fake_diff_returns_the_rows_a_test_sets() -> None:
    fake = FakePodman()
    cid = fake.create(
        CreateSpec(name="ginn_provision_arch_g1", image_digest_ref=DIGEST, network="host")
    )
    assert fake.diff(cid) == []
    fake.diffs[cid] = [("A", "/usr/bin/htop"), ("C", "/var/lib/pacman")]
    assert fake.diff(cid) == [("A", "/usr/bin/htop"), ("C", "/var/lib/pacman")]


@pytest.mark.fr("FR-011")
def test_the_fake_refuses_a_commit_onto_a_name_that_holds_an_image() -> None:
    fake = FakePodman()
    cid = fake.create(
        CreateSpec(name="ginn_provision_arch_g1", image_digest_ref=DIGEST, network="host")
    )
    first = fake.commit(cid, STAGE, changes=["LABEL io.ginn.provision="])
    with pytest.raises(GinnError) as info:
        fake.commit(cid, STAGE, changes=["LABEL io.ginn.provision=arch:1"])
    assert info.value.code == "GINN-E-INTERNAL"
    assert fake.image_id(STAGE) == first
    stored = fake.images_by_name("localhost/ginn-init-arch:")
    assert stored[STAGE].labels == {"io.ginn.provision": ""}


@pytest.mark.fr("FR-011")
@pytest.mark.parametrize("op", ["image_id", "images_by_name", "diff"])
def test_the_fake_fails_a_read_once_when_a_test_injects_a_failure_for_it(op: str) -> None:
    fake = FakePodman()
    cid = fake.create(
        CreateSpec(name="ginn_provision_arch_g1", image_digest_ref=DIGEST, network="host")
    )
    fake.commit(cid, STAGE)
    reads = {
        "image_id": lambda: fake.image_id(STAGE),
        "images_by_name": lambda: fake.images_by_name("localhost/"),
        "diff": lambda: fake.diff(cid),
    }
    fake.fail_ops[op] = "GINN-E-INTERNAL"
    with pytest.raises(GinnError) as info:
        reads[op]()
    assert info.value.code == "GINN-E-INTERNAL"
    assert op not in fake.fail_ops
    assert reads[op]() is not None
