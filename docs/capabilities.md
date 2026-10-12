# Capabilities and devices per tier

This document lists every podman option that ginn adds to a realm beyond the base argv that all realms share. It is the enumerated set that NFR-SEC-002 refers to. `security/caps.py` (T6.1) must produce exactly this set, and the per-tier argv test compares against it.

The set is accepted (decision of 2026-10-12, recorded in ADR-006 for Development and in this file for Kali). Each row names its evidence: a spike ID and a case ID in `spikes/results/`. Rows marked "pending" are not yet proven on the reference host.

## Base argv (all tiers)

Options every realm carries. None adds a capability or a device.

- `--runtime crun`, and the global flags `--root`, `--runroot` and `--tmpdir` (ADR-004, IR-020).
- `--name`, `--userns keep-id` (roadmap, design choices), `--network host` for non-Kali realms (AS-006), and `--init --init-path <catatonit>` when services are off (FR-082).
- Creation from a digest reference in the Trust Manifest (FR-011, ADR-005).
- No `--privileged`, and no `--cap-add ALL`. The argv builder in `src/ginn/podman/adapter.py` refuses both (NFR-SEC-001).

## Basic

| Item | Option | Evidence | Status |
|---|---|---|---|
| Added options | none | none needed | done |
| GPU device nodes (FR-061) | `--device` for each GPU node; NVIDIA through CDI (FR-069) | S-06 and ADR-008 (planned) decide the node list. Not in this document yet. | pending |

## Development

Outer options added to every Development realm.

| Item | Option | Why | Evidence | Status |
|---|---|---|---|---|
| Inner storage device | `--device /dev/fuse` | fuse-overlayfs is the inner storage driver (FR-062 AC-3) | S-07 M2 (inner overlay mount fails without it); S-07d M-fuse (rc 127) | proven |
| Inner network device | `--device /dev/net/tun` | pasta and slirp4netns need it for the inner default network (FR-062 AC-3) | S-07 A4 (pasta fails without it); S-07d M-tun (rc 127) | proven |
| KVM device | `--device /dev/kvm`, added only when the host has `/dev/kvm` (IR-040) | FR-062 AC-4 | S-07c K1 (realm user opens /dev/kvm read-write with keep-groups); S-07c K2 (denied without it). K1 tests open() only. | open() proven; KVM ioctls and the combined set pending (M6) |
| Kept groups | `--group-add keep-groups` | keeps the host supplementary groups, so the realm user keeps the kvm group. crun only (ADR-004). | S-07c K1 and K2 | proven |
| Proc unmask | `--security-opt unmask=/proc/*` | the inner proc mounts and `ping_group_range` need the unmasked entries | S-07 M3 (`unmask=/proc/sys` fails at the proc mount); S-07 M4 (`unmask=/proc/sys/net` fails); S-07 A8 (no unmask fails); S-07d G4 passes; S-07d M-unmask fails | proven |
| Seccomp profile | `--security-opt seccomp=<generated profile>` (rule below) | the inner crun calls `sethostname`, which the default profile refuses without CAP_SYS_ADMIN | S-07d M-seccomp (default profile: rc 126, sethostname EPERM); S-07d G2 and G4 pass; S-07d U1 (realm cannot change its own outer hostname) | proven for sethostname; setdomainname not tested alone |

Realm-internal changes. These happen inside the realm image and the realm's own files, not on the host.

| Item | Change | Why | Evidence | Status |
|---|---|---|---|---|
| Newuidmap helper | `/usr/bin/newuidmap` with `cap_setuid=ep`, mode 0755, no setuid bit | a setuid helper runs with euid 0, and the kernel then needs CAP_SYS_ADMIN for the uid_map write (ADR-006) | S-07d F1 passes; S-07d G3 (setuid helpers, control) fails with rc 125; S-07d P1 and P2 | proven on Debian stable only |
| Newgidmap helper | `/usr/bin/newgidmap` with `cap_setgid=ep`, mode 0755, no setuid bit | same as newuidmap | same as above | proven on Debian stable only |
| Setcap tool package | `libcap2-bin` (Debian and Kali), `libcap` (Arch and Fedora). Development only. | needed to run `setcap` in the realm build | S-07d used `libcap2-bin` on Debian stable | Arch and Fedora package names untested. Conflicts with FR-013 and CR-007 as written: see CL-8 below. |
| Realm subuid and subgid | Replace the image's `useradd` line (`dev:100000:65536`) with the realm user's range. Tested value: `dev:1001:64000`. | the image's line is not mapped inside the outer namespace; inner podman needs a mapped range | S-07 Finding 4 (image line unmapped); S-07 A7 (no range: lchown fails with EINVAL); S-07c and S-07d used `dev:1001:64000` | proven as tested. The two-line form `dev:1:999` and `dev:1001:64536` gives inner 0..65535 (S-07 A6, under SYS_ADMIN). The exact value is set in T6.7. |

### Position on CR-007, FR-013 and CL-8

The helper change and the setcap package are realm-internal steps after create. They are applied only in the Development tier. They are the same kind of step as installing the Integration Package Set. The base image is not modified: it stays the official image at its pinned digest (CR-007). The steps are to be recorded so that rebuild replays them.

This does not satisfy the SRS as written, so it is not claimed as compliant:

- FR-013 limits the Integration Package Set to CA certificates and locale data, and fixes it per distribution. AC-1 allows no other package additions. A libcap package in Development realms is outside that list.
- CR-007 limits realm contents to the official image, the Integration Package Set, and requested packages with their declared dependencies. The libcap package is none of these. The file-capability change also alters a file in the realm, which CR-007 does not list.
- FR-021 names only the Install Manifest for rebuild. The SRS does not say that this step is replayed.

CL-8 in `docs/clarifications.md` asks SRS v1.1 to decide this. Until then the setcap step and its package are a known conflict, not a settled rule.

Rules for Development. These are things that must not appear.

| Rule | Why | Evidence |
|---|---|---|
| No `--cap-add` of any kind. SYS_ADMIN is not granted. | ADR-006, decision 1 | S-07d G4: CapBnd `0x800405fb`, CapEff empty |
| No `--privileged` | NFR-SEC-001, CR-003 | refused by the argv builder |
| No `seccomp=unconfined` | disables the whole filter, not only the two calls | S-07d G1 passes but is informative only; not used |
| No `no-new-privileges` | blocks the file capabilities the helpers need | S-07 M5 fails with the setuid helpers; not retested with setcap |
| No `label=disable` | the decision omits it: Void hosts have no SELinux (decision 1) | S-07 M1, and S-07d C3c-g2 and G4, pass without it. Host-specific: the test host had no SELinux. Revisit on an SELinux host. |

### Seccomp profile generation rule

1. The source is `/usr/share/containers/seccomp.json` on the host, the default profile podman uses.
2. Remove `sethostname` and `setdomainname` from every rule that names them. That removes the rule that allows them only with CAP_SYS_ADMIN, and the rule that returns EPERM without it. Drop any rule left with no names.
3. Insert one rule at the front that allows both names unconditionally (`SCMP_ACT_ALLOW`, no includes or excludes).
4. Change nothing else. The tested generator is `make_seccomp` in `spikes/s07d_newuidmap.sh`.
5. Fail closed. If the source file is missing, or the `sethostname` and `setdomainname` entries are not found, ginn stops with an error. It never falls back to `seccomp=unconfined`. The error needs a catalogue code. No code for this case exists in `docs/errors.md` yet; a dedicated code is requested for the errors owner.
6. Write the result under an Owned Path (DR-010). The file name is the SHA-256 of the source file, so a changed host profile produces a new file. Unit tests use a hand-written fixture profile, not a copy of the host file.

## Custom (FR-063)

The SRS does not define a Custom capability set. Each Custom toggle for a permission in FR-062 adds only the options in the Development table above. No toggle adds a capability. Which option each toggle adds is set in T6.1 and T6.2. A toggle that would add any option not listed here requires this document to change first (NFR-SEC-002). The D-Bus filter widening (AS-012) is not a podman option and is not in this table.

## Kali (a distro overlay, on any tier)

Kali is a distro, not a tier (FR-026, FR-027). Its extra options:

| Item | Option | Why | Evidence | Status |
|---|---|---|---|---|
| Isolated network | `--network pasta` in place of `--network host` | FR-026: Kali uses its own network namespace | S-09 A (outbound TCP works through pasta); S-09 P2 (publish on 127.0.0.1 only) | proven |
| Raw sockets | `--cap-add NET_RAW` | FR-027 AC-1: raw sockets inside the realm's own namespace | S-09 A: copied nmap SYN and ACK scans, tcpdump AF_PACKET capture | proven. NET_RAW was not tested alone (no pasta run without it that opens a raw socket). |

Not added: NET_ADMIN. S-09 C shows that NET_ADMIN lets the stock nmap run, but the stock binary is not needed (S-09 Finding 2 and 6).

Open, not decided: remove the file capabilities from `/usr/lib/nmap/nmap` in the Kali realm (S-09 Finding 2). This is a candidate fix and was not tested. It removes a capability rather than adding one, so it is not in the enumerated set. It conflicts with pulling the official image by digest unless a build step is added (CR-007). Decision for T10.1.

## Not in this document

- FR-085 (Docker in a realm) is deferred. Its options will be added here when it is decided (CL-2).
- NVIDIA CDI devices (FR-069) are covered by ADR-008, which is planned.

## Open items for M6

The M6 host test `tests/integration/test_tiers_real.py` must prove these, because the spikes did not:

- The full Development set together: `/dev/kvm`, keep-groups, the seccomp profile and the setcap helpers, with no SYS_ADMIN.
- KVM ioctls, not only open().
- The setcap helpers and package on the Arch and Fedora images.
- `setdomainname` on its own, if the seccomp rule is kept for it.
- The realm subuid value (T6.7).
