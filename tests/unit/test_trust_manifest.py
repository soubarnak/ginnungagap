# SPDX-License-Identifier: Apache-2.0
"""Trust Manifest loader: packaged data, validation rules and pull references (DR-040, FR-011)."""

import datetime as dt
import re
from pathlib import Path

import pytest

import ginn.trust as trust_package
from ginn.domain.enums import DistroId
from ginn.errors import GinnError
from ginn.trust import SignatureKind, TrustManifest, load, load_file, pull_reference
from ginn.trust.manifest import VERIFIED_SIGNATURE_KINDS, parse

PACKAGED = Path(trust_package.__file__).resolve().parent / "data" / "trust.toml"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
A = "a" * 64
B = "b" * 64

BASE = f"""\
schema = 1

[distro.arch]
image = "quay.io/archlinux/archlinux:base"
digest_kind = "platform"
digest = "sha256:{A}"
compressed_size_bytes = 139248529
estimated_installed_bytes = 410379776
measured_installed_bytes = 410385305
signature = "sigstore-unverified"
verified_on = "2026-10-12"
integration_packages = []

[distro.debian]
image = "docker.io/library/debian:stable-slim"
digest_kind = "platform"
digest = "sha256:{B}"
compressed_size_bytes = 29843347
estimated_installed_bytes = 81254400
measured_installed_bytes = 81256893
signature = "none"
verified_on = "2026-10-12"
integration_packages = ["ca-certificates", "locales"]
"""

ENTRY_FIELDS = (
    "image",
    "digest_kind",
    "digest",
    "compressed_size_bytes",
    "estimated_installed_bytes",
    "measured_installed_bytes",
    "signature",
    "verified_on",
    "integration_packages",
)


def _base_manifest() -> TrustManifest:
    return parse(BASE, "test-manifest")


def _edited(old: str, new: str) -> str:
    """Replace the first occurrence of `old` in the valid base text. The base must hold it."""
    assert old in BASE, old
    return BASE.replace(old, new, 1)


def _expect_invalid(text: str, fragment: str) -> None:
    with pytest.raises(GinnError) as info:
        parse(text, "test-manifest")
    assert info.value.code == "GINN-E-INTERNAL"
    assert info.value.exit_code == 70
    assert fragment in info.value.message, info.value.message


@pytest.mark.fr("DR-040")
def test_packaged_manifest_loads_and_matches_the_packaged_copy() -> None:
    manifest = load()
    assert manifest.schema == 1
    assert tuple(entry.key for entry in manifest.entries) == ("arch", "debian", "fedora", "kali")
    assert load_file(PACKAGED).entries == manifest.entries


@pytest.mark.fr("IR-021")
def test_packaged_manifest_covers_the_four_official_channels() -> None:
    manifest = load()
    assert {entry.key for entry in manifest.entries} == {member.value for member in DistroId}
    repositories = {entry.key: entry.repository for entry in manifest.entries}
    assert repositories == {
        "arch": "quay.io/archlinux/archlinux",
        "debian": "docker.io/library/debian",
        "fedora": "registry.fedoraproject.org/fedora-minimal",
        "kali": "docker.io/kalilinux/kali-rolling",
    }


@pytest.mark.fr("CR-007")
def test_packaged_channels_are_minimal_images() -> None:
    entries = {entry.key: entry for entry in load().entries}
    assert entries["arch"].tag == "base"
    assert entries["debian"].tag == "stable-slim"
    assert entries["fedora"].repository.endswith("/fedora-minimal")


@pytest.mark.fr("DR-040")
def test_packaged_size_fields_agree_with_the_s10_measurements() -> None:
    # S-10b measured podman's size within 6 KB of the layer tar sizes. 0.1% is a loose bound.
    for entry in load().entries:
        gap = abs(entry.measured_installed_bytes - entry.estimated_installed_bytes)
        assert gap <= entry.estimated_installed_bytes // 1000, entry.key
        assert entry.estimated_installed_bytes >= entry.compressed_size_bytes, entry.key


@pytest.mark.fr("DR-040")
def test_packaged_signature_metadata_is_recorded_as_s10_found_it() -> None:
    signatures = {entry.key: entry.signature for entry in load().entries}
    assert signatures == {
        "arch": SignatureKind.SIGSTORE_UNVERIFIED,
        "debian": SignatureKind.NONE,
        "fedora": SignatureKind.NONE,
        "kali": SignatureKind.NONE,
    }


@pytest.mark.fr("FR-011")
def test_sigstore_unverified_is_never_reported_as_verified() -> None:
    assert VERIFIED_SIGNATURE_KINDS == frozenset()
    assert SignatureKind.SIGSTORE_UNVERIFIED not in VERIFIED_SIGNATURE_KINDS
    arch = load().entry("arch")
    assert arch.signature is SignatureKind.SIGSTORE_UNVERIFIED
    assert arch.signature_verified is False
    assert all(entry.signature_verified is False for entry in load().entries)


@pytest.mark.fr("DR-040")
def test_entry_fields_parse_to_typed_values() -> None:
    entry = _base_manifest().entry("arch")
    assert entry.repository == "quay.io/archlinux/archlinux"
    assert entry.tag == "base"
    assert entry.image == "quay.io/archlinux/archlinux:base"
    assert entry.digest == f"sha256:{A}"
    assert entry.compressed_size_bytes == 139248529
    assert entry.verified_on == dt.date(2026, 10, 12)
    assert entry.integration_packages == ()
    debian = _base_manifest().entry("debian")
    assert debian.integration_packages == ("ca-certificates", "locales")


@pytest.mark.fr("FR-011")
def test_pull_reference_is_repository_at_the_platform_digest() -> None:
    for entry in load().entries:
        reference = pull_reference(entry.key)
        assert reference == f"{entry.repository}@{entry.digest}"
        name, digest = reference.split("@")
        assert DIGEST_RE.fullmatch(digest), reference
        assert ":" not in name.rsplit("/", 1)[-1], "a pull reference must not carry a tag"


@pytest.mark.fr("FR-011")
def test_pull_reference_format_from_a_manifest_file() -> None:
    assert _base_manifest().pull_reference("arch") == f"quay.io/archlinux/archlinux@sha256:{A}"
    assert _base_manifest().pull_reference("debian") == (f"docker.io/library/debian@sha256:{B}")


@pytest.mark.fr("FR-011")
def test_unknown_key_raises_image_unverified() -> None:
    with pytest.raises(GinnError) as info:
        load().entry("nosuch")
    assert info.value.code == "GINN-E-IMAGE-UNVERIFIED"
    assert info.value.exit_code == 65
    assert info.value.details == {"distro": "nosuch"}
    with pytest.raises(GinnError, match="GINN-E-IMAGE-UNVERIFIED"):
        pull_reference("nosuch")


@pytest.mark.fr("FR-011")
def test_key_lookup_is_exact_and_case_sensitive() -> None:
    with pytest.raises(GinnError, match="GINN-E-IMAGE-UNVERIFIED"):
        pull_reference("Arch")


@pytest.mark.fr("DR-040")
def test_valid_base_text_loads() -> None:
    manifest = parse(BASE, "test-manifest")
    assert [entry.key for entry in manifest.entries] == ["arch", "debian"]
    assert manifest.source == "test-manifest"


@pytest.mark.fr("DR-040")
@pytest.mark.parametrize(
    "image",
    [
        "registry.example.com:5000/base/os:1.0",
        "quay.io/archlinux/archlinux:stable-slim",
        "docker.io/library/debian:12.5",
    ],
)
def test_image_reference_with_port_or_dotted_tag_is_accepted(image: str) -> None:
    text = _edited('image = "quay.io/archlinux/archlinux:base"', f'image = "{image}"')
    entry = parse(text, "test-manifest").entry("arch")
    assert entry.image == image


@pytest.mark.fr("DR-040")
def test_schema_must_be_the_supported_version() -> None:
    _expect_invalid(_edited("schema = 1\n", "schema = 2\n"), "schema 2 is not supported")


@pytest.mark.fr("DR-040")
def test_schema_must_be_an_integer() -> None:
    _expect_invalid(_edited("schema = 1\n", 'schema = "1"\n'), "schema must be an integer")


@pytest.mark.fr("DR-040")
def test_schema_rejects_booleans() -> None:
    _expect_invalid(_edited("schema = 1\n", "schema = true\n"), "schema must be an integer")


@pytest.mark.fr("DR-040")
def test_unknown_top_level_key_is_rejected() -> None:
    _expect_invalid(_edited("schema = 1\n", "schema = 1\nowner = 'x'\n"), "unknown key(s): owner")


@pytest.mark.fr("DR-040")
def test_missing_or_empty_distro_table_is_rejected() -> None:
    _expect_invalid("schema = 1\n", "top level has no distro")
    _expect_invalid("schema = 1\n[distro]\n", "distro must list at least one distro")


@pytest.mark.fr("DR-040")
def test_distro_entry_must_be_a_table() -> None:
    _expect_invalid('schema = 1\ndistro.arch = "x"\n', "distro.arch must be a table")


@pytest.mark.fr("DR-040")
@pytest.mark.parametrize("field", ENTRY_FIELDS)
def test_every_entry_field_is_required(field: str) -> None:
    text, count = re.subn(rf"^{field} = .*\n", "", BASE, count=1, flags=re.MULTILINE)
    assert count == 1, field
    _expect_invalid(text, f"distro.arch has no {field}")


@pytest.mark.fr("DR-040")
def test_unknown_entry_field_is_rejected() -> None:
    text = _edited('signature = "none"\n', 'signature = "none"\ndigests = "x"\n')
    _expect_invalid(text, "distro.debian has unknown key(s): digests")


@pytest.mark.fr("DR-040")
def test_distro_key_must_be_lowercase() -> None:
    _expect_invalid(_edited("[distro.arch]", "[distro.Arch]"), "distro key 'Arch' must be")


@pytest.mark.fr("DR-040")
@pytest.mark.parametrize(
    ("image", "fragment"),
    [
        ("quay.io/archlinux/archlinux", "needs a tag"),
        ("archlinux:base", "must be lowercase, with an explicit registry host"),
        ("quay.io/Archlinux/archlinux:base", "must be lowercase, with an explicit registry host"),
        ("quay.io/archlinux/archlinux:-base", "is not a valid tag"),
        ("quay.io/archlinux/archlinux:base@sha256:" + A, "is not a valid tag"),
        ("quay.io/archlinux/archlinux@sha256:" + A, "must be lowercase, with an explicit"),
    ],
)
def test_image_must_be_a_tagged_registry_repository(image: str, fragment: str) -> None:
    text = _edited('image = "quay.io/archlinux/archlinux:base"', f'image = "{image}"')
    _expect_invalid(text, fragment)


@pytest.mark.fr("DR-040")
def test_digest_kind_must_be_platform() -> None:
    text = _edited('digest_kind = "platform"', 'digest_kind = "index"')
    _expect_invalid(text, 'digest_kind must be "platform"')


@pytest.mark.fr("DR-040")
@pytest.mark.parametrize(
    "digest",
    [
        "sha256:" + "A" * 64,  # upper-case hex
        "sha256:" + "a" * 63,  # too short
        "sha256:" + "a" * 65,  # too long
        "sha512:" + "a" * 64,  # wrong algorithm
        "sha256:" + "g" * 64,  # not hex
        "a" * 64,  # no prefix
    ],
)
def test_digest_must_be_sha256_and_64_lowercase_hex(digest: str) -> None:
    text = _edited(f"sha256:{A}", digest)
    _expect_invalid(text, "digest must be sha256: followed by 64 lowercase hex digits")


@pytest.mark.fr("DR-040")
@pytest.mark.parametrize(
    "value",
    ['"139248529"', "true", "0", "-5", "1.5"],
)
def test_sizes_must_be_positive_integers(value: str) -> None:
    text = _edited("compressed_size_bytes = 139248529", f"compressed_size_bytes = {value}")
    _expect_invalid(text, "compressed_size_bytes must be a positive integer of bytes")


@pytest.mark.fr("DR-040")
def test_installed_size_cannot_be_below_the_compressed_size() -> None:
    text = _edited("estimated_installed_bytes = 410379776", "estimated_installed_bytes = 1000")
    _expect_invalid(text, "estimated_installed_bytes is smaller than compressed_size_bytes")


@pytest.mark.fr("DR-040")
@pytest.mark.parametrize("name", ["sigstore", "cosign", "sigstore-verified", "NONE"])
def test_signature_must_be_a_recorded_kind(name: str) -> None:
    text = _edited('signature = "sigstore-unverified"', f'signature = "{name}"')
    _expect_invalid(text, "must be one of: none, sigstore-unverified")


@pytest.mark.fr("DR-040")
def test_old_sigstore_value_is_refused_after_the_rename() -> None:
    # Before the rename, S-10 wrote "sigstore". That name claimed verification, so it is refused.
    text = _edited('signature = "sigstore-unverified"', 'signature = "sigstore"')
    _expect_invalid(text, "must be one of: none, sigstore-unverified")


@pytest.mark.fr("DR-040")
@pytest.mark.parametrize(
    "value", ['"2026-13-01"', '"yesterday"', "20261012", "2026-10-12T00:00:00"]
)
def test_verified_on_must_be_a_date(value: str) -> None:
    text = _edited('verified_on = "2026-10-12"', f"verified_on = {value}")
    _expect_invalid(text, "verified_on must be a date such as 2026-10-12")


@pytest.mark.fr("DR-040")
def test_verified_on_accepts_an_unquoted_toml_date() -> None:
    text = _edited('verified_on = "2026-10-12"', "verified_on = 2026-10-12")
    assert parse(text, "test-manifest").entry("arch").verified_on == dt.date(2026, 10, 12)


@pytest.mark.fr("DR-040")
@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ('"ca-certificates"', "must be a list of package names"),
        ('["{user}"]', "entry '{user}' is not a package name"),
        ('["Locales"]', "entry 'Locales' is not a package name"),
        ('[""]', "entry '' is not a package name"),
        ("[1]", "entry 1 is not a package name"),
        ('["locales", "locales"]', "lists 'locales' twice"),
    ],
)
def test_integration_packages_must_be_package_names(value: str, fragment: str) -> None:
    text = _edited(
        'integration_packages = ["ca-certificates", "locales"]', f"integration_packages = {value}"
    )
    _expect_invalid(text, fragment)


@pytest.mark.fr("DR-040")
def test_integration_packages_accept_the_lang_placeholder() -> None:
    text = _edited(
        'integration_packages = ["ca-certificates", "locales"]',
        'integration_packages = ["glibc-langpack-{lang}"]',
    )
    assert parse(text, "test-manifest").entry("debian").integration_packages == (
        "glibc-langpack-{lang}",
    )


@pytest.mark.fr("DR-040")
def test_invalid_toml_is_rejected() -> None:
    _expect_invalid("schema = \n", "not valid TOML")


@pytest.mark.fr("DR-040")
def test_load_file_reports_an_unreadable_path() -> None:
    with pytest.raises(GinnError) as info:
        load_file(Path("/nonexistent/trust-manifest-for-tests.toml"))
    assert info.value.code == "GINN-E-INTERNAL"
    assert "cannot be read" in info.value.message


@pytest.mark.fr("DR-040")
def test_malformed_manifest_reports_the_source_in_details() -> None:
    with pytest.raises(GinnError) as info:
        parse("schema = 2\n", "some-source")
    assert info.value.details["source"] == "some-source"
