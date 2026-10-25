# ADR-005: Image channels and Trust Manifest

- Status: Accepted
- Date: 2026-10-12

## Context

FR-011 is a Must. Before use, each image is verified against the Trust Manifest (DR-040). A mismatch aborts creation, and no image layers remain. DR-040 says a manifest entry holds the image source and the verification material: a pinned digest, a signing key, or both. NFR-SEC-004 says images are used only when they match the manifest, and that package-manager signature checks are never disabled. IR-021 names the official channels for Arch, Debian, Fedora and Kali. CR-007 limits a realm to the official minimal image, the Integration Package Set, and requested packages.

AS-024 says a Debian realm tracks the current Debian stable release, and a Fedora realm tracks the current Fedora release at creation time. A new major release is done by rebuild (FR-021). AS-024 does not cover Kali (CL-6).

R-003 is the risk that upstream channels offer no signatures. Digest pinning then needs frequent manifest updates. Its mitigation names the update process, and says that the base is upgraded on the first update.

Measured in S-10 and S-10b (2026-10-12), on the registry metadata and by pulling each recommended image by its digest:

- Arch: `quay.io/archlinux/archlinux:base` is a single manifest, so its platform digest equals its index digest. It carries a cosign keyless signature. The ECDSA check and the payload's digest pin pass locally. The Fulcio chain, the signing identity against the publisher's policy, and Rekor inclusion were not verified (S-10 section 3). The Docker Hub mirror is one week older and unsigned, so it is not used.
- Debian: `docker.io/library/debian:stable-slim`. The pulled image is trixie (Debian 13). No signature was found. Docker Content Trust was not probed.
- Fedora: `registry.fedoraproject.org/fedora-minimal:44`. The image ships dnf5 5.4.6. No signature was found. GPG status is unknown. The full `fedora:44` image is 50 MB larger and not chosen. Fedora 45 exists, but its GA status was not verified, so it is not used.
- Kali: `docker.io/kalilinux/kali-rolling:latest`. No signature was found. `kali-last-release` is pinned to the 2026.3 branch and would not move, so it is not used.

Measured sizes match the uncompressed layer sizes within 6 KB for all five pulled images (S-10 section 4). FR-121 uses that estimate, and NFR-008 caps a realm at the official image size plus 200 MiB.

Ranked characteristics (SRS section 4), in order:

1. Performance: the manifest is read only at create and update, so it does not affect the hot path.
2. Host integrity and isolation: decides the question. Only a pinned digest is accepted, never a tag.
3. Recoverability: a refresh that moves a digest must not break existing realms. The update path verifies the new digest (see Consequences).
4. Interoperability: each distro uses the channel its users expect.
5. Usability: a refresh is one command plus a measured edit.

Assumptions and gaps, each with the step that confirms it:

- The Debian and Kali `locales` steps and the Fedora `glibc-langpack-en` step are untested. They need network access (S-10 section 8, item 1).
- Fedora GPG and Debian Docker Content Trust are unknown (S-10 section 8, items 3 and 6).
- The Arch cosign chain and Rekor inclusion are not checked. The TUF root is outside the registries (S-10 section 8, item 4).

## Decision

ginn pins one linux/amd64 platform digest per distro in `src/ginn/trust/data/trust.toml`. Each entry has `digest_kind = "platform"`. The index digest is kept in a comment for traceability. ginn pulls by `repo@sha256:...` and never by tag. An image whose digest does not match the manifest is refused (GINN-E-IMAGE-UNVERIFIED, FR-011 AC-1).

The chosen channels are:

- Arch: `quay.io/archlinux/archlinux:base`. Its signature field is `sigstore-unverified`.
- Debian: `docker.io/library/debian:stable-slim`. Signature: none.
- Fedora: `registry.fedoraproject.org/fedora-minimal:44`. Signature: none.
- Kali: `docker.io/kalilinux/kali-rolling:latest`. Signature: none. The channel is a proposal for SRS v1.1 (CL-6).

No signature kind counts as verified yet. `VERIFIED_SIGNATURE_KINDS` in `src/ginn/trust/manifest.py` is empty. So trust rests on digest pinning. The `signature` field records what was found and does not grant trust.

A channel changes only at refresh. The `image` key holds the repository and the release tag, for example `fedora-minimal:44`. A new major release is a rebuild (FR-021), not an update in place.

Refresh procedure, as built by T2.14. `tools/refresh-trust-manifest.py` is a dev-time tool, and ginn never runs it (DR-040). It uses `skopeo` with argv lists only.

1. Run it with no option, or with `--check`, to print each pinned and current digest. `--check` exits 1 when a digest moved. Nothing is written.
2. Run it with `--write` to rewrite the `digest` line of each moved entry. Comments and other fields are kept. The new file is parsed with `ginn.trust.manifest.parse` before it is written.
3. Measure the changed images by hand (S-10 section 6, steps 2 to 4 and the other fields of step 5). Pull each one by its new digest into a throwaway storage root, read `podman image inspect --format '{{.Size}}'`, and update `compressed_size_bytes`, `estimated_installed_bytes`, `measured_installed_bytes`, `signature` and `verified_on`. The tool prints a reminder for these fields.
4. For Arch, confirm the signature on each refresh. The payload's manifest digest must match the new digest. Cosign must verify the bundle against the Arch CI identity, with the Sigstore TUF root. This is a dev-time check, not ginn code.
5. Test that the file parses and every digest is `sha256:` followed by 64 hex characters. Then commit `trust.toml` and cite the refresh (FR-011, DR-040).

The proposed cadence is from S-10 section 6. It is not part of this decision. Arch about every two weeks and before a release. Debian after each point release, and at least monthly. Fedora on the first change to `latest`. Kali monthly.

R-003 mitigation. When a refreshed `trust.toml` ships, an existing realm moves to the new digest on its first update. That update goes through FR-023 and checks the new digest. This is the mechanism S-10 section 6 proposes. T8.4 builds it. Until then, existing realms keep their digest.

## Alternatives considered

- Pull by tag, with no digest pin. Rejected. A tag can move between creates, and FR-011 requires verification of the image that is used.
- Pin the index (manifest-list) digest. Rejected. It changes when another architecture or an attestation is rebuilt, which forces a refresh with no change on x86_64 (trust.toml header).
- The Arch Docker Hub mirror (`docker.io/library/archlinux:base`). Rejected. It is one week older and unsigned.
- `registry.fedoraproject.org/fedora:44`, the full image. Rejected by CR-007, which asks for the minimal image. Both images ship dnf5, so the package check does not favour the full image.
- `debian:stable`, the full image. Rejected by CR-007, for the same reason.
- `kali-last-release`. Rejected: its tag is pinned to 2026.3 and does not move. Kept as an open question in CL-6.
- Fedora 45 now. Rejected until its GA status is verified. The pin stays at 44.
- Verify signatures inside ginn now, with cosign or GPG. Not built. No verification material is wired, the Arch chain needs the TUF root, which is not a registry, and `VERIFIED_SIGNATURE_KINDS` is empty. Digest pinning is the only check until that changes.

## Consequences

- Code must pull by digest and must refuse a mismatch before any layer remains in storage (FR-011 AC-1). The digest is verified, never the tag.
- Code must not treat a `signature` value as verification. Only kinds in `VERIFIED_SIGNATURE_KINDS` count, and that set is empty.
- The Integration Package Set is not in `trust.toml`. It is in each distro's TOML (R9). Debian and Kali need `locales` and Fedora needs `glibc-langpack-en`, and those package steps are not yet tested (see Context). The Arch locale step needs a root step inside the container. S-10b tested `locale-gen` offline, as container root. The data uses `localedef`, which S-10b did not run, so that step is not verified until M4.
- Fedora needs a writable HOME for dnf under keep-id. The realm must set it (S-10 section 8, item 8).
- Sizes in `trust.toml` are the input for FR-121's disk check (ISIZE times 1.10). The stored size is the base image only. The Integration Package Set and `--with` packages are added at check time.
- The `trust.toml` header names the refresh procedure (S-10 section 6). It says which steps the tool runs (step 1, and the `digest` line of step 5) and which steps are by hand.
- Follow-ups: CL-6 (Kali channel, SRS v1.1), the M4 host tests for each distro, T8.4 (update path that checks the new digest), and the S-10 open items in section 8.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| DR-040 | The Trust Manifest has one platform digest per distro. The signature field records what was found. |
| FR-011 | Images are pulled by digest and checked before use. A mismatch refuses creation (GINN-E-IMAGE-UNVERIFIED). |
| R-003 | Digest pinning with a refresh tool is the mitigation. The first update moves an existing realm to the new digest. |
| AS-024 | Each Debian and Fedora channel carries the release tag at refresh time. A new major release is a rebuild. |
| CR-007 | The channels are the official minimal images, not the full images. |
| IR-021 | The four official channels are named here. |
| NFR-SEC-004 | Only manifest-matched images are used. Package signature checks stay on (FR-012). |
| FR-121 | The sizes in the manifest are the input for the disk check. |
| NFR-008 | The measured sizes give the budget base for the official image plus 200 MiB. |
| FR-023 | The update path that verifies a moved digest. Built in T8.4. |
| CL-6 | Kali is not covered by AS-024. The Kali channel is a proposal for SRS v1.1. |
