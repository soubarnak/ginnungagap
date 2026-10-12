# Clarification register

This register records the places where `GINN-SRS-001-v1.0.md` is unclear, or where the accepted decisions need the SRS to say more. Each entry gives the requirement IDs, the issue, a proposed wording for the SRS author, and a status.

The SRS is not edited here. Every entry has the status "open, for SRS v1.1" unless it says otherwise. An entry closes when the SRS author writes the wording into the SRS, or decides against it.

| ID | Topic | Requirement IDs | Status |
|---|---|---|---|
| CL-1 | Runtime directory for services-on realms | DR-010, FR-081, FR-084, DEP-3 | confirmed by S-04s (simulated); SRS wording open for v1.1 |
| CL-2 | Docker daemon in a realm (FR-085) | FR-085, AS-006, NFR-SEC-002 | open, for SRS v1.1 (deferred with FR-085) |
| CL-3 | Realm root maps to a subordinate UID | AS-007, CR-003, NFR-SEC-001 | open, for SRS v1.1 |
| CL-4 | unused | none | unused |
| CL-5 | Session sockets after re-login | IR-025, FR-084 | resolved by S-08s/S-08t (simulated); host confirmation in T7.8; SRS wording open for v1.1 |
| CL-6 | Kali image channel | AS-024, IR-021, FR-011 | open, for SRS v1.1 |
| CL-7 | Distribution ID is an enum | DR-005, NFR-060, DR-003 | open, for SRS v1.1 |
| CL-8 | Development-tier setcap step and libcap package | FR-013, CR-007, FR-021, FR-062, NFR-SEC-002 | withdrawn (2026-10-12, D2) |
| CL-9 | Create-time refresh and the Integration Package Set | FR-013, FR-023, CR-007 | open, for SRS v1.1 |
| CL-10 | Create-time mitigations for services-on realms | CR-007, FR-013, NFR-003, FR-080 | open, for SRS v1.1 (needed only if S-05s fails) |

## CL-1: runtime directory for services-on realms

- Requirement IDs: DR-010, FR-081, FR-084, DEP-3.
- Issue: DR-010 names `$XDG_RUNTIME_DIR/ginn` as an Owned Path. Under elogind, `$XDG_RUNTIME_DIR` belongs to a login session (DEP-3). It is removed at logout unless lingering is enabled. This is standard logind behaviour and was not measured here. A services-on realm must keep running after logout (FR-081, FR-084), so it needs a runtime directory that does not depend on a session.
- Proposed wording: DR-010 names `/run/ginn/<uid>`, created by the supervisor run script at each start, in place of `$XDG_RUNTIME_DIR/ginn`.
- How ginn works now: ginn uses `/run/ginn/<uid>`. The supervisor run script creates it at each start, as root (`hostsetup/runit_system.py`, ADR-004). `/run` is a tmpfs, so a directory made once at setup would vanish at reboot. The deviation from DR-010 stands until this clarification is written into the SRS.
- Evidence: the real-logout run S-04 is invalid. The host created no elogind session for the test login (`spikes/results/S-04.md`). The simulation S-04s (same file, section "Simulated logout") shows that a runroot and tmpdir outside the session directory survive a simulated logout. Inside the session directory they do not survive: podman runs a "system restart" state refresh at the first call after login, and it loses the container. Variant A used `/dev/shm`, not `/run/ginn/<uid>`. ADR-004 has the addendum.
- Status: confirmed by S-04s (simulated). The result assumes elogind `KillUserProcesses=no`, the Void default (commented out in `/etc/elogind/logind.conf`). The doctor check in T3.6 fails when the effective value is `yes` (ADR-004). The SRS wording is still open for v1.1, so this entry stays open until the SRS author writes it.

## CL-2: Docker daemon in a realm (FR-085)

- Requirement IDs: FR-085 (Should), AS-006, NFR-SEC-002.
- Issue: FR-085 asks for `docker run --rm hello-world` inside a services-on Development realm. A realm with services on shares the host network namespace (AS-006). S-07 expects, without testing it, that creating bridges in that namespace needs CAP_NET_ADMIN there, and NFR-SEC-002 does not grant it. A rootless dockerd would need a full ID map inside the realm. The single-ID design of ADR-006 (amended, S-07e) gives one ID only, and dockerd was not tested on it.
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
- Proposed wording: decided by S-08s, not by the SRS. ginn rebinds the stale sockets in place (ADR-008). Suggested SRS wording: "A services-on realm keeps running after re-login, and its session sockets are rebound on the next enter or launch."
- Evidence: the real-logout run S-08 is invalid for the same reason as S-04: no elogind session for the test login (`spikes/results/S-08.md`). The simulation S-08s (same file, section "Simulated logout") shows a stale socket after the simulated logout. An in-place rebind under `podman unshare` (umount2, then move_mount) gives a live socket with no restart. The keep-id case is the simulation S-08t (same file, section "S-08t: rebind under keep-id"). The caveats are in ADR-008.
- Status: resolved by S-08s/S-08t (simulated); host confirmation in T7.8. The resolution is the rebind, through the mount namespace only (variant b, S-08s, S-08t, ADR-008). Restart-required was rejected, because services-on realms exist to keep their services running across logout (FR-081, FR-084). The SRS wording is still open for v1.1.

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
- Former position (withdrawn): ginn applies these steps after create, only in the Development tier. They are the same kind of step as installing the Integration Package Set. The base image is not modified: it stays the official image at its pinned digest. The steps are recorded so that rebuild replays them. This does not comply with FR-013 or CR-007 as they are written, and the register does not claim that it does.
- Former proposed wording (no longer needed): amend FR-013 and CR-007 to allow a Development-tier set of post-create steps, named in `docs/capabilities.md`. Add the replay of those steps to FR-021. No other route is known yet that avoids both a file change and an extra package.
- Evidence: S-07d F1 and G3 (the helper change), and S-07d used `libcap2-bin` on Debian stable only.
- Status: withdrawn (2026-10-12, D2). The raiser withdraws it. The single-ID design (ADR-006, amended) needs no setcap step, no libcap package and no helper file change (S-07e E1o, S-07e2 E1d-u2). The realm user is created with `useradd -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0`, which is part of realm creation (T4.4). The Realm Home config files are ginn-owned data. So the setcap and libcap steps no longer conflict with FR-013 AC-1 or CR-007, and the former position and wording above are not needed. FR-062 AC-3 passes with the decided set (S-07f case A, scope in ADR-006).
- Open point, not part of this withdrawal: the image must contain podman and its helpers. A realm can get them only as requested packages, which CR-007 allows. S-07e2 did not decide how the request is made. That is for the user.
- Open point, a CR-007 question: the inner `storage.conf` sets `mount_program = "/usr/bin/fuse-overlayfs"`. Whether fuse-overlayfs comes from the host package or must be in the realm image is not decided. fuse-overlayfs stays in the design and in ginn's packaging template.

## CL-9: create-time refresh and the Integration Package Set

- Requirement IDs: FR-013, FR-023, CR-007.
- Issue: FR-013 AC-1 and FR-023 AC-1 do not say whether creating a realm may bring the image's packages up to date. CR-007 lists realm contents as the official image, the Integration Package Set and the requested packages, and does not say which versions of the image's packages a new realm keeps. On Arch the first install needs a current sync database, and a full synchronisation at creation is the only supported way to get one (ADR-011). Debian, Kali and Fedora run a metadata refresh first, which changes no installed package.
- Proposed wording: "The Integration Package Set is installed against package versions current at creation; on rolling distributions creation includes a full synchronisation of the image's packages."
- How ginn works now: creation runs `refresh` once, then installs the Integration Package Set, on every distribution. On Arch the refresh is the full upgrade. ADR-011 gives the reading under which this complies with the SRS as written. The register does not claim compliance, because the wording is open.
- Evidence: `spikes/results/S-10.md` section 5 and section 7 (the Arch image ships `ca-certificates`). S-10 does not record the image's sync database or package lists. M4 records both, and ADR-011 lists the check.

## CL-10: create-time mitigations for services-on realms

- Requirement IDs: CR-007, FR-013, NFR-003, FR-080.
- Issue: the a+b+d mitigations of ADR-007 change the realm's /etc: a journald configuration file, symlinks that mask ten units, and, for the unit paths the Debian image lacks, the placeholder files that a bind mount creates. CR-007 limits realm contents to the official image, the Integration Package Set and requested packages with their declared dependencies. FR-013 is read the same way. Config files and mask links are not on that list.
- Proposed wording: "CR-007 does not count bind mounts and environment that ginn sets at realm creation. Where a services-on mitigation needs a file in the realm's /etc, ginn may write it, and the file is listed in docs/capabilities.md."
- How ginn works now: the product form uses create-time mounts and environment: a tmpfs at `/var/log/journal`, `/dev/null` bind mounts over the unit paths, and `SYSTEMD_LOG_LEVEL=warning` in the environment (ADR-007). The image and its /etc are not changed, except for the placeholder files a bind creates where the image has no unit file. S-05s checks that before T7.3.
- Evidence: S-05r measured the a+b+d PASS with the mitigations as image layers (`spikes/s05/Dockerfile.mit`). Those layers write the journald file and the mask symlinks into /etc. S-05s has not run, so the create-time forms are unmeasured.
- Status: open, for SRS v1.1. The entry is needed only if S-05s shows that the create-time forms fail. The fallback writes files into the realm's /etc, and CR-007 then needs this wording to allow it.
