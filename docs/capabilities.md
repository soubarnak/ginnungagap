# Capabilities and devices per tier

This document lists every podman option that ginn adds to a realm beyond the base argv that all realms share. It is the enumerated set that NFR-SEC-002 refers to. `security/caps.py` (T6.1) must produce exactly this set, and the per-tier argv test compares against it.

The set is accepted (decision of 2026-10-12). The Development part follows ADR-006, as amended on the same day. Each row names its evidence: a spike ID and a case ID in `spikes/results/`. Rows marked "pending" are not yet proven on the reference host.

## Base argv (all tiers)

Options every realm carries. None adds a capability or a device.

- `--runtime crun`, and the global flags `--root`, `--runroot` and `--tmpdir` (ADR-004, IR-020).
- `--name`, `--userns keep-id` (roadmap, design choices), `--network host` for non-Kali realms (AS-006), and `--init` when services are off (FR-082). No `--init-path` is passed. Only S-05r ran plain `--init` (services off, no `--init-path`), and it succeeded through podman's lookup path. S-07 and S-08 passed `--init-path`, and S-09 dropped `--init`. Void installs catatonit at `/usr/libexec/catatonit/catatonit`, with podman's lookup link at `/usr/libexec/podman/catatonit` (S-05r). `/usr/bin/catatonit` is absent on the reference host (S-05b).
- Creation from a digest reference in the Trust Manifest (FR-011, ADR-005).
- No `--privileged`, and no `--cap-add ALL`. The argv builder in `src/ginn/podman/adapter.py` refuses both (NFR-SEC-001).

## Basic

FR-061 grants exactly: `~/Downloads` (read-write); the external drive mount roots `/run/media/$USER`, `/media` and `/mnt` (read-write); the display socket(s); the audio socket(s); the GPU device nodes; and the host fonts and timezone (read-only). The table lists the options that carry each grant, plus the NVIDIA items of FR-069 that need GPU permission. Each row is marked exercised or design-only. "Exercised" means a spike ran the option on the reference host, and the row names the spike and section. "Design-only" means no spike ran it. `security/caps.py` (T6.1) must produce exactly this set. T4.2 (`runtime/mounts.py`) builds the binds and T4.3 (`runtime/env.py`) builds the environment. The T6.1 argv test compares their combined output with this list.

| Item | Option | Grant | Evidence | Status |
|---|---|---|---|---|
| Downloads | `-v <home>/Downloads:<home>/Downloads`, read-write, same path | FR-061, FR-065 | none | design-only (T4.2). ADR-012 records that it was not run. |
| Mount roots | `-v /run/media/$USER:/run/media/$USER`, `-v /media:/media` and `-v /mnt:/mnt`, read-write, same paths | FR-061, FR-065 | none | design-only (T4.2). Not run. |
| Display socket | Bind `$XDG_RUNTIME_DIR/wayland-1` to `/run/user/<uid>/wayland-1`; bind `/tmp/.X11-unix`; `-e WAYLAND_DISPLAY -e DISPLAY` | FR-061 | S-06 section 2 (runc) | exercised. The Wayland EGL and X11 GLX renderer checks ran through these binds (S-06 section 3). |
| Audio socket | Bind `$XDG_RUNTIME_DIR/pipewire-0` to `/run/user/<uid>/pipewire-0`; bind `pulse/native` | FR-061 | S-06 section 2 (runc) | exercised as binds only: both sockets were bound and visible. No connection was made, because the image has no `pactl`. |
| GPU device nodes | `--device /dev/dri`, which exposes `card0`, `card1`, `renderD128` and `renderD129`, including the NVIDIA DRM nodes | FR-061 | S-06 sections 2 and 3 (runc) | exercised on runc. The AMD renderer checks ran. Pending crun (T6.6). |
| Host fonts | `-v /usr/share/fonts:/usr/share/fonts:ro` | FR-061 | S-06 section 2 (runc) | exercised |
| Host timezone | `-v /etc/localtime:/etc/localtime:ro` | FR-061 | S-06 section 2 (runc) | exercised |
| Runtime directory (support for the socket binds; FR-061 names no directory) | `--tmpfs /run/user/<uid>:mode=0700,U`, with `-e XDG_RUNTIME_DIR=/run/user/<uid>` | ADR-012 Decision | S-06 section 2 (runc) | exercised. `U` gives the realm user as owner. Plain `mode=0700` gives owner 0:0. podman refused `uid=`. |
| NVIDIA libraries and nodes (FR-069, when the host has the NVIDIA driver) | `--cdi-spec-dir <CDI dir> --device nvidia.com/gpu=all`, with the spec written under the ginn data directory; `LD_LIBRARY_PATH=/usr/lib/ginn-nvidia` | FR-069, GPU permission | S-06 sections 2, 4 and 5 (runc) | exercised: `nvidia-smi` and the GLX, EGL and Vulkan checks ran with the spec attached. Pending T6.6: the CDI `env` field, the mismatch case, and crun. |

Not in this set:

- `--group-add keep-groups`. S-06 passed it on runc, and it had no effect there (S-06 section 2). ADR-012 leaves it undecided until M6 tests it under crun. It is not in the Basic set. The Development table lists it as an option of that tier.
- `XAUTHORITY`. S-06 mounted the Xauthority file only when the host sets the variable. This host does not, so the mount was not run. FR-061 names no authority file, so it is not in the set.

## Development

Development grants Basic plus nested rootless containers (FR-062 AC-3) and `/dev/kvm` (FR-062 AC-4). The tier adds no capability to the realm. It adds three devices (`/dev/fuse`, `/dev/net/tun`, and `/dev/kvm` when the host has it), two security options and one group. It also runs the inner podman in single-ID mode, with no subordinate ID range for the realm user. The design is in ADR-006.

### Outer options

These options are added to every Development realm, beyond the base argv.

| Item | Option | Why | Evidence | Status |
|---|---|---|---|---|
| Inner storage device | `--device /dev/fuse` | The overlay driver mounts through fuse-overlayfs. The vfs driver does not need it. | S-07d M-fuse (overlay mount fails, rc 127), in the S-07d setcap design. S-07e E5c (vfs passes without it). The single-ID overlay runs (S-07e E1o, E1d) carry `/dev/fuse` and never drop it. S-07f cases A to D, with the full set. | proven for the single-ID set, Debian realm (S-07f cases A to D). |
| Inner network device | `--device /dev/net/tun` | pasta, the inner default network, needs it. | S-07e E5a (pasta fails without it, rc 127). S-07d M-tun. | proven |
| KVM device | `--device /dev/kvm`, only when the host has `/dev/kvm` (IR-040) | FR-062 AC-4 | S-07c K1 (realm user opens /dev/kvm read-write with keep-groups). S-07c K2 (denied without it). S-07f `KVM_OPEN_RW=ok` in cases A to D. | open() proven under the full set (S-07f). KVM ioctls pending (M6). |
| Kept groups | `--group-add keep-groups` | Keeps the host's supplementary groups, so the realm user keeps the kvm group. The 14 host gids are kept in the kernel but appear unmapped, shown as `nogroup` in the realm. Works under crun only (ADR-004). | S-07c K1 and K2. S-07f check 9 (`id` shows `65534(nogroup)`). | proven for the /dev/kvm open() and for the inner `alpine:3` run with the full set (S-07f cases A to D). KVM ioctls pending (M6). |
| Proc unmask | `--security-opt unmask=/proc/*` | The inner proc mount and `ping_group_range` need the unmasked entries. | S-07e E5b (`ping_group_range` fails without it, rc 126). S-07d M-unmask. S-07 M3 and M4 (narrower values fail). | proven |
| Seccomp profile | `--security-opt seccomp=<generated profile>` (rule below) | The inner crun calls `sethostname`. The default profile refuses that call without CAP_SYS_ADMIN. | S-07e E3 (default profile: rc 126, EPERM). S-07e E4c (generated profile passes). | proven for `sethostname`. `setdomainname` is not ungated, and it is not needed (S-07e Finding 3). |

The base argv also carries `--runtime crun`, `--userns keep-id`, `--network host` and `--init`. The S-07e and S-07f runs used the same options, with `--init-path` pointing at a spike copy of catatonit. The product argv has no `--init-path`. ginn adds no `--cap-add`, and nothing in this table grants a capability.

### Realm contents and create-time steps

These steps run inside the realm or in the Realm Home. They do not change the host.

| Item | Change | Why | Evidence | Status |
|---|---|---|---|---|
| Realm user | Create the realm user with `useradd -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0 -m -u <uid> <name>`. No line for the realm user goes into `/etc/subuid` or `/etc/subgid`. | Plain `useradd` adds `dev:100000:65536`. Inner podman then calls newuidmap, which fails without CAP_SYS_ADMIN. | S-07e2 useradd check (plain useradd writes both lines, `-K` writes none). S-07e2 E1d-u1 (plain useradd: rc 125, newuidmap EPERM). S-07e2 E1d-u2 (`-K`: passes). S-07f check 4 (`-K` writes no line on Debian, Arch and Fedora 44). S-07f check 5 (plain useradd wrote `dev:100000:65536` on Debian and Arch, and `dev:524288:65536` on Fedora 44; the cause was not checked). | proven on Debian stable: AC-3 passes with podman inside (S-07f). Arch and Fedora 44: `-K` writes no line, no podman run. |
| Inner storage config | ginn writes `~/.config/containers/storage.conf` in the Realm Home. Overlay form: `[storage]` with `driver = "overlay"`, then `[storage.options.overlay]` with `mount_program = "/usr/bin/fuse-overlayfs"` and `ignore_chown_errors = "true"`. | Single-ID mode needs `ignore_chown_errors`. Without it, the image unpack fails on `/etc/shadow`. | S-07e2 E1d (pass; its image had the subordinate lines removed by `sed`). S-07e2 E1d-u2 (pass with `-K`). S-07e2 E1d-nokey (no key: lchown EINVAL, rc 125). | proven on Debian stable, podman 5.4.2 inside |
| Inner storage config, vfs form | The same file uses `driver = "vfs"` and `[storage.options.vfs]` with `ignore_chown_errors = "true"`. | An alternative that does not need fuse-overlayfs. Not the default. | S-07e2 E1d-vfs and E1d-u2-vfs (pass). S-07e2 E1d-vfs-nokey (fails without the key). | proven as an alternative. Disk and speed cost not measured. |
| Inner container config | ginn writes `~/.config/containers/containers.conf` with `[engine]`, `cgroup_manager = "cgroupfs"` and `events_logger = "file"`. | Pins two choices that podman otherwise infers. S-07f showed that podman infers `cgroupManager=cgroupfs` and `eventLogger=file` in a services-off realm. | S-07f cases B and D pass without the file (`podman info` reports the same two values in all four cases). S-07e2 (same file as S-07e). | written; pins choices podman otherwise infers; required for services-on realms; untested until M7. Not needed for AC-3 (S-07f cases B and D). |
| Realm image | The image needs podman and its helpers. They come in as requested packages (CR-007). ginn does not add them. No setcap step, no libcap package, no helper file change. | The inner podman has to exist. | S-07e2 Conclusion, item 4, and open item 4 | open: how the request is made is the user's decision (CL-8) |

Notes on the storage choice. The overlay form with fuse-overlayfs is the design choice. It is the form the Development outer set is built for, with `/dev/fuse` in the set. The vfs form also passes, and it needs no `/dev/fuse`. Its disk and speed cost was not measured, so it stays an alternative.

Inner podman runs without `--root`, `--runroot`, `--tmpdir`, `--storage-driver` or `--storage-opt`. Podman 5.4.2 drops the storage.conf options when `--root` is on the command line (S-07e Finding 1). With `XDG_RUNTIME_DIR` unset, the inner runroot falls back to `/tmp/storage-run-1000` (S-07e2).

### Limitation: one ID inside nested containers

A nested container sees one ID, uid 0. The single mapping is `0 1000 1`. `chown` to any other ID fails with EINVAL (S-07e E6: `chown 100:100 /x`, rc 1). Image files owned by other IDs unpack as root. A nested process cannot switch to another user. Only uid 0 was exercised. A nested process that runs as a non-root image user was not tested.

The earlier design, with setcap helpers and a subordinate range, gave a full ID map inside the realm. It needed the setcap step and the libcap package. Those conflict with FR-013 and CR-007 (ADR-006, CL-8).

### Rules for Development

These options and actions must not appear in a Development realm.

| Rule | Why | Evidence |
|---|---|---|
| No `--cap-add` of any kind. SYS_ADMIN is not granted. | NFR-SEC-002. SYS_ADMIN is outside the rootless defaults. | The single-ID set passes without it (S-07e E1o, S-07e2 E1d-u2). The S-07d setcap set also passes without it (S-07d G4: `/dev/fuse`, `/dev/net/tun`, `unmask=/proc/*`, the narrow seccomp profile, no label, setcap helpers; inner rc 0; outer CapEff 0). The older subordinate-ID set needed it (S-07 A5, S-07c C4). |
| No `--privileged`. | NFR-SEC-001, CR-003 | Refused by the argv builder. |
| No `seccomp=unconfined`. | It disables the whole filter, not the one call that needs relief. | S-07d G1 passes, as an informative run only. |
| No setcap, no libcap package, no change to a helper binary. | They conflict with FR-013 and CR-007 (CL-8). The single-ID set does not need them. | S-07e E1o-realm (helpers unused), S-07e2 E1d-u2 (no setcap, no libcap). |
| No `/etc/subuid` or `/etc/subgid` line for the realm user. | A line makes inner podman call newuidmap, which fails. | S-07e E0 (rc 125). S-07e2 E1d-u1 (rc 125). |
| No host ACL on `/dev/kvm`. | keep-groups already opens the device. An ACL would need a root step. | S-07c K1. |
| No `label=disable`. | The reference host has no SELinux, and S-07 notes that the option may be inert there. The passing runs do not use it. | S-07e E1o and S-07 C3c-g2 (pass without it). An SELinux host is untested. |
| No `no-new-privileges`. | NNP is compatible with the nested-podman set (S-07f case C). It is not adopted because setuid and file-capability workflows inside a realm are untested: sudo, su, passwd, ping, and the FR-043 AUR flow where makepkg escalates. Revisit in M6 with that list. | S-07f case C passes (outer rc 0, inner rc 0). S-07 M5 (old set: blocks the setuid helper). |
| Inner podman takes no `--root`, `--runroot`, `--tmpdir`, `--storage-driver` or `--storage-opt`. | Podman 5.4.2 drops the storage.conf options when `--root` is on the command line. | S-07e Finding 1 (E1 fails). S-07e2 (plain run passes). |

### Seccomp profile generation rule

1. The source is `/usr/share/containers/seccomp.json` on the host, the default profile podman uses.
2. Remove the name `sethostname` from every rule that names it. The host file names it in two rules (S-07e Finding 3). Leave `setdomainname` alone, so it keeps its original conditions. Drop any rule that is left with no names.
3. Insert one rule at the front. It allows `sethostname` with action `SCMP_ACT_ALLOW`. It has no includes and no excludes.
4. Change nothing else. The tested generator is the sethostname-only variant of `spikes/s07e_seccomp.py`.
5. Fail closed. If the source file is missing, or the `sethostname` entries are not found, ginn stops with an error. It never falls back to `seccomp=unconfined`. The error needs a catalogue code. None exists in `docs/errors.md` yet. A code is requested for the errors owner.
6. Write the result under an Owned Path (DR-010). Name the file after the SHA-256 of the source, so a changed host profile gives a new file. Unit tests use a hand-written fixture profile, not a copy of the host file.

### Position on CR-007, FR-013 and CL-8

The single-ID design adds no package, no helper file change and no setcap step. Its realm-side changes are these:

- The realm user is created with the `-K` flags. That is part of realm creation (T4.4), not a package addition.
- The two config files go into the Realm Home. They are ginn-owned data (DR-006).

Two points stay open:

- The image must contain podman and its helpers. A realm can get them only as requested packages, which CR-007 allows. FR-013 AC-1 allows no other additions, so ginn does not add podman by itself. S-07e2 left the request path to the user.
- The `-K` user creation is not in the Install Manifest. FR-021 names only the Install Manifest for replay. The rebuild must repeat the creation step by design (T8.3). The SRS does not say so.

CL-8 is withdrawn for the setcap and libcap steps. See `docs/clarifications.md`.

## Custom (FR-063)

The SRS does not define a Custom capability set. Each Custom toggle for a permission in FR-062 adds only the options in the Development table above. No toggle adds a capability. Which option each toggle adds is set in T6.1 and T6.2. A toggle that would add any option not listed here requires this document to change first (NFR-SEC-002). The D-Bus filter widening (AS-012) is not a podman option and is not in this table.

## Kali (a distro overlay, on any tier)

Kali is a distro, not a tier (FR-026, FR-027). Its extra options:

| Item | Option | Why | Evidence | Status |
|---|---|---|---|---|
| Isolated network | `--network pasta` in place of `--network host` | FR-026: Kali uses its own network namespace | S-09 A (outbound TCP through pasta) and P2 (publish on 127.0.0.1 only), both from the first pass, which did not pin `XDG_RUNTIME_DIR`. The pinned rerun covers the product argv for AC-2 only. | proven on the first pass; pinned for the product argv under AC-2 only |
| Raw sockets | `--cap-add NET_RAW` | FR-027 AC-1: raw sockets inside the realm's own namespace | S-09 rerun ("S-09 Run C rerun"): FR-027 AC-1 and AC-2 PASS with the product argv `--network pasta --cap-add NET_RAW`, `XDG_RUNTIME_DIR` pinned. First pass (S-09 A, not pinned): copied nmap SYN and ACK scans, tcpdump AF_PACKET capture. | proven by the rerun. The stock nmap does not run under NET_RAW alone (S-09 Finding 2). The fix for that is not tested. |

Not added: NET_ADMIN. S-09 C shows that NET_ADMIN lets the stock nmap run, but the stock binary is not needed (S-09 Finding 2 and 6).

Open, not decided: remove the file capabilities from `/usr/lib/nmap/nmap` in the Kali realm (S-09 Finding 2). This is a candidate fix and was not tested. It removes a capability rather than adding one, so it is not in the enumerated set. It conflicts with pulling the official image by digest unless a build step is added (CR-007). Decision for T10.1.

## Not in this document

- FR-085 (Docker in a realm) is deferred. Its options will be added here when it is decided (CL-2).
- NVIDIA CDI devices (FR-069) are covered by ADR-012 (Accepted in part; see the Basic table for what is pending).

## Open items for the M6 host test

The M6 host test `tests/integration/test_tiers_real.py` must prove these. The spikes did not.

- The full Development set was run together in S-07f (case A, Debian realm): `/dev/kvm`, keep-groups, `unmask=/proc/*`, `/dev/fuse`, `/dev/net/tun`, the seccomp profile with only `sethostname`, the Realm Home config files and `useradd -K`, with no SYS_ADMIN. Its scope is in ADR-006. The items below are still open.
- `XDG_RUNTIME_DIR` set inside the realm. Tested only unset (S-07e2, S-07f). A realm that sets it moves the inner runroot.
- A Realm Home bind-mounted from the host. S-07e2 and S-07f used the outer container's own `/home/dev`. The owner, the permissions and the keep-id mapping of a bind mount were not tested.
- Inner podman on Arch and Fedora. Not run: S-07f ran only `useradd -K` on those images.
- A services-on realm. Not tested under this set. The containers.conf pin is untested until M7.
- Setuid workflows under no-new-privileges: sudo, su, passwd, ping, and the FR-043 AUR flow where makepkg escalates. Not tested (see the no-new-privileges rule above). Revisit in M6.
- The Realm package route for podman inside the realm (CR-007). Open. The user decides how the request is made (CL-8).
- KVM ioctls, not only open() (S-07c K1, S-07f).
- `setdomainname` through `--domainname` in an inner container, under the sethostname-only profile (S-07e follow-up 2).
- A realm created as Basic and later changed to Development. The `-K` rule applies at create time. It does not remove a subordinate line that already exists. Not tested.
- The disk and speed cost of the vfs form, if it is chosen.
- Other distributions (Arch, Fedora, Kali) and other podman versions. Tested so far: a Debian stable image with podman 5.4.2 inside, and podman 5.8.8 outside.
- Rebuild repeats the `-K` user creation (T8.3).
- The Basic grant under crun, with and without `keep-groups`. S-06 ran on runc only (ADR-012, pending T6.6).
- The CDI `env` field for `LD_LIBRARY_PATH` (pending T6.6, ADR-012).
