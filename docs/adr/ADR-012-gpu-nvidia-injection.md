# ADR-012: GPU and NVIDIA injection

- Status: Accepted, for FR-046, and for the parts of FR-069 that S-06 ran: `nvidia-smi` in the realm showed host driver 595.104.02, with GLX offload, EGL and Vulkan. Pending T6.6: the CDI `env` field, and every item under "Did not run" below.
- Date: 2026-10-12
- Renumbered from ADR-008 on 2026-10-12, because ADR-008 went to session socket rebind. `spikes/results/S-06.md` section 7 still carries the heading "Recommendations for ADR-008". The spike file is owned elsewhere and is not edited here.
- Amended: 2026-10-12 (D6). The status is set to what S-06 ran. The S-06 method is cited from its variant table. The catatonit fact is corrected, and DR-010 and FR-082 are added. No decision changes.

## Context

FR-046 (Must): an exported GUI app in a Basic-tier realm renders on Wayland or X11 with a GL renderer that is not software, plays audio, and uses the GPU (AC-1 and AC-2). FR-061 (Must): Basic grants, among other things, GPU device nodes and the display and audio sockets. FR-069 (Must): on glibc hosts with the NVIDIA proprietary driver, the host's matching userspace libraries and device nodes are made available read-only in realms with GPU permission. Its AC-1 requires that `nvidia-smi` in a Basic realm reports the GPU and the host driver version. R-005: a host driver update can break bind-mounted libraries while a realm is running. IR-025 names the display and audio sockets, and IR-026 names the GPU device nodes and the NVIDIA host libraries as interfaces. CR-007 limits realm contents, which bears on any file the GPU path writes into the realm.

Ranked characteristics (SRS section 4): host integrity and isolation comes second. The NVIDIA path binds host files into the realm, so the binds must be read-only and limited to the names ginn generates.

Measured by S-06 (`spikes/results/S-06.md`) on the reference host:

- Void glibc 2.41, kernel 7.2.9_1, a Wayland session with Xwayland on `:0`.
- GPUs: AMD Radeon 660M (amdgpu, `card0`, `renderD128`) and NVIDIA GeForce RTX 2050 (nvidia-drm, `card1`, `renderD129`). NVIDIA kernel driver 595.104.02. Host Mesa 26.2.4.
- podman 5.8.8 rootless with **runc 1.5.2**. crun was not installed, so every S-06 result is a runc result.
- Realm image: Debian trixie stable, Mesa 25.0.7.

## S-06: what ran, and what did not

Ran:

- The Basic grant (S-06 section 2) on runc. The AMD renderer string on the Wayland EGL platform (`eglinfo -p wayland`) and on X11 through Xwayland (`glxinfo`) is `radeonsi`. glmark2 (x11 flavour) scored 10870 in an X11 window. Vulkan `GPU0` is RADV on the AMD GPU.
- The NVIDIA grant through a CDI spec written by `spikes/s06/nvidia-cdi.py`: 7 device nodes and 70 read-only bind mounts. `nvidia-smi` reports the RTX 2050 with driver 595.104.02 after ldconfig as container root (variant b), and it returns rc 0 with `LD_LIBRARY_PATH` set on the command (variant a, S-06.md:114). GLX with the NVIDIA vendor string, EGL surfaceless with the NVIDIA vendor, and Vulkan `GPU1` as NVIDIA.
- The R-005 check in the generator: every library version matched the kernel driver, and `stale_libs` was empty.
- Device node reads: `nvidia0`, `nvidiactl`, `nvidia-uvm`, `nvidia-modeset` and `nvidia-caps/nvidia-cap2` were readable. `nvidia-caps/nvidia-cap1` (mode 0400) was denied.

Did not run. Each item is open. The closing task follows it in parentheses. The GPU items close in T6.6 (pending T6.6).

- crun. The Basic argv ran on runc only (pending T6.6). `--group-add keep-groups` had no effect under runc (S-06 section 2). Its effect for this grant under crun is untested. S-07c K1 showed keep-groups opening `/dev/kvm` under crun, but that is a different device in a different run, so it does not stand in for an S-06 crun run.
- A drawn Wayland window. The Wayland check creates an EGL context and reads its renderer. The image has no Wayland GL client (pending T6.6).
- NVIDIA EGL on the Wayland platform. `eglInitialize` failed (rc 1). Not root-caused. FR-046 is decided on AMD, so this does not gate it (pending T6.6).
- Audio. The image has no `pactl`. The PipeWire and Pulse sockets were bound and visible, and no connection was made (pending T6.6).
- An exported app launched from the host menu, which is the flow in FR-046 AC-1. S-06 ran the renderer checks, not the export path (pending T5.4 and T6.6).
- The CDI `containerEdits.env` field (pending T6.6). `LD_LIBRARY_PATH` was set on the command, not through the spec.
- A driver mismatch. The host has one driver version, so the NVML error `Driver/library version mismatch` was not reproduced. The R-005 check was tested in the match case only (pending T6.6).
- A driver update and the regeneration of the spec (pending T6.6).
- Intel GPUs. The reference host has none. The release gate covers AMD or Intel (SRS section 9, gate 1), so the Intel path is untested. No task in the roadmap names an Intel host.
- musl hosts. NVIDIA is informational on musl (FR-069 AC-2). Not run (pending M7, T7.7).
- Arch, Fedora and Kali images. Only Debian trixie ran (pending M4 and M10).
- Services-on realms. Not run (pending T7.7).

## Decision

Basic GPU grant. The grant is `--device /dev/dri`, which exposes `card0`, `card1`, `renderD128` and `renderD129` in the realm. `card1` and `renderD129` belong to the NVIDIA DRM driver, so this grant also exposes the NVIDIA DRM nodes. Mesa did not use them for the AMD renderer (S-06 section 3). Passing per-node devices instead is untested and is left to M6. The grant also includes read-only binds of fonts and `/etc/localtime`, the display and audio socket binds (`wayland-1`, `/tmp/.X11-unix`, `pipewire-0`, `pulse/native`), and the environment `WAYLAND_DISPLAY`, `DISPLAY` and `XDG_RUNTIME_DIR=/run/user/<uid>` (S-06 sections 2 and 5). The runtime directory is a tmpfs mounted with `U`, not `uid=`, because podman refused `uid=`. `U` gives owner 1000:1000, and plain `mode=0700` gives owner 0:0 (S-06 section 2). The S-06 run had no `--init`. Catatonit was absent on the host (S-06.md:48), and podman failed the init lookup there (`lookup init binary: exec: "catatonit"`). Podman looks the init binary up on the host, so the image is irrelevant. FR-082 needs catatonit from packaging (ADR-007, T0.6). The `keep-groups` flag is not decided by this ADR. It stays in the S-06 argv until M6 tests it under crun.

NVIDIA through a generated CDI spec (FR-069). ginn generates the spec from the host driver files, with the logic of `spikes/s06/nvidia-cdi.py`. The spec lists the device nodes, the read-only bind mounts (the libraries under `/usr/lib/ginn-nvidia/<name>`, the vendor JSON files at their host paths, and `nvidia-smi` at `/usr/bin/nvidia-smi`), and an ld.so drop-in. The realm receives it through `--cdi-spec-dir <CDI dir> --device nvidia.com/gpu=all`. The spec lives under the ginn data directory (roadmap, NVIDIA item). ginn regenerates the spec when `/proc/driver/nvidia/version` changes, and it refuses to start a GPU realm while the spec's `stale_libs` list is not empty (R-005).

Library resolution. The drop-in alone does nothing. The loader reads only `/etc/ld.so.cache`, and `nvidia-smi` exited with rc 12 (S-06 section 4, variant (c)). Two forms worked:

- `LD_LIBRARY_PATH=/usr/lib/ginn-nvidia`, tested as `env LD_LIBRARY_PATH=/usr/lib/ginn-nvidia nvidia-smi` in the realm (variant (a), S-06.md:114). This is the form chosen for M6. It needs no root step and writes nothing into the realm. S-06's recommendation text (S-06.md:119) names `podman exec -e` for the same test. That disagrees with its own table. This ADR follows the table, and the owner of S-06 must reconcile the two. M6 must first test the form through the CDI `containerEdits.env` field, which S-06 did not test (pending T6.6).
- `ldconfig` run as container root (`--user 0:0`), which worked (variant (b)). This is the fallback. It writes `/etc/ld.so.cache` into the realm's upper layer, and that cache is lost when the container is recreated. A write into realm /etc is a CR-007 question. The fallback therefore needs its own clarification, as CL-10 does for ADR-007, before it is used.

GL selection. For NVIDIA GLX, the environment is `__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia`. `DRI_PRIME=1` is not an NVIDIA route: on this host it failed to load `nouveau` and fell back to zink on the AMD RADV device (S-06 section 3). Every GPU check must name its platform (`eglinfo -p wayland`, or `glxinfo` with `DISPLAY`), because a bare `eglinfo` reports llvmpipe through the GBM platform (S-06 section 6). Realm HOME: keep-id leaves `HOME` at `/`, and Mesa's shader cache then fails. M6 sets `HOME` and `XDG_CACHE_HOME` (S-06 section 6).

## Alternatives considered

- Drop-in only, with no ldconfig and no environment variable. Rejected. `nvidia-smi` exits with rc 12 (S-06 variant (c)).
- `ldconfig` as container root as the primary form. Not chosen. It works (variant (b)), but it writes `/etc/ld.so.cache` into the realm, which CR-007 does not allow as written, and the cache is lost on recreate. It is kept as the fallback.
- `DRI_PRIME=1` for NVIDIA. Rejected. On this host it loads zink on AMD RADV (S-06 section 3).
- Per-node devices instead of `/dev/dri`, so that the NVIDIA DRM nodes stay out of Basic realms. Not tested. Left to M6, with the node list it would need.

## Consequences

- Positive: on the reference host, AMD GL reports a non-software renderer on Wayland EGL and X11 GLX (the FR-046 renderer check). In the realm, NVIDIA `nvidia-smi` reports the host driver (FR-069 AC-1, on runc). The NVIDIA path writes nothing to the host, and it needs no root step at run time.
- Negative: the NVIDIA DRM nodes are visible in Basic realms through `/dev/dri`. `nvidia-cap1` (mode 0400 on the host) is in the spec and unreadable from the realm. `nvidia-smi` and GL do not need it. A MIG or fabric path that does would fail. The generator includes it anyway. Whether it should skip nodes the user cannot open is open (S-06 section 6).
- R-005: a driver update removes the versioned library names the spec lists, so the next container start fails at the mount step until the spec is regenerated. This is inferred from the design and was not tested. The NVML mismatch error itself was not reproduced.
- The spec and the drop-in are generated files under the ginn data directory, which is an Owned Path (DR-010, roadmap NVIDIA item).
- The M6 task T6.6 must match this ADR. Its `ldconfig` step is the fallback, and `LD_LIBRARY_PATH` through the CDI env is the form to test first.
- Follow-up tasks: M6 (T6.6 in the roadmap), a crun run of the Basic grant with `keep-groups`, the CDI env field test, the Wayland NVIDIA EGL failure, the audio connection test, the drawn-window test, and a clarification for the ldconfig fallback under CR-007.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| FR-046 | The Basic grant renders with a non-software GL renderer on AMD, on Wayland EGL and X11 GLX (S-06 section 3, runc). The Wayland check is an EGL context, not a drawn window. Audio and the menu-launch flow were not run. |
| FR-061 | The Basic grant: `--device /dev/dri`, read-only fonts and localtime, the display and audio socket binds, and the environment (S-06 sections 2 and 5). The `~/Downloads` and mount-root binds of FR-061 were not run. They are design-only, and `docs/capabilities.md` lists them. |
| FR-069 | NVIDIA through a generated CDI spec. `nvidia-smi` reports the host driver 595.104.02 in the realm (S-06 section 4, runc). The CDI `env` field and the mismatch case are pending T6.6. musl was not run. |
| DR-010 | The generated CDI spec and the ld.so drop-in are written under the ginn data directory, which is an Owned Path. The realm reads the host libraries read-only. Nothing else on the host changes. |
| FR-082 | The GPU grant runs without `--init`, because catatonit was absent on the host (S-06.md:48). Podman looks the init binary up on the host, so the image does not matter. Services-off realms need catatonit from packaging (ADR-007, T0.6). |
| R-005 | The generator checks each library version against `/proc/driver/nvidia/version` and reports `stale_libs`. The match case ran. The mismatch case did not, and the start is refused on a non-empty list (decided, not tested). |
| IR-025 | The display and audio sockets that the Basic grant binds (S-06 section 2). |
| IR-026 | The GPU device nodes and the NVIDIA host libraries are the interface this ADR decides (S-06 sections 2 and 4). |
| CR-007 | The primary form (`LD_LIBRARY_PATH`) writes nothing into the realm. The fallback (`ldconfig` as container root) writes `/etc/ld.so.cache` into it, which needs a clarification first. |
