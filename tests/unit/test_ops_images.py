# SPDX-License-Identifier: Apache-2.0
"""The removal rule of ops/images.py for a base image that a derived image is built on (FR-019,
ADR-015 Decision 13, Open question 13).

A pinned base is shared by the realms of its distribution. A derived image of one realm is built
on it, and it carries the base's manifest digest as `io.ginn.base-digest`. podman drops the base's
record under a child (S-11), so `remove_image` keeps the base while such an image exists. The fake
keeps images and containers in memory and runs no podman.
"""

from dataclasses import replace

import pytest

from ginn.errors import make
from ginn.ops.images import remove_image
from ginn.podman.adapter import CreateSpec
from ginn.runtime.provision import LABEL_BASE_DIGEST
from tests.fakes.fake_podman import FakePodman

BASE = "registry.example/debian@sha256:" + "cd" * 32
BASE_SHA = "sha256:" + "cd" * 32
OTHER_BASE = "registry.example/arch@sha256:" + "ee" * 32
OTHER_SHA = "sha256:" + "ee" * 32


@pytest.fixture
def fake() -> FakePodman:
    return FakePodman()


def _labelled(digest: str) -> list[str]:
    return [f"LABEL {LABEL_BASE_DIGEST}={digest}"]


def _commit(fake: FakePodman, name: str, changes: list[str]) -> str:
    """Commit an image named `name` from a probe container of BASE, then remove the probe, so the
    image has no user. Return the image's ID."""
    probe = fake.create(
        CreateSpec(name=f"ginn-probe-{len(fake.images)}", image_digest_ref=BASE, network="host")
    )
    image = fake.commit(probe, name, changes=changes)
    fake.rm(probe, force=True)
    return image


@pytest.mark.fr("FR-019")
def test_a_base_with_a_derived_image_built_on_it_is_kept_and_the_note_names_that_image(
    fake: FakePodman,
) -> None:
    fake.pull(BASE)
    _commit(fake, "localhost/ginn-init-lab:g1", _labelled(BASE_SHA))
    fake.calls.clear()

    outcome, code, notes = remove_image(fake, BASE)

    assert (outcome, code) == ("kept", None)
    assert notes == [
        "image kept: realm lab's derived image localhost/ginn-init-lab:g1 is built on this base"
    ]
    assert BASE in fake.images
    assert "rmi" not in fake.ops()


@pytest.mark.fr("FR-019")
def test_the_base_goes_when_the_last_derived_image_built_on_it_has_gone(fake: FakePodman) -> None:
    fake.pull(BASE)
    _commit(fake, "localhost/ginn-init-lab:g1", _labelled(BASE_SHA))
    _commit(fake, "localhost/ginn-init-other:g1", _labelled(BASE_SHA))

    assert remove_image(fake, BASE)[0] == "kept"
    assert remove_image(fake, "localhost/ginn-init-lab:g1")[0] == "removed"
    assert remove_image(fake, BASE)[0] == "kept"  # the other realm's derived image still sits on it
    assert remove_image(fake, "localhost/ginn-init-other:g1")[0] == "removed"

    assert remove_image(fake, BASE) == ("removed", None, [])
    assert BASE not in fake.images


@pytest.mark.fr("FR-019")
def test_a_derived_image_named_by_the_repo_digest_of_the_base_keeps_it(fake: FakePodman) -> None:
    """The guard reads every manifest digest of the base, not only `ImageInfo.digest`. Here the
    base's `digest` is another value, and the label names the `sha256:` part of its repo digest."""
    fake.pull(BASE)
    fake.images[BASE] = replace(fake.images[BASE], digest="sha256:" + "11" * 32)
    _commit(fake, "localhost/ginn-init-lab:g1", _labelled(BASE_SHA))

    outcome, code, notes = remove_image(fake, BASE)

    assert (outcome, code) == ("kept", None)
    assert notes == [
        "image kept: realm lab's derived image localhost/ginn-init-lab:g1 is built on this base"
    ]
    assert BASE in fake.images


@pytest.mark.fr("FR-019")
def test_a_derived_image_built_on_another_base_does_not_keep_this_base(fake: FakePodman) -> None:
    fake.pull(BASE)
    fake.pull(OTHER_BASE)
    _commit(fake, "localhost/ginn-init-lab:g1", _labelled(OTHER_SHA))

    assert remove_image(fake, BASE) == ("removed", None, [])
    assert OTHER_BASE in fake.images


@pytest.mark.fr("FR-019")
@pytest.mark.parametrize("changes", [[], ["LABEL io.ginn.base-digest="]], ids=["no-label", "empty"])
def test_a_derived_image_with_no_base_digest_value_does_not_keep_the_base(
    fake: FakePodman, changes: list[str]
) -> None:
    fake.pull(BASE)
    _commit(fake, "localhost/ginn-init-lab:g1", changes)

    assert remove_image(fake, BASE) == ("removed", None, [])


@pytest.mark.fr("FR-019")
def test_an_image_outside_the_derived_prefix_does_not_keep_the_base(fake: FakePodman) -> None:
    fake.pull(BASE)
    _commit(fake, "localhost/someone-else:latest", _labelled(BASE_SHA))

    assert remove_image(fake, BASE) == ("removed", None, [])


@pytest.mark.fr("FR-019")
def test_a_base_whose_digest_cannot_be_read_is_kept_and_nothing_is_removed(
    fake: FakePodman, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.pull(BASE)

    def unreadable(ref: str) -> None:
        raise make("GINN-E-INTERNAL")

    monkeypatch.setattr(fake, "image_inspect", unreadable)

    outcome, code, notes = remove_image(fake, BASE)

    assert (outcome, code) == ("kept", None)
    assert notes == [
        "image kept: its digest cannot be read, so a derived image built on it is not ruled out"
    ]
    assert BASE in fake.images
    assert "rmi" not in fake.ops()


@pytest.mark.fr("FR-019")
def test_a_container_that_uses_the_base_keeps_it_as_before(fake: FakePodman) -> None:
    fake.pull(BASE)
    fake.create(CreateSpec(name="ginn-lab-g1", image_digest_ref=BASE, network="host"))

    outcome, code, notes = remove_image(fake, BASE)

    assert (outcome, code) == ("kept", None)
    assert notes[0].startswith("image kept: container ")
    assert notes[0].endswith(" uses it")
    assert BASE in fake.images


@pytest.mark.fr("FR-019")
def test_a_failed_rmi_keeps_the_base_with_the_code(fake: FakePodman) -> None:
    fake.pull(BASE)
    fake.fail_ops = {"rmi": "GINN-E-INTERNAL"}

    assert remove_image(fake, BASE) == (
        "kept",
        "GINN-E-INTERNAL",
        ["image kept: rmi failed with GINN-E-INTERNAL"],
    )


@pytest.mark.fr("FR-019")
def test_an_absent_image_is_reported_absent_and_nothing_is_removed(fake: FakePodman) -> None:
    assert remove_image(fake, BASE) == ("absent", None, [])
    assert "rmi" not in fake.ops()
