# Clarification register

This register records the places where `GINN-SRS-001-v1.0.md` is unclear, or where the accepted decisions need the SRS to say more. Each entry gives the requirement IDs, the issue, a proposed wording for the SRS author, and a status.

The SRS is not edited here. Every entry has the status "open, for SRS v1.1" unless it says otherwise. An entry closes when the SRS author writes the wording into the SRS, or decides against it.

| ID | Topic | Requirement IDs | Status |
|---|---|---|---|
| CL-1 | Runtime directory for services-on realms | DR-010, FR-081, FR-084, DEP-3 | open, for SRS v1.1 |
| CL-2 | Docker daemon in a realm (FR-085) | FR-085, AS-006, NFR-SEC-002 | open, for SRS v1.1 (deferred with FR-085) |
| CL-3 | Realm root maps to a subordinate UID | AS-007, CR-003, NFR-SEC-001 | open, for SRS v1.1 |
| CL-4 | unused | none | unused |
| CL-5 | Session sockets after re-login | IR-025, FR-084 | open, for SRS v1.1 |
| CL-6 | Kali image channel | AS-024, IR-021, FR-011 | open, for SRS v1.1 |
| CL-7 | Distribution ID is an enum | DR-005, NFR-060, DR-003 | open, for SRS v1.1 |
| CL-8 | Development-tier setcap step and libcap package | FR-013, CR-007, FR-021, FR-062, NFR-SEC-002 | open, for SRS v1.1 |

## CL-1: runtime directory for services-on realms

- Requirement IDs: DR-010, FR-081, FR-084, DEP-3.
- Issue: DR-010 names `$XDG_RUNTIME_DIR/ginn` as an Owned Path. Under elogind, `$XDG_RUNTIME_DIR` belongs to a login session (DEP-3). It is removed at logout unless lingering is enabled. This is standard logind behaviour and was not measured here. A services-on realm must keep running after logout (FR-081, FR-084), so it needs a runtime directory that does not depend on a session.
- Proposed wording: DR-010 names `/run/ginn/<uid>`, created by Host Setup, in place of `$XDG_RUNTIME_DIR/ginn`.
- How ginn works now: ginn uses `/run/ginn/<uid>`, created by Host Setup (ADR-004). The deviation from DR-010 stands until this clarification is written into the SRS.
- Evidence: S-04 (roadmap M1) is the confirming spike. It has not run yet, and no S-04 result exists.

## CL-2: Docker daemon in a realm (FR-085)

- Requirement IDs: FR-085 (Should), AS-006, NFR-SEC-002.
- Issue: FR-085 asks for `docker run --rm hello-world` inside a services-on Development realm. A realm with services on shares the host network namespace (AS-006). S-07 expects, without testing it, that creating bridges in that namespace needs CAP_NET_ADMIN there, and NFR-SEC-002 does not grant it. A rootless dockerd would need the newuidmap set from ADR-006, which is not tested for dockerd.
- Proposed wording: FR-085 names a rootless-mode dockerd. A rootful dockerd in a realm is out of scope for v1.
- Evidence: S-07 Part B was skipped. Its expectations are not tested (S-07 Part B). FR-085 is deferred with its milestone, and S-07 Part B is re-run after M7, when services are on.

## CL-3: realm root maps to a subordinate UID

- Requirement IDs: AS-007, CR-003, NFR-SEC-001.
- Issue: AS-007 says realm root "maps to the unprivileged host user". With `--userns=keep-id`, realm root (container UID 0) maps to the first subordinate UID of the host user. The host user keeps its own UID for the realm user (S-07 environment facts: container 0 maps to host 100000, container 1000 to host 1000). The mapping is still unprivileged on the host. The wording does not match.
- Proposed wording: AS-007 reads "realm root maps to an unprivileged subordinate UID of the host user".
- Evidence: S-07 environment facts. The mapping was observed on the reference host.

## CL-4: unused

No CL-4 was raised. The number is kept so that the register does not renumber.

## CL-5: session sockets after re-login

- Requirement IDs: IR-025, FR-084.
- Issue: session sockets (display, audio, D-Bus) are bound when a realm starts. After logout they go stale, while a services-on realm keeps running. A realm that autostarts (FR-084) would then hold dead sockets after the next login.
- Proposed wording: to be decided after S-08. Either the session sockets are rebound on login, or the SRS states that a services-on realm needs a restart after re-login.
- Evidence: S-08 (roadmap M1) decides this, by rebinding through `open_tree` and `move_mount`, or by a documented restart. S-08 has not run.

## CL-6: Kali image channel

- Requirement IDs: AS-024, IR-021, FR-011.
- Issue: AS-024 covers Debian and Fedora only. Kali has two channels: `kali-rolling`, which moves, and `kali-last-release`, which is pinned to the 2026.3 branch and does not move.
- Proposed wording: AS-024 adds "a Kali realm tracks `kali-rolling`".
- How ginn works now: Kali uses `docker.io/kalilinux/kali-rolling`, pinned by digest in the Trust Manifest (ADR-005).
- Evidence: S-10 section 2 and section 8, item 5.

## CL-7: distribution ID is an enum

- Requirement IDs: DR-005, NFR-060, DR-003.
- Issue: DR-005 defines the distribution ID as an enum: `arch`, `debian`, `fedora`, `kali`. NFR-060 requires that a new distribution is added by data only, with no change to lifecycle code. ginn derives the set of distribution IDs from the shipped distribution definitions (commit bb561cf, R10). The enum and NFR-060 conflict.
- Proposed wording: DR-005 reads "one of the shipped distribution definitions".

## CL-8: Development-tier setcap step and libcap package

- Requirement IDs: FR-013, CR-007, FR-021, FR-062, NFR-SEC-002.
- Issue: the Development tier needs file capabilities on `newuidmap` and `newgidmap` inside the realm (ADR-006). That needs the setcap tool, from `libcap2-bin` on Debian and Kali, or `libcap` on Arch and Fedora. Both changes are made after create. FR-013 limits the Integration Package Set to CA certificates and locale data. It is fixed per distribution, and AC-1 allows no other package additions. A Development realm would show a libcap package in its package diff. CR-007 limits realm contents to the official image, the Integration Package Set, and requested packages with their declared dependencies. The libcap package fits none of these. The changed helper files are also a change to realm contents. FR-021 names only the Install Manifest for rebuild, so the SRS does not say that these steps are replayed.
- Position: ginn applies these steps after create, only in the Development tier. They are the same kind of step as installing the Integration Package Set. The base image is not modified: it stays the official image at its pinned digest. The steps are recorded so that rebuild replays them. This does not comply with FR-013 or CR-007 as they are written, and the register does not claim that it does.
- Proposed wording: amend FR-013 and CR-007 to allow a Development-tier set of post-create steps, named in `docs/capabilities.md`. Add the replay of those steps to FR-021. No other route is known yet that avoids both a file change and an extra package.
- Evidence: S-07d F1 and G3 (the helper change), and S-07d used `libcap2-bin` on Debian stable only.
