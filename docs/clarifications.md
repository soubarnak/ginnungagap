# Clarification register

This register records the places where `GINN-SRS-001-v1.0.md` is unclear, or where the accepted decisions need the SRS to say more. Each entry gives the requirement IDs, the issue, a proposed wording for the SRS author, and a status.

The SRS is not edited here. Every entry has the status "open, for SRS v1.1" unless it says otherwise. An entry closes when the SRS author writes the wording into the SRS, or decides against it.

| ID | Topic | Requirement IDs | Status |
|---|---|---|---|
| CL-1 | Runtime directory for services-on realms | DR-010, FR-081, FR-084, DEP-3 | confirmed by S-04s (simulated); SRS wording open for v1.1 |
| CL-2 | Docker daemon in a realm (FR-085) | FR-085, AS-006, NFR-SEC-002 | open, for SRS v1.1 (deferred with FR-085) |
| CL-3 | Realm root maps to a subordinate UID | AS-007, CR-003, NFR-SEC-001 | open, for SRS v1.1 |
| CL-4 | unused | none | unused |
| CL-5 | Session sockets after re-login | IR-025, FR-084 | resolved by S-08s/S-08t (simulated); host confirmation in T4.9 (attach gate) and T7.8 (real logout); SRS wording open for v1.1 |
| CL-6 | Kali image channel | AS-024, IR-021, FR-011 | open, for SRS v1.1 |
| CL-7 | Distribution ID is an enum | DR-005, NFR-060, DR-003 | open, for SRS v1.1 |
| CL-8 | Development-tier setcap step and libcap package | FR-013, CR-007, FR-021, FR-062, NFR-SEC-002 | withdrawn (2026-10-12, D2) |
| CL-9 | Create-time refresh and the Integration Package Set | FR-013, FR-023, CR-007 | open, for SRS v1.1 |
| CL-10 | Create-time mitigations for services-on realms | CR-007, FR-013, NFR-003, FR-080 | open, for SRS v1.1 (needed only if S-05s fails) |
| CL-11 | Host Setup undo when a path is shared, or its state is gone | FR-003 | open, for SRS v1.1; the plan proceeds under the proposed wording |
| CL-12 | Identical paths and fixed names for grants attached after start | FR-065, FR-061, FR-066, IR-025 | open, for SRS v1.1; the plan proceeds under the proposed wording |
| CL-13 | Doctor readiness: "all checks passing" and the disk thresholds | FR-001, FR-007 | open, for SRS v1.1; the plan proceeds under the proposed wording |
| CL-14 | ginn's own sockets as typed internal grants | DR-004, FR-067, FR-068, NFR-SEC-006, NFR-SEC-008 | open, for SRS v1.1; the plan proceeds under the proposed wording |
| CL-15 | An abstract-socket session bus with the host network (FR-026) | FR-068, FR-026, AS-012 | open, for SRS v1.1 |

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
- Status: resolved by S-08s/S-08t (simulated); host confirmation in T4.9 (the attach gate, which covers the variable grant attachment of the ADR-008 amendment of 2026-10-12) and in T7.8 (the real logout run). The resolution is the rebind, through the mount namespace only (variant b, S-08s, S-08t, ADR-008). Restart-required was rejected, because services-on realms exist to keep their services running across logout (FR-081, FR-084). The SRS wording is still open for v1.1.

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
- Note (2026-10-13, T4.9b): the `useradd -K` text in this entry is superseded. The realm user is the keep-id passwd entry, sent with `--passwd-entry`, and no `useradd` runs (ADR-006 amendment of 2026-10-13, T4.9b). The withdrawal stands.
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
- Proposed wording: "CR-007 does not count bind mounts and environment that ginn sets at realm creation, or the mountpoint stubs that the container runtime creates for those mounts. Where a services-on mitigation needs a file in the realm's /etc, ginn may write it, and the file is listed in docs/capabilities.md."
- How ginn works now: the product form uses create-time mounts and environment: a read-only bind of a ginn-owned journald drop-in at `/etc/systemd/journald.conf.d/ginn.conf` (this replaces the tmpfs form of D5), `/dev/null` bind mounts over the unit paths, and `SYSTEMD_LOG_LEVEL=warning` in the environment (ADR-007). ginn writes no file into the realm's /etc. The image's /etc changes only where podman creates a mountpoint stub for a bind whose target the image lacks. That write is podman's, not ginn's. S-05s checks the mounted drop-in before T7.3 (ADR-007).
- Evidence: S-05r measured the a+b+d PASS with the mitigations as image layers (`spikes/s05/Dockerfile.mit`). Those layers write the journald file and the mask symlinks into /etc. S-05s has not run, so the create-time forms are unmeasured.
- Status: open, for SRS v1.1. The entry is needed only if S-05s shows that the create-time forms fail. The fallback writes files into the realm's /etc, and CR-007 then needs this wording to allow it.

## CL-11: Host Setup undo when a path is shared, or its state is gone

- Requirement IDs: FR-003 (AC-1).
- Issue: AC-1 says that every path in the Host Setup Record is "restored to its pre-setup content or removed". That cannot hold for these cases:
  - the subid files, after another user appended a line, because restoring the whole file would drop that line;
  - the subid files, after setup appended a line terminator to an admin's last line that had none, because undo removes only its own line and never writes the bytes back, so the file is reported as different;
  - an admin-widened subid line, which undo leaves in place, because it is not the line that setup wrote;
  - a shared directory that another user's entries still use, because `rmdir` fails on a non-empty directory;
  - root `cgroup.subtree_control` after a reboot, when the cgroup state is gone, and while other users remain, when undo leaves the controllers in place for them.
- Proposed wording: "every path in the Host Setup Record is restored, removed, or has setup's own change reversed; a shared path still used by another record is left and reported."
- How ginn works now: ADR-013 Decision 15 reverses setup's change instead of writing bytes back. Subid lines are removed by exact match (i). Root controllers are disabled only for the last record, and only in the same boot (ii). Shared directories are removed only when their pre-image is absent and they are empty (iii). Undo prints each path it leaves, and why. The register does not claim that AC-1 holds as written.
- Evidence: ADR-013 Decisions 6, 15 and 17. The shared record followed the eighth review (MJ1), which showed that per-user records lost shared pre-images when users were undone out of order. The cases above follow from the same rules. G1 (`spikes/results/G1-host-setup-roundtrip.md`, 2026-10-12, commit 4a3b508) ran one setup and one undo on a Void host. Undo left 4 paths in place and restored both subid files byte for byte. The subid lines it left were the user's own line from before setup, which is not an admin edit. The reason text for that case was changed after the run.
- Status: open, for SRS v1.1. The SRS author decides the wording. The plan proceeds on the assumption that the proposed wording is accepted, and T3.5 is built to it.

## CL-12: identical paths and fixed names for grants attached after start

- Requirement IDs: FR-065, FR-061, FR-066, IR-025.
- Issue: FR-065 says every shared path appears inside the realm at the same absolute path as on the host. ADR-008 (amendment of 2026-10-12) attaches the variable grants after `podman start`, not at create. A grant is then attached at each start, enter and launch, and not when the realm is created. ADR-008 also gives the session paths fixed realm names under `/run/user/<uid>`: the Wayland socket at `wayland-0`, and the authority file at `ginn-xauthority`. The X11 socket directory is the one exception, and it keeps its own path, `/tmp/.X11-unix`. The SRS says neither that a grant attached after start meets FR-065, nor that a session path may use a fixed name. Two points need the SRS to say more:
  - A shared path keeps its host path inside the realm, although the attach, not the create, places it. A grant that is attached after start therefore still meets FR-065 AC-1, which asks that the same path show the same files.
  - A source that appears after the realm starts, such as a drive mounted after start or a folder made after start, is visible to the next enter or launch, which runs the lazy re-attach. It is not visible in a shell that was already open before the source appeared. FR-065 AC-1 holds for that new shell, and not for the shell already open.
- Proposed wording: "Every shared path appears inside the realm at the same absolute path as on the host, from the next enter or launch after the source appears. A session path appears at a fixed name under the realm's runtime directory, except the X11 socket directory, which keeps its own path."
- How ginn works now: a grant that is a shared path is attached at its host path. The session paths are attached at `/run/user/<uid>/wayland-0` and `/run/user/<uid>/ginn-xauthority`, and `WAYLAND_DISPLAY` and `XAUTHORITY` are set to those names at exec (T4.3). A session path is not a shared path, so the fixed names do not break FR-065 in its ordinary meaning. The SRS does not say so.
- Evidence: none on the host. The T4.9 host gate (ADR-008 amendment, review amendment) proves the attach and the lazy re-attach, including a drive mounted after start. The keep-id runs S-08s and S-08t cover one socket under the rebind, in simulation.
- Status: open, for SRS v1.1. The SRS author decides the wording. The plan proceeds on the proposed wording, and T4.9 tests it.

## CL-13: doctor readiness: "all checks passing" and the disk thresholds

- Requirement IDs: FR-001 (AC-1), FR-007 (AC-2).
- Issue: FR-001 AC-1 says that `ginn doctor`, run as the user after Host Setup, "reports all checks passing". FR-007 AC-2 says that every check reports PASS on a fully configured host. Some healthy states cannot report PASS without a change ginn should not make: the podman config that ginn writes at its first podman call is absent on a host where no realm exists yet (doctor writes nothing, so it cannot write the config), and a root that does not enable a cgroup controller is tolerated by ADR-013 Decision 14. Some thresholds that doctor uses have no SRS number: free disk under 2 GiB fails the check, and under 10 GiB warns.
- Proposed wording: "FR-001 AC-1: 'all checks passing' means that no doctor check reports FAIL; WARN and INFO results are allowed." The disk thresholds of 2 GiB (FAIL) and 10 GiB (WARN) are stated as ginn-defined values, and the SRS does not set them.
- How ginn works now: `ginn doctor` exits 1 only when a check reports FAIL (GINN-E-DOCTOR-FAILED). WARN and INFO exit 0 and are printed with their reason. A fully configured host reports PASS on every check, and a test pins that (`tests/unit/test_doctor_checks.py`). The 2 GiB and 10 GiB values are named as ginn-defined in `src/ginn/doctor/checks.py`.
- Evidence: unit tests under a fake host (T3.6, T3.7). G1 (`spikes/results/G1-host-setup-roundtrip.md`, 2026-10-12, commit 4a3b508) ran `ginn doctor` as the user on a Void host. The first run had one FAIL, for the runsvdir check: the service was up, and the supervisor was idle because `~/.local/state/ginn/sv` did not exist yet. Setup does not create that directory, by design. Doctor now reports that idle supervisor as a WARN with `mkdir -m 0700 -p` as the fix, so no check fails right after setup. That change is by code and unit test, and it is not yet re-run on the host. AC-2 is unproven on a host, because the podman WARN (the config is written at the first podman call) keeps a check from reporting PASS.
- Status: open, for SRS v1.1. The SRS author decides the wording. The plan proceeds under the proposed wording, and T3.6 and T3.7 are built to it.

## CL-14: ginn's own sockets as typed internal grants

- Requirement IDs: DR-004, FR-067, FR-068, NFR-SEC-006, NFR-SEC-008.
- Issue: DR-004 makes every Owned Path never shareable. The ginn runtime root, `/run/ginn/<uid>`, is an Owned Path (ADR-004). ginn creates two sockets under it that a realm must reach. One is the Agent Proxy filter socket, `agent/ssh.sock` (FR-067). The other is a realm's D-Bus proxy socket, `proxy/<realm>/bus.sock` (FR-068). Binding either one reads an Owned Path, which DR-004 forbids for a shared path. The SRS does not say how ginn may bind a socket that it made itself.
- Proposed wording: "DR-004 does not apply to a socket that ginn creates under its runtime root. ginn may bind such a socket into a realm as a typed internal grant, for the Agent Proxy (FR-067) and the session D-Bus proxy (FR-068). A user cannot name such a path as a shared path. The other Owned Paths, those that hold records, the audit log, the journal or a Realm Home, stay unshareable (NFR-SEC-008)."
- How ginn works now: `runtime/mounts.py` marks both binds with `internal=True`, and their grant kind is `internal`. `_internal_refusal` admits the source only when it lies under the ginn runtime root, resolved, and it is a socket (`S_ISSOCK`). Every other path rule still applies, with no Owned Path list, so a socket under another Owned Path, such as the state root, is refused. A user share goes through the Owned Path list (`ops/permissions.py`), so the user cannot name these sockets. The Owned Paths that hold records, the audit log, the journal and Realm Homes keep the full DR-004 check. ADR-008 gives the attach rule.
- Evidence: the T6.8 gate passes FR-067 on a Void host. The real filter socket is a socket in a Development realm, and stopping the last realm clears it (`spikes/results/T6.8-tiers-real.md`, section 3). The same gate passes FR-063-AC2: a share of `~/.ssh` is refused with GINN-E-PATH-FORBIDDEN. FR-068 is not in the host gate (section 4), so the D-Bus socket is unit-tested only. `tests/unit/test_security_agent.py` and `tests/unit/test_security_dbus_filter.py` build the internal binds. `tests/unit/test_runtime_mounts.py` checks that an internal source outside the runtime root, or one that is not a socket, is refused (DR-004).
- Status: open, for SRS v1.1; the plan proceeds under the proposed wording.

## CL-15: an abstract-socket session bus with the host network

- Requirement IDs: FR-068, FR-026, AS-012.
- Issue: FR-068 AC-1 says that a realm reaches the host session bus only through the filter. A realm runs on the host network namespace (FR-026, every non-Kali realm). An abstract unix socket belongs to a network namespace, so such a realm can connect to an abstract session bus directly, past the proxy. The filter then cannot hold, and AC-1 holds only when the host bus is a path socket (`unix:path=`).
- Proposed wording: "FR-068 AC-1 holds when the host session bus is reachable as a path socket. A session address whose bus is an abstract socket is not filtered, and the realm gets no bus for it, with AS-012 as the reason."
- How ginn works now: `runtime/realm_security.bus_plan` refuses an address with an `abstract=` member (AS-012). The realm gets no proxy and no bus bind, and the refusal is a `bus` skip, so `ginn status` lists it. `ginn doctor` reports the `session-bus` check as a WARN with the text "session bus uses an abstract socket; realms on the host network can reach it unfiltered; FR-068 filtering is not in effect". The refusal stops ginn's own bus only. It does not stop a realm from reaching an abstract socket that the host runs.
- Evidence: unit tests only (`tests/unit/test_runtime_realm_security.py`, `tests/unit/test_doctor_checks.py`). Not measured on a host.
- Status: open, for SRS v1.1; the plan proceeds under the proposed wording.
