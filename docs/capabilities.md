# Capabilities and devices per tier

This document lists every podman option that ginn adds to a realm beyond the base argv that all realms share. It is the enumerated set that NFR-SEC-002 refers to. `security/caps.py` (T6.1) must produce exactly this set, and the per-tier argv test compares against it.

The set is accepted (decision of 2026-10-12). The Development part follows ADR-006, as amended on the same day. Each row names its evidence: a spike ID and a case ID in `spikes/results/`. Rows marked "pending" are not yet proven on the reference host.

## Base argv (all tiers)

Options every realm carries. None adds a capability or a device.

- `--runtime crun`, and the global flags `--root`, `--runroot` and `--tmpdir` (ADR-004, IR-020).
- `--name ginn-<realm>-g<generation>` (ADR-003, FR-083), `--user <uid>:<gid>` with `--userns keep-id` (roadmap, design choices; the ids are the host user's, AS-007), `--network host` for non-Kali realms (AS-006), and `--init` when services are off (FR-082). No `--init-path` is passed. Only S-05r ran plain `--init` (services off, no `--init-path`), and it succeeded through podman's lookup path. S-07 and S-08 passed `--init-path`, and S-09 dropped `--init`. Void installs catatonit at `/usr/libexec/catatonit/catatonit`, with podman's lookup link at `/usr/libexec/podman/catatonit` (S-05r). `/usr/bin/catatonit` is absent on the reference host (S-05b).
- Labels `--label io.ginn.realm=<realm>`, `--label io.ginn.distro=<distro>` and `--label io.ginn.generation=<n>` (T4.1). ginn touches only containers that carry these labels, so it never acts on another container. Its `ps` filters use these keys (T4.4, T4.5).
- Keep-alive for a services-off realm: `--entrypoint '["sleep"]'`, with the command `infinity` after the image (ADR-010 spike command, FR-082). Both are given explicitly, so the image's CMD does not matter (T4.1).
- Creation from a digest reference in the Trust Manifest (FR-011, ADR-005).
- Create-time binds with stable sources only: the Realm Home at the host `$HOME` path, `--tmpfs /run/user/<uid>`, host fonts and localtime (Basic table), device nodes, and `--http-proxy=false` (T4.1). Variable grants are not create-time binds. They are attached after start (ADR-008 amendment, Basic table).
- No `--privileged`, and no `--cap-add ALL`. The argv builder in `src/ginn/podman/adapter.py` refuses both (NFR-SEC-001).

## Basic

FR-061 grants exactly: `~/Downloads` (read-write); the external drive mount roots `/run/media/$USER`, `/media` and `/mnt` (read-write); the display socket(s); the audio socket(s); the GPU device nodes; and the host fonts and timezone (read-only). The table lists the options that carry each grant, plus the NVIDIA items of FR-069 that need GPU permission. One row, the X11 authority file, is not named by FR-061. It serves the display grant under FR-046 AC-2. Each row is marked exercised or design-only. "Exercised" means a spike ran the option on the reference host, and the row names the spike and section. "Design-only" means no spike ran it. `security/caps.py` (T6.1) must produce exactly this set. Each row also says when it is applied. A create-time row is part of the `podman create` argv (T4.1, and T4.2 in `runtime/mounts.py`). A start-time row is attached after `podman start` by the helper, under the ADR-008 amendment (T4.10 in `runtime/attach.py`). A row marked exec is set by `runtime/env.py` (T4.3) when ginn runs a command in the realm. The T6.1 test compares the create argv, the exec argv and the attach plan with this list.

| Item | Applied | Option | Grant | Evidence | Status |
|---|---|---|---|---|---|
| Downloads | start (attach) | Attach `<home>/Downloads` read-write at the same realm path, after `podman start`. The realm mountpoint is an empty directory in the Realm Home. When the host folder is missing, the grant is skipped and recorded in `RealmRecord.share_skipped`, and ginn does not create the folder (FR-064). | FR-061, FR-064, FR-065 | none | design-only (T4.10; host gate T4.9). ADR-012 records that it was not run. ADR-008 amendment. |
| Mount roots | start (attach) | Attach `/run/media/$USER`, `/media` and `/mnt`, read-write, at the same paths, after `podman start`. A drive mounted below a root after start reaches the realm at the next enter or launch, through the lazy re-attach. Host mounts are private, so `rslave` propagation is not used. | FR-061, FR-065 | none | design-only (T4.10; host gate T4.9, which must include a drive mounted after start). Not run. |
| Display socket | start (attach) | Attach the Wayland socket, under any host name, at `/run/user/<uid>/wayland-0`. The host name comes from `WAYLAND_DISPLAY`, as a socket name under the host runtime directory or an absolute path. The source must be a socket. A name that is, or resolves to, the D-Bus socket `bus` is refused (AS-012). The realm always sees `wayland-0`, as the session environment row says. Attach `/tmp/.X11-unix` read-only at the same path. The environment variables are in the session environment row. | FR-061 | S-06 section 2 (runc), as create-time binds, under the host socket name | exercised as create-time binds under the host name. The Wayland EGL and X11 GLX renderer checks ran through these binds (S-06 section 3). The fixed realm name `wayland-0` and the attach form are pending the T4.9 host gate. |
| X11 authority file | start (attach) | Decided (2026-10-12). Attach the file that the host `XAUTHORITY` names, read-only, at the fixed realm path `/run/user/<uid>/ginn-xauthority`. Exec sets `XAUTHORITY` to that path (session environment row). Only when the host sets `XAUTHORITY`. A copy of the file is not used, because it would add a second cookie file to disk. A rotated cookie file is an ordinary re-attach (ADR-008 amendment). | FR-046 AC-2. FR-061 names no authority file. | none. S-06 section 5 recommends the bind. `XAUTHORITY` is unset on the reference host (S-06 section 2), so no mount ran. | decided. Design-only until the T4.9 host gate and a real Xorg session (FR-046 AC-2). Without the attach, Xlib is expected to fall back to `$HOME/.Xauthority` in the Realm Home, where no file exists, and the X connection is expected to be refused. Not tested. |
| Audio socket | start (attach) | Attach `$XDG_RUNTIME_DIR/pipewire-0` at `/run/user/<uid>/pipewire-0`. Attach the Pulse socket `pulse/native` at `/run/user/<uid>/pulse/native`. | FR-061 | S-06 section 2 (runc), as create-time binds | exercised as binds only: both sockets were bound and visible. No connection was made, because the image has no `pactl`. The attach form is pending the T4.9 host gate. |
| GPU device nodes | create | `--device /dev/dri`, which exposes `card0`, `card1`, `renderD128` and `renderD129`, including the NVIDIA DRM nodes | FR-061 | S-06 sections 2 and 3 (runc) | exercised on runc. The AMD renderer checks ran. Pending crun (T6.6). |
| Host fonts | create | `-v /usr/share/fonts:/usr/share/fonts:ro` | FR-061 | S-06 section 2 (runc) | exercised |
| Host timezone | create | `-v /etc/localtime:/etc/localtime:ro` | FR-061 | S-06 section 2 (runc) | exercised |
| Runtime directory (support for the session paths; FR-061 names no directory) | create | `--tmpfs /run/user/<uid>:mode=0700,U`. `XDG_RUNTIME_DIR` is set at exec (session environment row). | ADR-012 Decision | S-06 section 2 (runc) | exercised. `U` gives the realm user as owner. Plain `mode=0700` gives owner 0:0. podman refused `uid=`. |
| NVIDIA libraries and nodes (FR-069, when the host has the NVIDIA driver) | create (CDI); `LD_LIBRARY_PATH` at exec | `--cdi-spec-dir <CDI dir> --device nvidia.com/gpu=all`, with the spec written under the ginn data directory; `LD_LIBRARY_PATH=/usr/lib/ginn-nvidia` | FR-069, GPU permission | S-06 sections 2, 4 and 5 (runc) | exercised: `nvidia-smi` and the GLX, EGL and Vulkan checks ran with the spec attached. Pending T6.6: the CDI `env` field, the mismatch case, and crun. |
| Session environment | exec | `HOME` (the host home path), `XDG_RUNTIME_DIR=/run/user/<uid>`, `WAYLAND_DISPLAY=wayland-0` (the realm name, whatever the host socket is called), `DISPLAY`, `XAUTHORITY` (the fixed realm path, when the authority file is attached), `LANG`, `TZ`, and the proxy variables. `runtime/env.py` sets these at `podman exec` and at launch, not at create. Proxy variables go on the argv as `--env NAME` only, never as `NAME=VALUE`, so their values do not appear in `/proc/<pid>/cmdline`. `runtime/env.py` returns their names in `SessionEnv.pass_through` and never their values. Podman reads the value from its own environment. | FR-061, FR-046 AC-2 | S-06 section 5 (runc) set `WAYLAND_DISPLAY`, `DISPLAY` and `XDG_RUNTIME_DIR` on the command. S-06 section 6 covers the keep-id `HOME` problem. | design. Not run as exec-time environment on a host. ADR-004's subprocess allowlist (CR-006) holds the six proxy names since its 2026-10-12 amendment, so podman can read them. T4.6 implements the pass-through. |

Where the Option column shows podman `-v`, ginn emits the same bind as `--mount` in the create argv. Start-time rows are not argv at all. The helper attaches them after `podman start` (ADR-008 amendment).

Not in this set:

- `--group-add keep-groups`. S-06 passed it on runc, and it had no effect there (S-06 section 2). ADR-012 leaves it undecided until M6 tests it under crun. It is not in the Basic set. The Development table lists it as an option of that tier.

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

The base argv also carries `--runtime crun`, `--userns keep-id`, `--passwd-entry <entry>`, `--network host` and `--init`. Dated 2026-10-13 (T4.9b): `--passwd-entry` carries the realm user, which is the entry podman writes for the keep-id user. `--passwd=false` is never sent, because podman 5.8.8 refuses it on `podman create` (ADR-006 amendment, T4.9b). The argv form is in `CreateSpec.to_argv`. The S-07e and S-07f runs used the same options, with `--init-path` pointing at a spike copy of catatonit. The product argv has no `--init-path`. ginn adds no `--cap-add`, and nothing in this table grants a capability.

### Attach rows (start-time)

Development adds these variable grants to the Basic ones. Each one is attached after `podman start`, under the ADR-008 amendment, as the Basic table says. A missing source is skipped and recorded (FR-064). The T6.1 test compares the attach plan of a Development realm with this table. The start path plans these rows from the record each time, and `ginn tier` and `ginn perms` (T6.2) change the record.

| Item | Applied | Option | Grant | Evidence | Status |
|---|---|---|---|---|---|
| `~/Projects` | start (attach) | Attach `<home>/Projects` read-write at the same path. The realm mountpoint is an empty directory in the Realm Home. | FR-062 | none | design-only (T6.2) |
| `~/.bashrc`, `~/.zshrc` | start (attach) | Attach each file read-only at the same path. The file is optional: a missing one is no grant and no skip, and the next start tries it again. The AS-011 rule admits a read-only share of these files. | FR-062 AC-2, AS-010 | none | design-only (T6.2) |
| SSH and GPG agent | start (attach) | Agent Proxy sockets. Owned by `security/agent.py` (T6.4), not in this table, and it adds no option to `security/caps.py`. | FR-062, FR-067 | none | pending T6.4 |

### Realm contents and create-time steps

These steps run inside the realm or in the Realm Home. They do not change the host.

| Item | Change | Why | Evidence | Status |
|---|---|---|---|---|
| Realm user | The realm user is podman's keep-id passwd entry. It is sent at create as `--passwd-entry <user>:*:<uid>:<gid>:<user>:<host home>:<login shell>` (T4.9b, ADR-006 amendment). ginn runs no `useradd`. After start, `getent passwd <uid>` must return that entry exactly, or the create fails with GINN-E-REALM-USER and rolls back. Nothing writes a line for the realm user into `/etc/subuid` or `/etc/subgid`. | Plain `useradd` adds `dev:100000:65536`, and inner podman then calls newuidmap, which fails without CAP_SYS_ADMIN. Without the entry flag, podman writes `:/:/bin/sh`, which is neither the host home nor the login shell. | T4.9 section 4.3 (`--passwd=false` refused on podman 5.8.8, exit 125; the injected entry). T4.9b: on debian, gate items d-create-product-argv (entry, one uid line, host home, no subuid or subgid line) and d-control-without-passwd-entry (injected `:/:/bin/sh`) pass. S-07e2 and S-07f show the subuid cause. | exercised on debian (T4.9b). Arch and Fedora: `getent` and the entry are unverified. |
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
| No `/etc/subuid` or `/etc/subgid` line for the realm user. | A line makes inner podman call newuidmap, which fails. | S-07e E0 (rc 125). S-07e2 E1d-u1 (rc 125). T4.9b: none after create on debian (gate item d-create-product-argv, `subid_lines_for_user` 0). |
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

- The realm user is the keep-id passwd entry (T4.9b). It is not a package addition, and no `useradd` runs.
- The two config files go into the Realm Home. They are ginn-owned data (DR-006).

Two points stay open:

- The image must contain podman and its helpers. A realm can get them only as requested packages, which CR-007 allows. FR-013 AC-1 allows no other additions, so ginn does not add podman by itself. S-07e2 left the request path to the user.
- The realm user entry is not in the Install Manifest. FR-021 names only the Install Manifest for replay. The rebuild sends the same entry by design (T8.3, ops/rebuild.py). The SRS does not say so.

CL-8 is withdrawn for the setcap and libcap steps. See `docs/clarifications.md`.

## Custom (FR-063)

The SRS does not define a Custom capability set. Each Custom toggle for a permission in FR-061 or FR-062 adds only the options in the Development table above, or one of the grants named in its row. No toggle adds a capability. Which option each toggle adds is set in T6.1 and T6.2. A toggle that would add any option not listed here requires this document to change first (NFR-SEC-002). The D-Bus filter widening (AS-012) is not a podman option and is not in this table.

### Custom toggles

Toggle names (`ginn perms --enable` and `--disable`, domain/permissions.py) and what each one adds:

| Toggle | FR | Adds at create | Adds at start (attach) |
|---|---|---|---|
| `downloads` | FR-061 | nothing | `~/Downloads`, read-write |
| `mount_roots` | FR-061 | nothing | `/run/media/<user>`, `/media` and `/mnt`, read-write |
| `display` | FR-061 | nothing | the Wayland socket, read-write, and the X11 socket directory and the X11 authority file, read-only, with `WAYLAND_DISPLAY`, `DISPLAY` and `XAUTHORITY` |
| `audio` | FR-061 | nothing | the PipeWire and PulseAudio sockets, read-write |
| `gpu` | FR-061, FR-069 | the `/dev/dri` device and, when the host has the NVIDIA driver, the NVIDIA CDI grant (see the note below) | nothing |
| `fonts_timezone` | FR-061, AS-009 | the host fonts and the host localtime, read-only, when the host has them (see the note below) | `TZ`, when the host sets it |
| `projects` | FR-062 | nothing | `~/Projects`, read-write |
| `shell_rc` | FR-062 | nothing | `~/.bashrc` and `~/.zshrc`, read-only |
| `agent` | FR-062, FR-067 | nothing: the Agent Proxy adds no create-time option (`security/agent.py`, T6.4) | the Agent Proxy sockets (T6.4) |
| `nested` | FR-062 AC-3 | `--device /dev/fuse`, `--device /dev/net/tun`, `--security-opt unmask=/proc/*`, `--security-opt seccomp=<generated profile>`, and the Realm Home config files | nothing |
| `kvm` | FR-062 AC-4 | `--device /dev/kvm` and `--group-add keep-groups`, only when the host has `/dev/kvm` at create | nothing |

Basic is `downloads`, `mount_roots`, `display`, `audio`, `gpu` and `fonts_timezone`. Development is every toggle. A Custom realm starts from Basic's toggles, and it keeps the toggles of the tier it came from when it is switched to Custom. A toggle that is off plans no bind, device or variable for its grant.

The `gpu` and `fonts_timezone` grants are create-time. Their binds and the `/dev/dri` device are fixed at `podman create`, so they are not in the option names of `option_keys`. The record keeps the state of each toggle at create (`gpu_at_create` and `fonts_at_create`), and a change of either toggle is recorded at once. The next start then rebuilds the realm into a container that has the new grant (FR-066, T8.3, ops/lifecycle.py `_stale_container`). The NVIDIA CDI grant is made only when `gpu` is on and the host driver is ready (`gpu_cdi`). A create-time option (`nested`, `kvm`) is handled the same way. `display` and `audio` change attach-time grants only: `ginn perms` flags the realm restart-required, and the change applies at the next start.

A stored Custom part without a `format` key (format 1) keeps the grants that format 1 always attached: `display`, `audio`, `gpu` and `fonts_timezone` are on for it, so an upgrade changes no realm's grants.

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
- A realm created as Basic and later changed to Development. No line for the realm user is written at create time (T4.9b). A later change to the realm user is not tested.
- The disk and speed cost of the vfs form, if it is chosen.
- Other distributions (Arch, Fedora, Kali) and other podman versions. Tested so far: a Debian stable image with podman 5.4.2 inside, and podman 5.8.8 outside.
- Rebuild sends the same passwd entry (T8.3): ops/rebuild.py plans it through the create validation, as create does.
- The Basic grant under crun, with and without `keep-groups`. S-06 ran on runc only (ADR-012, pending T6.6).
- The CDI `env` field for `LD_LIBRARY_PATH` (pending T6.6, ADR-012).
