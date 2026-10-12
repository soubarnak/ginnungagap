# ADR-006: Development-tier capability set without SYS_ADMIN

- Status: Accepted
- Date: 2026-10-12
- Amended: 2026-10-12 (D2). The first decision used setcap helpers and a subordinate ID range inside the realm. The amended decision runs the inner podman in single-ID mode. It needs no setcap, no libcap package and no subordinate range for the realm user. No code implements the first decision (M6 is not started), so nothing migrates.

## Context

FR-062 is a Must. Its AC-3 requires `podman run --rm <image> true` to exit 0 inside a Development realm with services off. Its AC-4 requires KVM acceleration inside the realm on a KVM-capable host. NFR-SEC-002 limits capabilities beyond the rootless defaults to the Kali raw-socket capability and an enumerated set for Development nested containers and `/dev/kvm`. AS-005 gives each user at least 524,288 subordinate IDs. Q-013 places Docker and VMs in the Development tier only. CR-007 and FR-013 limit what a realm may contain.

Measured on the reference host (Void glibc x86_64, kernel 7.2.9_1, rootless podman 5.8.8, crun 1.30.1). Where a fact comes from the inner podman, the version is given.

- The subordinate-ID design needs CAP_SYS_ADMIN. In the realm, the setuid `newuidmap` fails with `write to uid_map failed: Operation not permitted` unless the realm holds SYS_ADMIN (S-07 A1 to A3, S-07c C1 and C2). S-07d shows the cause from probes: a setuid helper runs with euid 0, and the uid_map write then needs CAP_SYS_ADMIN in the effective set. A helper with file capabilities keeps the owner's euid and passes without SYS_ADMIN (S-07d F1). The kernel explanation comes from reading the source. It was not checked again. The probes are the evidence.
- When SYS_ADMIN is granted, it lands in the realm's effective and ambient sets, not only in the helper (S-07 Finding 2, S-07c). That fails NFR-SEC-002 and ranked characteristic 2.
- The default seccomp profile returns EPERM for `sethostname` unless CAP_SYS_ADMIN is held (S-07e E3, rc 126; S-07d F2). A profile that ungates `sethostname` alone passes (S-07e E4c). With that profile the realm user still cannot change the hostname of its own outer container (S-07d U1). The outer hostname is the host's, because the outer container uses `--network host`.
- A realm image that runs plain `useradd` gets the line `dev:100000:65536` in `/etc/subuid` and `/etc/subgid`. Inner podman then fails in newuidmap (S-07e2 useradd check, E1d-u1). `useradd -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0` writes no line, and the inner podman passes (S-07e2 E1d-u2).
- In single-ID mode, inner podman does not call newuidmap. Its uid_map is one entry, `0 1000 1` (S-07e Finding 2, E1o-uidmap).
- The image unpack fails on `lchown /etc/shadow` in single-ID mode unless the storage config sets `ignore_chown_errors` (S-07e E1, S-07e2 E1d-nokey). With the key set, the unpack passes (S-07e E1o, S-07e2 E1d and E1d-u2).
- Podman 5.4.2 drops the storage.conf options when `--root` is on the command line. The storage config must therefore reach inner podman as a file, and the inner command takes no storage flags (S-07e Finding 1, S-07e2).
- `/dev/kvm` opens read-write when the realm keeps the host's supplementary groups under crun (S-07c K1). It is denied without them (S-07c K2).

Assumptions and gaps, each with the test that confirms it:

- FR-062 AC-3 passes in parts; the decided combination is pending S-07f. The parts never run together:
  - the CLI storage options (S-07e E1o, E4c) and the Realm Home `storage.conf` (S-07e2 E1d, E1d-u2) were run separately;
  - the both-names seccomp profile (S-07e E1o, S-07e2 E1d-u2) and the sethostname-only profile (S-07e E4c) were run separately;
  - keep-groups was not run with the single-ID set. Keep-groups combined with a single-ID user namespace that denies `setgroups` is untested.
  The full set, with `/dev/kvm`, `unmask=/proc/*`, the sethostname-only profile, the Realm Home config files and `useradd -K`, is for the M6 test `tests/integration/test_tiers_real.py`.
- K1 tests open() only. The KVM ioctls were not run.
- Inner tests used a Debian stable image and podman 5.4.2 inside the realm. Arch, Fedora and Kali were not tested.
- `setdomainname` is not ungated. An inner container that sets `--domainname` was not tested (S-07e follow-up 2).
- A realm created as Basic and later changed to Development was not tested. The `-K` rule applies at create time.
- `XDG_RUNTIME_DIR` inside the realm and a bind-mounted Realm Home were not tested (S-07e2 open items 1 and 2).
- Void hosts have no SELinux, so `label=disable` is omitted. The test host confirmed this for the older design only.

Ranked characteristics (SRS section 4), in order:

1. Performance: not affected.
2. Host integrity and isolation: decides the question. No capability is added. The seccomp rule touches one call.
3. Recoverability: not affected.
4. Interoperability: Development realms must work for AC-3 and AC-4.
5. Usability: nested containers work with the default tools. Inside them, only one ID is visible (see Consequences).

Open conflict: the setcap step and the libcap package are withdrawn (CL-8). The podman package still needs a route that CR-007 allows, which is a requested package. The decision of the user is pending.

## Decision

A Development realm gets no added capability. ginn adds no `--cap-add`, no SYS_ADMIN, and no `--privileged`. Each Development realm carries these outer options, beyond the base argv: `--device /dev/fuse`, `--device /dev/net/tun`, `--device /dev/kvm` (only when the host has `/dev/kvm`, IR-040), `--group-add keep-groups`, `--security-opt unmask=/proc/*` and `--security-opt seccomp=<generated profile>`. ginn never sets `no-new-privileges`, and it does not set `label=disable`.

The realm user is created without subordinate IDs. ginn runs `useradd -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0`, so the realm's `/etc/subuid` and `/etc/subgid` get no line for it. Inside the realm, podman then uses the single mapping `0 1000 1`.

ginn writes two files into the Realm Home, `~/.config/containers/storage.conf` and `~/.config/containers/containers.conf`. The storage file uses the overlay driver with fuse-overlayfs and `ignore_chown_errors = "true"`. The vfs form also passes and is kept as an alternative. The containers file sets `cgroup_manager = "cgroupfs"` and `events_logger = "file"`. The user runs the inner podman with no `--root`, `--runroot`, `--tmpdir`, `--storage-driver` or `--storage-opt`.

Podman and its helpers enter the realm as requested packages (CR-007). ginn does not add them itself.

The seccomp profile is generated on each host from `/usr/share/containers/seccomp.json`. It removes `sethostname` from every rule that names it. It adds one rule at the front that allows `sethostname`. It leaves `setdomainname` as it is. The file is written under an Owned Path and keyed by the SHA-256 of the source. If the source is missing, or the expected entries are not found, ginn fails closed. It never uses `seccomp=unconfined`.

The enumerated set is in `docs/capabilities.md`. `security/caps.py` (T6.1) must produce exactly that set.

## Alternatives considered

- Add CAP_SYS_ADMIN to the realm (`--cap-add SYS_ADMIN`, S-07 A5, S-07c C4). It passes AC-3 with a full ID map. Rejected. It is outside the rootless defaults. The realm holds it as effective and ambient, not only in the helper. It fails ranked characteristic 2.
- Setcap helpers with a subordinate range and a narrow seccomp profile, and no SYS_ADMIN (S-07d G4). It passes AC-3 and gives a full ID map inside the realm. Rejected for now. It needs a setcap step and a libcap package in the realm, and both conflict with FR-013 AC-1 and CR-007 as written (CL-8). The single-ID design needs neither. Its cost is the one-ID limit.
- `seccomp=unconfined` (S-07 A2, S-07d G1). It disables the whole filter to reach one call. Rejected.
- Setuid helpers with no SYS_ADMIN (S-07d G3). Rejected. The uid_map write fails (rc 125).
- A host ACL on `/dev/kvm` (`setfacl`, S-07 Finding 9). Rejected. keep-groups already opens the device (S-07c K1). An ACL would need a root step at run time, and it was never tested.
- No nested containers in the Development tier. Rejected. FR-062 AC-3 is a Must.
- The vfs driver as the default. Not chosen. It needs no `/dev/fuse`, but its disk and speed cost was not measured. The overlay form with fuse-overlayfs is the design choice. vfs stays as the tested alternative.

## Consequences

- Positive: FR-062 AC-3 passes in parts; the decided combination is pending S-07f. The parts pass with no SYS_ADMIN and no setcap step (S-07e E1o, S-07e2 E1d-u2). AC-4 open() passes under crun with keep-groups (S-07c K1). The added set is short and fully listed. It adds no capability.
- Negative: nested containers see one ID. `chown` to any other ID fails with EINVAL (S-07e E6). Images that need other IDs run as root inside a nested container. This is a known limitation. A user who needs a full ID map inside the realm needs a different design, and that design is not decided.
- Negative: the seccomp profile lets a realm process call `sethostname` without CAP_SYS_ADMIN. The kernel still checks the UTS namespace. The realm cannot change its own outer hostname (S-07d U1). The change is still a change to the host's filter, so it is a documented rule, not a loose setting.
- The code must not add a capability for any Development option. It must not use `seccomp=unconfined`, `--privileged` or `no-new-privileges`.
- The code must create the realm user with the `-K` flags. It must not write a subordinate line for that user. The rebuild (T8.3) must repeat the same creation step. A realm changed to Development after create needs its own check. That check was not tested.
- The code must write the Realm Home config files. It must not pass `--root`, `--runroot`, `--tmpdir`, `--storage-driver` or `--storage-opt` to inner podman.
- The seccomp source is a host file. On Void it belongs to the `containers-common` package (checked in the xbps file list on 2026-10-12), and the ginn package template declares that package. A dedicated GINN error code is needed for a missing source or entry. None exists in `docs/errors.md`, and one is requested for the errors owner, as ADR-004 did for the runtime directory.
- Follow-up tasks: T6.1 (`security/caps.py`), T6.7 (realm user creation with `-K`, the Realm Home config files and the seccomp profile), T8.3 (the rebuild repeats the user creation), the M6 test `test_tiers_real.py` (the open items in `docs/capabilities.md`), and CL-8 (withdrawn; the podman request route is still open).
- FR-085 (Docker in a realm) is deferred. Its options are not in this decision. Re-run S-07 Part B after M7. CL-2 records the open question.
- The Launch path reservation that the ADR index gave to ADR-006 now sits at ADR-010. See the ADR index.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| NFR-SEC-002 | Decides the enumerated Development set and the seccomp rule. `docs/capabilities.md` lists it, and T6.1 tests it. |
| FR-062 | AC-3 passes in parts; the decided combination is pending S-07f. No SYS_ADMIN and no setcap in the parts (S-07e E1o, S-07e2 E1d-u2). AC-4 open() passes under crun with keep-groups (S-07c K1). ioctls and the full set are pending in M6. |
| FR-013 | ginn adds no package in the Development tier. The libcap package is withdrawn. Podman comes in only as a requested package (CR-007). |
| CR-007 | The realm changes are the user creation at create time, and the Realm Home config files, which are ginn-owned data. Podman enters as a requested package. No helper file changes. |
| FR-021 | The rebuild must repeat the user creation step. FR-021 names only the Install Manifest, so the replay is a design choice (T8.3). The SRS does not say it. |
| NFR-SEC-001 | Realms are never privileged. The argv builder refuses `--privileged`. |
| CR-003 | Rootless only. No capability is added to the host. |
| CR-002 | Host platform is Void. The decision omits `label=disable` because Void hosts have no SELinux. |
| DR-010 | The generated seccomp profile and the Realm Home config files are written under Owned Paths. |
| IR-040 | `/dev/kvm` is added only when the host has it. |
| FR-063 | Custom toggles add only options listed in this ADR. |
| NFR-SEC-005 | The seccomp path and every option are argv entries, never a shell string. |
| NFR-040 | Errors map to catalogue codes. A dedicated code for the seccomp case is requested. |
| AS-005 | Host Setup still adds the outer subordinate range, which the outer map needs. A nested level under this design draws on no range. |
| FR-085 | Deferred (Should). Its options are not decided here. CL-2 records the deferral. |
