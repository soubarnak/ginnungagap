# ADR-006: Development-tier capability set without SYS_ADMIN

- Status: Accepted
- Date: 2026-10-12

## Context

FR-062 is a Must. Its AC-3 requires `podman run --rm <image> true` to exit 0 inside a Development realm with services off. Its AC-4 requires KVM acceleration inside the realm on a KVM-capable host. NFR-SEC-002 limits capabilities beyond the rootless defaults to the Kali raw-socket capability and an enumerated set for Development nested containers and `/dev/kvm`. AS-005 gives each user at least 524,288 subordinate IDs, and Q-013 places Docker and VMs in the Development tier only.

Measured on the reference host (Void glibc x86_64, kernel 7.2.9_1, rootless podman 5.8.8, crun 1.30.1):

- Inside a realm, the setuid `newuidmap` fails with `write to uid_map failed: Operation not permitted` unless the realm holds CAP_SYS_ADMIN (S-07 A1 to A3; S-07c C1, C2, C4). S-07 Finding 2 and the S-07c capability dump show that SYS_ADMIN then lands in the realm's effective and ambient sets, not only in the helper, and the bounding set grows by `cap_sys_admin`.
- The cause (S-07d). The setuid helper runs with euid 0, not with the namespace owner's uid. The kernel's uid_map write check then needs CAP_SYS_ADMIN in the effective set. The probes show this: a direct helper call fails without SYS_ADMIN (S-07d P1), and passes with it (P2). A helper with file capabilities and no setuid bit keeps the owner's euid and passes without SYS_ADMIN (S-07d F1). The kernel-source explanation is the spike author's reading of the source. It was not re-checked here. The probes are the evidence.
- The default seccomp profile returns EPERM for `sethostname` and `setdomainname` unless CAP_SYS_ADMIN is held (the excludes rule in `/usr/share/containers/seccomp.json`). The inner crun then fails at `sethostname` (S-07d F2, rc 126). A profile with those two calls ungated passes (S-07d G2, G4). With it, the realm user still cannot change the hostname of its own outer container, and the host hostname did not change (S-07d U1).
- `/dev/kvm` opens read-write when the realm keeps the host's supplementary groups under crun (S-07c K1). Without keep-groups it is denied (S-07c K2).
- Single-ID mapping cannot run the inner image. Subordinate IDs are required (S-07 A7 and Finding 3).

Assumptions and gaps, each with the test that confirms it:

- The combined Development set, with `/dev/kvm`, keep-groups, the seccomp profile and the setcap helpers, is not proven together. M6 `tests/integration/test_tiers_real.py` proves it.
- K1 tests open() only. The KVM ioctls are not run.
- The setcap helpers were tested on Debian stable only. Arch and Fedora are untested (M6).
- `setdomainname` is ungated by the same rule, but only `sethostname` failed in the tests.
- Void hosts have no SELinux, so `label=disable` is omitted. S-07 and S-07d saw this on the test host only.

Ranked characteristics (SRS section 4), in order:

1. Performance: not affected.
2. Host integrity and isolation: decides the question. No capability is added, and the seccomp rule touches two calls only.
3. Recoverability: not affected.
4. Interoperability: Development realms must work for AC-3 and AC-4.
5. Usability: nested containers work with the default tools.

The image gap: a realm image may ship with a `useradd` line in `/etc/subuid` that the outer namespace does not map (S-07 Finding 4). The realm must overwrite it when the realm is created.

Open conflict: the setcap step and the libcap package conflict with FR-013 and CR-007 as written. CL-8 records it. This ADR does not claim that they comply.

## Decision

A Development realm gets no added capability. ginn adds no `--cap-add`, no SYS_ADMIN, and no `--privileged`. Each Development realm carries these outer options, beyond the base argv: `--device /dev/fuse`, `--device /dev/net/tun`, `--device /dev/kvm` (only when the host has `/dev/kvm`, IR-040), `--group-add keep-groups`, `--security-opt unmask=/proc/*` and `--security-opt seccomp=<generated profile>`. ginn never sets `no-new-privileges`, and it does not set `label=disable`.

The realm image is changed after create, inside the realm. The `newuidmap` and `newgidmap` helpers get `cap_setuid=ep` and `cap_setgid=ep`, with mode 0755 and no setuid bit. The realm's `/etc/subuid` and `/etc/subgid` get the realm user's range. The setcap tool package is added in Development realms only. Both steps are recorded so that rebuild replays them (CL-8 covers the SRS side).

The seccomp profile is generated on each host from `/usr/share/containers/seccomp.json`. `sethostname` and `setdomainname` are removed from every rule that names them, and one unconditional allow rule for both is added at the front. Nothing else changes. The file is written under an Owned Path and keyed by the SHA-256 of the source. If the source is missing, or the expected entries are not found, ginn fails closed. It never uses `seccomp=unconfined`.

The enumerated set is in `docs/capabilities.md`. `security/caps.py` (T6.1) must produce exactly that set.

## Alternatives considered

- Add CAP_SYS_ADMIN to the realm (`--cap-add SYS_ADMIN`, S-07 A5, S-07c C4). It passes AC-3 with the default seccomp profile. Rejected by decision 1: it is outside the rootless defaults, the realm holds it as effective and ambient, not only in the helper, and it fails ranked characteristic 2 (host integrity and isolation).
- `seccomp=unconfined` (S-07 A2, S-07d G1). It disables the whole filter to reach two calls. Rejected. It did not fix the newuidmap failure (S-07 A2), and it is not needed once the narrow profile exists.
- Keep the setuid helpers, with no SYS_ADMIN (S-07d G3). Fails: the nested user namespace cannot be mapped (rc 125). Rejected in favour of file capabilities.
- Host ACL on `/dev/kvm` (`setfacl -m u:<user>:rw /dev/kvm`, S-07 Finding 9). Rejected. It needs a root step at run time and was never tested. S-07c K1 shows that keep-groups under crun opens the device without it.
- No nested containers in the Development tier. Rejected: FR-062 AC-3 is a Must.

## Consequences

- Positive: AC-3 passes with no SYS_ADMIN on the S-07d minimal set (S-07d G4). AC-4 open() passes under crun (S-07c K1). The enumerated set is short and fully listed.
- Negative: the seccomp profile allows `sethostname` and `setdomainname` without CAP_SYS_ADMIN. The kernel still checks the capability inside the namespace, and S-07d U1 shows that the realm cannot change its own outer hostname. The seccomp change is still a change to the host's filter, so it must be a documented rule, not a loose setting.
- The helpers keep `cap_setuid` and `cap_setgid`. The realm user's bounding set already holds both (S-07d H3), so no new capability enters the set.
- The code must not add a capability for any Development option, and must not use `seccomp=unconfined`, `--privileged`, or `no-new-privileges`.
- The code must overwrite the image's `/etc/subuid` and `/etc/subgid` lines at create time. The tested value is `dev:1001:64000`. The exact value is set in T6.7.
- The distro data has one integration list, with no tier dimension (`src/ginn/distros/data/fedora.toml`). The libcap package for Development realms needs a follow-up in the loader or the distro data. CL-8 must be resolved first, because FR-013 and CR-007 do not allow it.
- The seccomp source is a host file. The package that provides it on Void is not checked. A dedicated GINN error code is needed for a missing source or entry. None exists in `docs/errors.md` yet, and one is requested for the errors owner (as ADR-004 did for the runtime directory).
- Follow-up tasks: T6.1 (`security/caps.py`), T6.7 (realm subuid lines), M6 `test_tiers_real.py` (the combined set, the setcap helpers on Arch and Fedora, KVM ioctls), and CL-8.
- FR-085 (Docker in a realm) is deferred. Its options are not in this decision. Re-run S-07 Part B after M7, when services are on. CL-2 records the open question.
- The Launch path reservation that the ADR index gave to ADR-006 now sits at ADR-010. See the ADR index.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| NFR-SEC-002 | This ADR decides the enumerated Development set and the seccomp rule. `docs/capabilities.md` lists it, and T6.1 tests it. |
| FR-062 | AC-3 passes with no SYS_ADMIN (S-07d G4). AC-4 open() passes under crun with keep-groups (S-07c K1). ioctls and the combined set are pending in M6. |
| FR-085 | Deferred (Should). Its options are not decided here. CL-2 and the S-07 Final verdict record the deferral. |
| AS-005 | Host Setup supplies the subordinate range that nested mapping draws on. The floor for one nested level is 65,536 IDs, and the 524,288 value is sufficient (S-07 Arithmetic). |
| NFR-SEC-001 | Realms are never privileged. The argv builder refuses `--privileged`. |
| CR-003 | Rootless only. The realm runs as the host user, and no capability is added to the host. |
| CR-002 | Host platform is Void. Decision 1 omits `label=disable` on the basis that Void hosts have no SELinux. |
| DR-010 | The generated seccomp profile is written under an Owned Path. |
| IR-040 | `/dev/kvm` is added only when the host has it. |
| FR-063 | Custom toggles add only options listed in this ADR. |
| CR-007 | Conflict: realm changes are limited to the image, the Integration Package Set, and requested packages. The setcap step is outside that as written. CL-8. |
| FR-013 | Conflict: the Integration Package Set is limited to CA certificates and locale data. The Development libcap package is outside it as written. CL-8. |
| FR-021 | Rebuild names only the Install Manifest. The replay of the setcap step is not stated in the SRS. CL-8. |
| NFR-SEC-005 | The seccomp path and every option are argv entries, never a shell string. |
| NFR-040 | Errors map to catalogue codes. A dedicated code for the seccomp case is requested. |
