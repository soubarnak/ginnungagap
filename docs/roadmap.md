<!-- Roadmap of record for GINN-SRS-001 v1.0. Task IDs are cited in commits and tests. -->
# Ginnungagap (`ginn`) roadmap to v1

This document is the roadmap to v1 of GINN-SRS-001. It sets the target architecture, the spikes that retire the main technical risks, the milestones and their tasks, the testing strategy and the checks that close each milestone. The SRS remains the single source of truth for requirement IDs and behaviour.

## Target architecture

```
src/ginn/
  __main__.py  errors.py (GINN-E catalogue, NFR-040)
  cli/          main.py output.py commands/*.py      IR-010, --json, --verbose, exit codes IR-011
  domain/       names.py packages.py paths.py enums.py realm.py states.py   DR-002..013, DR-020
  distros/      loader.py data/{arch,debian,fedora,kali,_test}.toml        NFR-060, FR-013
  trust/        manifest.py data/trust.toml                                 DR-040
  store/        paths.py records.py locks.py journal.py audit.py           DR-010, DR-035, FR-123, FR-122, DR-050
  podman/       adapter.py (Protocol) cli.py models.py
  runtime/      spec.py mounts.py env.py attach.py launch.py enter.py       FR-061..065, FR-048/049
  hostsetup/    setup.py subids.py cgroups.py runit_system.py record.py undo.py   FR-001..003
  doctor/       checks.py report.py                                         FR-007
  integrity/    fingerprint.py state.py                                     FR-006
  packages/     manager.py manifest.py aur.py kali.py                       FR-040..043, FR-014
  export/       desktop.py icons.py bins.py sync.py                         FR-044..047
  services/     runit_user.py supervisor.py autostart.py                    FR-080..084
  security/     caps.py dbus_filter.py agent.py nvidia.py                   NFR-SEC-002, FR-067..069
  ops/          create.py rebuild.py update.py delete.py retry.py disk.py recovery.py   FR-010/019/021/023, FR-120..122
  gui/          app.py backend.py viewmodels/*.py views/*.py                FR-100..107
native/   ginn-wait.c ginn-watch.c ginn-idle.c ginn-agent-filter.c ginn-rebind.c  (each <= 200 lines; no ginn-run.c for v1, ADR-010)
spikes/   docs/adr/  docs/errors.md  docs/traceability.md  docs/capabilities.md  docs/reference/
packaging/void/srcpkgs/ginn/template  tools/{release.sh,xbps-build.sh,provenance-scan.sh,traceability.py}
tests/{unit,property,integration,gui,fakes,shims,fixtures}
```

Key design choices, one reason each:

- Podman CLI with `--format json`, not libpod REST: REST needs a resident `podman system service`, which breaks NFR-001.
- Dedicated `--root $XDG_DATA_HOME/ginn/podman --runroot <runtime>/podman`: isolates realms from the user's own podman; is an Owned Path.
- `--userns keep-id`: Realm Home files owned by the host uid; services-on adds `--user 0:0` so systemd is PID 1.
- Pull by digest `repo@sha256:...` from `trust.toml`: mismatch leaves no layers (FR-011 AC-1 intrinsic).
- Container name `ginn-<realm>-g<N>`, record stores the ID; rebuild = atomic record swap to the new generation (DR-034). Each container carries the labels `io.ginn.realm=<realm>`, `io.ginn.distro=<distro>` and `io.ginn.generation=<N>` (T4.1). ginn touches only containers that carry these labels.
- Services off: `--init` (FR-082), with no `--init-path`. Only S-05r ran plain `--init` (services off, no `--init-path`), and it succeeded through podman's lookup path. S-07 and S-08 passed `--init-path`, and S-09 dropped `--init`. Void installs catatonit at `/usr/libexec/catatonit/catatonit`, with podman's lookup link at `/usr/libexec/podman/catatonit` (S-05r). `/usr/bin/catatonit` is absent on the reference host (S-05b). The catatonit pause alone does not prove the `--init` lookup. Services on: `--systemd=always --cgroupns private --cgroup-parent /ginn/<uid>/<realm> --user 0:0 --stop-signal SIGRTMIN+3 --stop-timeout 30 --runtime crun`, added to the realm's create argv and started only through the runit supervisor (ADR-003).
- Network: non-Kali `--network host` (AS-006); Kali `--network pasta --cap-add NET_RAW` (FR-026/027).
- No resident Python (NFR-002 8 MiB). Small C helpers: `ginn-wait` (what runit supervises; pidfd on conmon, SIGTERM -> `podman stop`), `ginn-watch` (inotify on realm `/usr/share/applications`, execs `ginn _sync`), `ginn-idle` (60 s idle stop, FR-018), `ginn-agent-filter` (ssh-agent allow-list: identities, sign, extension), `ginn-run` (not built for v1: S-02 measured `podman exec` within 300 ms on runc, and ADR-010 returns it to `native/` if T4.9 misses 300 ms on crun), `ginn-rebind` (grant attachment and session socket rebind for every start, enter and launch, ADR-008 amendment). All policy stays in Python.
- Grant attachment (ADR-008 amendment): create-time binds hold only sources that stay put (Realm Home, fonts, localtime, devices, the runtime tmpfs). Every variable grant (tier folders, mount roots, session paths at fixed realm names, Custom shares) is attached after `podman start` by `ginn-rebind`, under `podman unshare` and `setns` into the mount namespace, and re-attached lazily when its host source changes. Podman cannot add a mount to a running container, and a missing bind source stops the start.
- GPG: bind host `S.gpg-agent.extra` (restricted by GnuPG design) as the realm's `S.gpg-agent` (FR-067 AC-2).
- D-Bus: `xdg-dbus-proxy` (Void package, a dependency not copied code) `--filter --talk=org.freedesktop.Notifications --talk=org.freedesktop.portal.*`; Custom tier appends names (FR-068).
- NVIDIA: ginn generates a CDI JSON under `$XDG_DATA_HOME/ginn/cdi/` from host driver files; `--cdi-spec-dir` + `--device nvidia.com/gpu=all` (FR-069, R-005).
- GUI <-> backend: GUI reads the state dir and runs `ginn --json <cmd>` as a subprocess (same uid), progress as JSON lines, SIGINT = cancel; `Gio.FileMonitor` on the realm records for NFR-010. No socket, so NFR-SEC-009 holds by 0700 Owned Paths.
- Integrity: sha256 over sorted stat tuples of AS-020 paths, stat-only so 5000 files <= 1 s (NFR-011); pending warning blocks mutating ops until `ginn integrity --ack`.
- Host Setup (root, FR-001): subids >= 524288, `/etc/sv/ginn-<user>` run script (create + chown delegated cgroup `/sys/fs/cgroup/ginn/<uid>`, create `/run/ginn/<uid>`, move itself into the `supervisor` leaf (ADR-003), `setpriv` to the user, `exec runsvdir $XDG_STATE_HOME/ginn/sv`), symlink into the directory that `/var/service` resolves to (ADR-013 Decision 10), the two supervise links into `/run/runit` (ADR-013 Decision 21), Host Setup Record `/etc/ginn/setup-<user>.json` with pre-images for undo.
- One choke point `ops/_mutating.py`: lock, integrity before/after, journal, audit entry for every mutating op (DR-032, FR-123, FR-124).

## Spikes (M1), each writes `spikes/results/S-NN.md` and closes an ADR

| ID | Risk | Question | Pass criterion |
|---|---|---|---|
| S-01 | R-001 | systemd PID 1 in rootless podman with cgroupfs manager under a root-created delegated cgroup; runc and crun | `systemctl is-system-running` = running/degraded, zero failed default units |
| S-02 | R-006 | `podman exec` vs C `ginn-run` setns launch latency, 100 runs | p95 table; pick path meeting 300 ms (NFR-004) |
| S-03 | NFR-062 | musl Void VM: rootless podman, pasta, S-01 repeat, PyGObject import | Must-FR smoke passes |
| S-04 | DR-010, FR-081 | `/run/ginn/<uid>` from runit service vs `loginctl enable-linger` | realm survives logout, visible from session `podman ps` |
| S-05 | NFR-002/003 | idle RSS: conmon + catatonit + pause + helpers + dbus-proxy; services-on Debian | <= 8 MiB / <= 40 MiB or named mitigations |
| S-06 | FR-046/069 | GPU on Wayland and X11 rootless; hand-written CDI for NVIDIA | non-software renderer; `nvidia-smi` shows host driver |
| S-07 | FR-062 AC-3, FR-085 | nested rootless podman and dockerd with `--device /dev/fuse --security-opt unmask=/proc/*` | inner `podman run alpine true` exits 0; exact extra opts recorded in `docs/capabilities.md` |
| S-08 | IR-025, FR-084 | stale session sockets after re-login | rebind via `open_tree`/`move_mount` or documented restart |
| S-09 | FR-027 | Kali `--network pasta --cap-add NET_RAW` | raw socket works; host iface change denied |
| S-10 | R-003 | image channels and digests for arch/debian/fedora/kali; which are signed | `trust.toml` v1 and refresh procedure |

Gate: if S-01 fails on both runtimes, stop and escalate before M7 (release gate 9.3).

## Milestones

Task format: id, name, files, SRS IDs, proving test, host-only flag. Every test carries `@pytest.mark.fr("FR-xxx")`. Host-only tests run with `GINN_INTEGRATION=1` on a Void host with rootless podman.

### M0 Repo reset, skeleton, CI, packaging
- T0.1 Reset the tree to the clean-room baseline; new `LICENSE` (Apache-2.0), `NOTICE`, `COPYRIGHT`, `README.md`, `AGENTS.md`; `test_repo_hygiene.py` asserts no reference-project identifiers and SPDX headers.
- T0.2 `pyproject.toml`, `src/ginn/__main__.py`, `cli/main.py` with `--json --verbose --version`; `ginn --version` exit 0, unknown command exit 2 `GINN-E-USAGE`.
- T0.3 `errors.py` + `tools/gen_errors_doc.py` -> `docs/errors.md`; test: every `GINN-[EW]-*` code in the SRS exists in the catalogue.
- T0.4 `.github/workflows/ci.yaml`: pytest as non-root on `void-glibc` and `void-musl` containers, ruff, mypy, coverage >= 80 % non-GUI, traceability check, provenance job.
- T0.5 `tools/traceability.py`; `fr` marker registered in `pyproject.toml`; fails when a Must FR has no test.
- T0.6 `packaging/void/srcpkgs/ginn/template` (depends: podman crun conmon netavark aardvark-dns passt catatonit fuse-overlayfs shadow xdg-dbus-proxy runit util-linux containers-common; `ginn-gui` subpackage deferred to M9 (T9.9)), `tools/xbps-build.sh`, `tools/release.sh` designed from public xbps-src docs: build the package, sign repodata with a locally held key, publish the repository as GitHub release assets.
- T0.7 `tests/shims/xbps-fail/` PATH shim; `test_fr004_no_xbps.py` asserts no `xbps-*` call during the suite (FR-004).
- T0.8 ADR-001 Python + GTK4/libadwaita, ADR-002 podman CLI over REST, ADR template.

### M1 Spikes S-01..S-10
Deliverables: `spikes/*.sh|py`, `spikes/results/S-*.md` (one committed result per spike), ADR-003 (S-01), ADR-004, ADR-005, ADR-006, ADR-007 (S-05), ADR-008 (S-08), ADR-010 (S-02), ADR-011, ADR-012 (S-06); ADR-009 is reserved for M9 and is not an M1 deliverable. `docs/capabilities.md` draft (per-tier argv, device and capability set for NFR-SEC-002), `trust.toml` v1 (`src/ginn/trust/data/trust.toml`). All host-only. M2 and M3 run in parallel with M1.
Status: M1 spike phase closed; open residuals. Each residual names the task that closes it.
- S-01:
  - FR-080 AC-1 and AC-2 on crun under the pin: T7.7.
  - `cgroup.controllers` read inside the crun container: T7.7.
  - A run from the root-owned `supervisor` leaf on crun: T7.7.
  - Arch and Fedora services-on boot: T7.7, parametrised over the three images.
  - Placement of exec'd processes under `--cgroup-parent` unverified: T4.6.
- S-05s itself (the create-time mitigations of ADR-007): before T7.3.
- S-03 (musl): no result file yet. The musl S-01 repeat closes in T7.7, and the musl Must-FR suite closes at the M10 gate run (T10.6). Release gate 1(b) (SRS section 9) needs a musl bare-metal host or GPU passthrough, because a Void musl VM has no AMD or Intel GPU.
- S-04 and S-08 (real logout): the real-logout runs were invalid, because the host created no elogind session. The simulated runs (S-04s, S-08s) stand in until the real logout runs. Closes in T7.8.
- S-05 helper and xdg-dbus-proxy RSS: the helpers that NFR-002 and NFR-003 count were not measured. The proxy's RSS (about 4.7 MiB) comes from S-05 and was not re-measured in S-05r. Closes in T7.7 (helpers) and T6.5 (proxy).
- S-06: the audio connection (T6.6), the menu launch (T5.4), the NVIDIA Wayland EGL failure (T6.6), and the bare `eglinfo` llvmpipe fallback root cause (T6.6).
- S-07:
  - KVM ioctls, and the Arch and Fedora nested podman runs: T6.7.
  - Part B (dockerd, FR-085, CL-2) was skipped. Re-run in T7.7, which has services on. FR-085 is deferred (CL-2). If it stays deferred, T10.6 lists it in the release notes (gate 8).
- S-09: open item 1, the stock nmap needs an image change: T10.1.
- S-10: locale installs untested: T4.4.
- S-02 on crun: T4.9. S-06 on crun: T6.6.
- The capabilities Basic row is now filled, with each option marked exercised or design-only (FR-061, D6). ADR-010 is Accepted on runc. ADR-012 is Accepted for the FR-046 renderer check (runc) and the S-06 parts of FR-069; audio, menu launch, crun and the CDI env field are pending T6.6 and T5.4.
`spikes/run-all.sh` and `spikes/RESULTS.md` are dropped. Re-running every host spike, root steps included, buys nothing over the committed per-spike results: each result file records its own environment, and the root steps are one-off host writes that a full run would repeat.

### M2 Domain, store, podman adapter, distro data (DR-001..013, DR-020, DR-030..035, NFR-SEC-005, NFR-060)
- T2.1..T2.3 `domain/names.py`, `packages.py`, `paths.py` with hypothesis property tests (10k inputs under `GINN_FUZZ=1`, shell metacharacters, unicode); forbidden-path table (NFR-SEC-006).
- T2.4 `domain/enums.py`, `realm.py`, `states.py` DR-020 transition table with previous-state memory; exhaustive transition test.
- T2.5 `store/paths.py` Owned Paths from XDG + runtime dir (ADR-004).
- T2.6 `store/records.py` atomic write (tmp, fsync, rename, dir fsync); crash-injection test (DR-035).
- T2.7 `store/locks.py` flock per realm, `GINN-E-REALM-BUSY: <op> in progress`; two-process test (FR-123).
- T2.8 `store/journal.py` + `ops/recovery.py`: dead pid -> `broken` or revert (FR-122, NFR-030).
- T2.9 `store/audit.py` JSONL append, rotate 10 MiB keep 5, `ginn log` (FR-124/125, DR-032, AS-021).
- T2.10..T2.12 `podman/adapter.py` Protocol, `podman/cli.py` subprocess impl with `--verbose` echo (NFR-042) and error mapping, `tests/fakes/podman_bin/podman` record/replay shim, `tests/fakes/fake_podman.py` in-memory adapter.
- T2.13 `distros/loader.py` + TOML per distro: image, Integration Package Set and commands, package-manager verbs (install, remove, upgrade, check-updates, list-explicit, resolve, estimate), signature-error regex, name regex, shell; `_test` distro proves NFR-060.
- T2.14 `trust/manifest.py`, `tools/refresh-trust-manifest.py` (skopeo, dev-time).

### M3 Host setup, doctor, integrity (FR-001..007, NFR-011, NFR-041)
- T3.1 `hostsetup/subids.py` append >= 524288 non-overlapping to `/etc/subuid|subgid`.
- T3.2 `hostsetup/cgroups.py` delegated subtree (mkdir, chown, `subtree_control`); test on a fake cgroupfs tree.
- T3.3 `hostsetup/runit_system.py` renders `/etc/sv/ginn-<user>/{run,finish,log/run}`; golden file + shellcheck. The run script moves its own pid into `ginn/<uid>/supervisor/cgroup.procs` before the drop to the user, so runsvdir and every service it starts sit in the delegated subtree (ADR-003). Step 3 of the script does this. The two supervise symlinks, `/etc/sv/ginn-<user>/supervise` and `/etc/sv/ginn-<user>/log/supervise`, are not part of the rendered script. Host Setup (T3.4) writes them, pointing into `/run/runit` (ADR-013 Decision 21).
- T3.4 `hostsetup/record.py`, `setup.py`: root check `GINN-E-SETUP-ROOT`, idempotent "already configured", record with pre-images; tests under a `--root-dir` prefix (FR-001 AC-2, FR-002). Setup runs before `/run/ginn/<uid>` exists, and it must not stop on that (ADR-004). F11 (ADR-013 Decisions 21 to 24): setup writes the two supervise symlinks and records them with the runit directories they point at; the per-user record is schema 4 and carries `home`; `shared.json` carries the original `/etc/subuid` and `/etc/subgid`; a schema 3 record is refused.
- T3.5 `hostsetup/undo.py` `GINN-E-UNDO-REALMS-EXIST` (FR-003). Undo rules, binding (ADR-013 Decisions 12, 15, 17 and 21 to 26; CL-11 records that AC-1 does not hold for every path). Undo takes uid, gid and home from the record, never from the current account (Decision 24). Undo takes the global lock, works from a pending record too, and dispatches on `kind` and `shared`, never on path lists. (i) Subid lines: remove only an exact `name:start:count` match that is absent from the pre-image, through `subids.remove_exact`; a differing line stays with a warning; the files are never restored whole. For the last record, each file is settled against its `shared.json` original: it is deleted only when the original was absent and it holds no line, and otherwise foreign lines are reported (Decision 23). (ii)-(iv) Shared state comes from `shared.json`, and only when the record is the last one: shared dirs are removed with rmdir only when their pre-image is absent, before root `cgroup.subtree_control` gets `-<ctrl>` for each controller the pre-image lacks, only in the same boot (cgroup v2 refuses `-<ctrl>` while a child cgroup uses it); no bytes are written back. (v) Order: remove the service link; wait for runsv to exit through `supervise/ok` (injectable timeout, GINN-E-UNDO-SERVICE-RUNNING on expiry); per-user cleanup (the supervise links and runit dirs, cgroup leaves leaf first, `/run/ginn/<uid>` only when its pre-image is absent, symlinks against their recorded targets, sv files from their pre-images); if last, shared dirs, then the controller writes, then the subid originals; then remove the records, with `/etc/ginn` leaving by one rename when Decision 25 allows it, and in place otherwise. An absent shared.json on a last record means an earlier undo applied it. Undo prints every path it leaves and why, and the T3.5 test asserts that list. (FR-003 AC-1, CL-11.) The real-host round trip (`sudo ginn host-setup --user <user>`, then `--undo`, with before and after snapshots) ran on 2026-10-12 at commit 4a3b508, verdict PARTIAL; see `spikes/results/G1-host-setup-roundtrip.md`. FR-003 is not fully proven until the round trip is re-run after the wording and doctor changes. The runsv integration test is `tests/integration/test_runsv_real.py` (`GINN_INTEGRATION=1`, no root). It proves the supervise links and the waiter against the real runsv.
- T3.6 `doctor/checks.py`: podman rootless, subids, runtime dir (FAIL "supervisor not running" when `/run/ginn/<uid>` is missing), user runsvdir, cgroup delegation, userns sysctl, overlay/fuse, free disk, NVIDIA-on-musl informational; every FAIL prints its fix command (FR-007, FR-069 AC-2, NFR-041). elogind `KillUserProcesses` is checked: a FAIL when the effective value is `yes`. The check reads `/etc/elogind/logind.conf` and `/etc/elogind/logind.conf.d/*.conf`, the last value wins, and an unset value counts as no. The printed fix sets it to `no` and restarts elogind (ADR-004). Add a render node mode check, which reports the mode of `/dev/dri/renderD*`. Basic GPU access works on the reference host because the render nodes are mode 0666 (S-06 section 2). A host with 0660 render nodes needs `keep-groups`, which M6 decides. Doctor reads host state only (subid lines, the sv dir, the service link, the supervise links, `/run/runit`, the cgroup tree), never the Host Setup Record (ADR-013 Decision 14). Its root controller tolerance comes from the current root `cgroup.subtree_control`, and its subid check applies the one-line rule: one line of at least 524288 IDs per file. F11 adds these checks: `/run/runit` exists; both supervise links point into it; the modes of the shared directories are checked (ADR-013 Decision 18); and a user-owned `ginn/<uid>/supervisor` leaf gets a root chown as its printed fix (ADR-003).
- T3.7 `doctor/report.py` text + `--json`.
- T3.8 `integrity/fingerprint.py`, `state.py`, `ginn integrity [--ack]`, `GINN-W-HOST-CHANGED`; 5000-file timing test (NFR-011).
- T3.9 `ops/_mutating.py` choke point; ordering test (DR-032).

### M4 Realm lifecycle for arch, debian, fedora (FR-010..013, FR-015..017, FR-019, FR-020, FR-025, FR-026, FR-048, FR-049, FR-082, NFR-005, NFR-SEC-001/004)
- T4.1 `runtime/spec.py` RealmSpec -> `podman create` argv, Basic + services off; golden argv; assert `--privileged` never present. It passes `--http-proxy=false`, so podman does not copy the host's proxy variables into the realm (design; no spike). The create argv carries only stable binds (Realm Home at the host `$HOME` path, fonts, localtime, devices, `--tmpfs /run/user/<uid>`). Variable grants are not in `CreateSpec` (ADR-008 amendment). Dated 2026-10-13 (T4.4 review): the create argv also carries `--passwd=false`, so keep-id writes no passwd entry for the realm user (ADR-006 amendment). `create_argv` sets it for every realm, and the golden argv shows it.
- T4.2 `runtime/mounts.py` create-time binds, stable sources only (ADR-008 amendment). It also builds the grant plan for T4.10, with the missing-folder skip (FR-064) and identical paths (FR-065). A fixed path that is a symlink is checked at its target, and it is bound from its literal path, because podman resolves it at each start. A mount root that is a symlink is skipped (ADR-008 review amendment).
- T4.3 `runtime/env.py` session env discovery (Wayland, X11, PipeWire/Pulse, LANG, TZ). Each source is checked for its file type: a socket for Wayland and audio, a regular file for the authority file, and a directory for `/tmp/.X11-unix`. A source of another type is skipped and reported, as a missing one is. A `WAYLAND_DISPLAY` that is or resolves to the D-Bus socket `bus` is refused (AS-012, FR-068). The proxy variables are returned as names in `SessionEnv.pass_through`, and their values are never stored. T4.6 puts the names on the argv as `--env NAME`, because T4.1 passes `--http-proxy=false` (design; no spike).
- T4.10 `native/ginn-rebind.c` (at most 200 lines), `native/Makefile` and `runtime/attach.py`: grant attachment helper and executor (ADR-008 amendment, FR-061, FR-063, FR-064, FR-065, FR-066, FR-084, IR-025). T4.4 to T4.6 depend on it, so it is built first. The helper enters the container's mount namespace through `podman unshare`. It attaches each grant with `open_tree(OPEN_TREE_CLONE | AT_RECURSIVE)` and `move_mount`, sets `mount_setattr(AT_RECURSIVE, MOUNT_ATTR_RDONLY)` on a read-only grant, and detaches a directory grant with `umount2(MNT_DETACH)` before a re-attach. Python decides from the host side which grants to re-attach, and it passes those with `--force`, so the helper skips its own compare for them (ADR-008 review amendment). `attach.py` builds the attach list from `mounts.py` and `env.py`, creates the mountpoints, records each host identity (for a mount root, the mounts below it too) and each `share_skipped` entry, and runs the lazy re-attach. Failure on a start: a failed attach fails the start and stops the realm again. Failure at enter or launch: a failed lazy re-attach fails that enter or launch and reports it, and it never stops the realm. A services-on realm is stopped only by its `sv` service (FR-081). The attach is idempotent after a crash (ADR-008 amendment, T4.10 notes). Tests: unit tests on fakes for the attach plan, the skip, the lazy compare, the forced failure between `umount2` and `move_mount`, the re-run, and the enter-or-launch failure that must leave the realm running. The host proof is T4.9.
- T4.4 `ops/create.py`: disk precheck hook, pull by digest with retry hook, create gen-1, start (the start runs attachment and records `share_skipped`, T4.10), realm user account, Integration Package Set, `--with`, record, audit; full rollback (FR-010 AC-1..3, FR-011, FR-013, FR-015).
- T4.5 `ops/lifecycle.py` start/stop/status/list `--json` with disk usage (FR-016, FR-020). Start runs attachment and records `share_skipped`, and `status` shows the skips (FR-064 AC-1, T4.10). Built 2026-10-13: start and stop run in `mutating()`, and status and list are read-only; a wrong-state verb raises GINN-E-REALM-STATE (exit 4, DR-020); services-on and non-Basic realms raise NotImplementedError until M7 and M6; the status keys are pinned in the module and the test.
- T4.6 `runtime/enter.py` enter/exec/`--root`, exit-code passthrough, `$HOME` = host home path, auto-start (FR-017, FR-042, FR-048, FR-049). Auto-start, and the enter and launch path, run attachment and record `share_skipped`, and they run the lazy re-attach before exec (ADR-008 amendment, IR-025). A failed lazy re-attach fails that enter or launch, reports it, and never stops the realm. A services-on realm is stopped only by its `sv` service (ADR-008 review amendment, T4.10 notes). The lazy re-attach uses the grant list fixed at the last start (FR-066). Proxy variables go on the argv as `--env NAME` only, never `NAME=VALUE`, so credentials do not show in `/proc/<pid>/cmdline`. Podman reads the value from its own environment, so the ADR-004 subprocess allowlist carries the six proxy names (ADR-004 amendment, 2026-10-12). T4.6 implements the pass-through, from the names in `SessionEnv.pass_through` (T4.3). Built 2026-10-13: `runtime/enter.py` with `runtime/_enter_plan.py` (FR-017, FR-042, FR-048, FR-049, FR-061, FR-064, FR-066, NFR-004, IR-011). The pre-check is `attach.needs_reattach`, which makes the decision that `reattach` makes. Only a changed grant takes the realm lock, under `mutating("reattach")`, and the record is saved only when its grant or share list changed. A check that finds no work under the lock is a `noop` step with its reason. A realm lock held by a re-attach or a start is waited for up to 2 s, then refused with GINN-E-REALM-BUSY. `--root` uses `/root` for HOME and the workdir, so a root write never reaches the Realm Home (FR-042; the SRS is silent, and this is a decision). Kali is refused with GINN-E-NOT-SUPPORTED-YET (exit 2). T4.6 added that code; `ops/lifecycle.py` raises it itself for services-on and non-Basic realms. The `PodmanCli` exec and the fake take `pass_env` (`--env NAME`), and a non-TTY exec ignores SIGINT and SIGQUIT while podman runs. A 125 from exec is checked against the listing once. A ginn-side failure of enter or exec exits 125 (IR-011), which the CLI remaps (T4.11). The listing per enter is the ADR-010 amendment of 2026-10-13, and NFR-004 is measured at T4.9.
- T4.7 `ops/delete.py` with `--purge-home` confirmation (FR-019, AS-014). It accepts a `deleting` record as a starting state, which recovery leaves in place (T2.8). Built 2026-10-13: the steps are journaled (`STEPS`), containers are found by label, the Realm Home goes only on purge plus confirmation, a link home, a mount point (found in the mount table and by mount id, since a bind mount from the same file system keeps its st_dev) or a tree too deep is refused with GINN-E-HOME-UNSAFE before the record moves (DR-021), and a failed rmi keeps the image, with a note, while the delete goes on. Services-on realms are refused until M7 (ADR-003). Exports, the Install Manifest and the supervision entry wait for M5 and M7.
- T4.8 Owned Paths 0700, `GINN-E-NO-SUCH-REALM` (FR-025). Checked 2026-10-13: both already exist (`store/paths.py` `ensure`, `store/records.py` `load`) and are tested, so T4.8 adds only the delete-side tests in `tests/unit/test_ops_delete.py`.
- T4.9 `tests/integration/conftest.py` + `test_lifecycle_real.py`: create timed (NFR-007), start p95 <= 2 s (NFR-005), exec p95 <= 300 ms over 100 launches, with the ADR-008 pre-check included (NFR-004, ADR-010), no `systemd` process (FR-082), delete. Host-only. Host gate (ADR-008 amendment): attachment of a socket, a directory and a regular file under keep-id after `podman start`, the read-only attach of a file and a directory, and the lazy re-attach after a host change, including a drive mounted below a mount root after start. The gate runs after T4.10, which it tests. It also proves a busy directory grant (an open file and a submount under it) detached with `MNT_DETACH`, and the read-only flag and the `AT_RECURSIVE` clone reaching a child mount (ADR-008 review amendment). It proves whether a socket or file grant with an open descriptor gives `EBUSY` under a plain `umount2`, as ADR-008 leaves open. If the gate fails, stop and escalate before M5, as the S-01 gate does before M7. The exec p95 is measured on crun, the runtime the base argv names. S-02 measured runc. If p95 exceeds 300 ms, `ginn-run.c` returns to `native/` (ADR-010). A `host_musl` smoke marker selects the create, start, exec and delete smoke tests of this file, and runs only when the S-03 musl host exists; elsewhere it is skipped. Amended 2026-10-12: the drive case mounts the drive after the store's pause process exists, and the gate checks whether that mount propagates into the mount namespace that `podman unshare` enters. T7.8 checks whether the runtime tmpfs is recreated at login. The helper's `attached` line carries the device and inode, which T4.10 compares with the host identity (ADR-008, T4.9 notes). Dated 2026-10-13 (T4.4 review), added to this gate: (a) the realm user step runs after start, `useradd -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0 -m -d <host home> -u <uid> <name>` as root, exits 0, and creates the user with the host uid. (b) The keep-id passwd injection check: with `--passwd=false` in the create argv, the realm has no passwd entry for the host uid before the user step. Control: the same argv without `--passwd=false` shows the entry, and the user step then fails on the uid clash, which shows that the flag is needed. Both (a) and (b) are unverified until this gate runs. Dated 2026-10-13 (T4.5 review), added to this gate: (c) stop grace does nothing for a services-off realm, whose keep-alive is `sleep infinity` under `--init` (catatonit). The FR-016 body says the stop "terminates all realm processes", and AS-023 sets its 30 s timeout. The gate measures the stop of a services-off realm against that timeout and records what its init does with SIGTERM before the kill. Unverified until this gate runs. Run 2026-10-13 on the Void host: 16 PASS, 4 FAIL escalated (see `spikes/results/T4.9-lifecycle-real.md`), gate FAIL before M5.
- T4.11 CLI wiring for create, start, stop, status, list, enter, exec, delete (IR-010..012): command modules over ops/create.py, ops/lifecycle.py, runtime/enter.py and ops/delete.py; `--json` per IR-010, exit codes per IR-011; recovery (ops/recovery.py) runs at the start of each command (FR-122), since IR-010 defines no `ginn recover`; proving tests tests/unit/test_cli_*.py; no task wired these commands before this line (added 2026-10-13). T4.11 maps the NotImplementedError that runtime/spec.py and runtime/mounts.py raise (create's refusals) to GINN-E-NOT-SUPPORTED-YET, which T4.6 added to errors.py (exit 2). ops/lifecycle.py and ops/delete.py raise that code themselves (services-on until M7, ADR-003, FR-081; non-Basic tiers until M6), so the CLI maps nothing for them (T4.7).

### M5 Packages, desktop export, bin export, AUR (FR-040..047, FR-012, NFR-009, DR-007)
- T5.1 `packages/manager.py` verbs from TOML, argv lists never through a shell, `GINN-E-PKG-SIGNATURE` mapping (FR-012, FR-040 AC-2).
- T5.2 `packages/manifest.py` Install Manifest; explicit-package diff after `exec --root`/`enter` records method `native` (FR-042 AC-1).
- T5.3 `ginn install` / `ginn remove` through `_mutating` (FR-040, FR-041).
- T5.4 `export/desktop.py`: parse per Desktop Entry spec, rewrite `Exec` keeping field codes, `Name (realm)` incl. localized names, absolute `Icon`, drop `TryExec`/`DBusActivatable` (FR-044).
- T5.5 `export/icons.py` copy into `$XDG_DATA_HOME/ginn/icons/<realm>/`.
- T5.6 `export/sync.py` `ginn _sync <realm>` diff and apply (FR-044/045); host-only 5 s timing (NFR-009).
- T5.7 `export/bins.py` `ginn export --bin` launchers in `$XDG_DATA_HOME/ginn/bin` (FR-047, AS-008).
- T5.8 `packages/aur.py` transient toolchain, build as realm user, remove build-only set, restore on failure (FR-043).
- T5.9 `native/ginn-watch.c` + Makefile; host-only test.

### M6 Tiers, permissions, Agent Proxy, D-Bus filter, NVIDIA (FR-060..069, NFR-SEC-002/006/007)
- T6.1 `security/caps.py` enumerated per-tier set from `docs/capabilities.md`; argv dump equals the document (NFR-SEC-002).
- T6.2 `ginn tier` / `ginn perms`, Custom toggles and shared paths (DR-004), restart-required flag (FR-060, FR-063, FR-066).
- T6.3 `native/ginn-agent-filter.c`; hypothesis test over framing with a fake agent (FR-067 AC-1, NFR-SEC-007).
- T6.4 `security/agent.py` filter + gpg extra socket lifecycle, Development realms only.
- T6.5 `security/dbus_filter.py` xdg-dbus-proxy argv and lifecycle (FR-068). The proxy starts lazily, not with the realm, because S-05 measured 4.7 MiB RSS for one resident proxy (NFR-002). One proxy per realm: it starts on the first ginn launch that needs it and exits with the last ginn-launched app, so an idle realm has none (ADR-007).
- T6.6 `security/nvidia.py` detection, CDI generation with `LD_LIBRARY_PATH=/usr/lib/ginn-nvidia` in the spec env (test the CDI env field first), `ldconfig` as container root as the fallback, musl informational (FR-069, ADR-012).
- T6.7 Development realm: create the realm user without subuid lines (`useradd -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0`), write the Realm Home `storage.conf` (and `containers.conf`), generate the seccomp profile (FR-062 AC-3, ADR-006).
- Host-only `test_tiers_real.py` (FR-061 AC-1/2, FR-062 AC-1..4, FR-063 AC-1).

### M7 Services mode, runit supervision, autostart (FR-080..085, NFR-003, NFR-033, AS-028)
- T7.1 services-on argv variant per ADR-003, added to the realm's create argv, with `-e SYSTEMD_LOG_LEVEL=warning` (ADR-007, mitigation d). Supervisor-launch rule (ADR-003): a services-on realm starts only through its runit entry (`sv up`, FR-081), never from the CLI process. The enter and launch path runs the lazy attachment (ADR-008 amendment) for services-on realms too.
- T7.2 `native/ginn-wait.c`.
- S-05s (spike, runs before T7.3): the create-time forms of the ADR-007 mitigations a+b+d. Pass: journald honours the mounted drop-in (`/etc/systemd/journald.conf.d/ginn.conf`, read-only, `Storage=volatile`, `RuntimeMaxUse=8M`), with journald's computed maximum recorded in the result, and the idle median is within ±1 MiB of 37.63 MiB (the S-05r a+b+d median). The masks are checked with `systemctl show -p LoadState,ActiveState <unit>`, which must print `masked` and `inactive` for each unit in `spikes/s05/masks-base.txt`. The median alone does not show that the masks took effect (ADR-007). Fallback: in-realm files under CL-10. T7.3 does not ship the mitigations until S-05s passes or CL-10 is decided.
- T7.3 `services/runit_user.py` per-realm service dir `$XDG_STATE_HOME/ginn/sv/<realm>/{run,finish,down}`; `finish` counts crashes, 5 in 60 s -> `down` + state `failed` (FR-081 AC-3). Services-on realms ship the ADR-007 mitigations a+b+d by default, as create-time mounts and environment and not as files in realm /etc: a read-only bind of the ginn-owned journald drop-in at `/etc/systemd/journald.conf.d/ginn.conf` (`Storage=volatile`, `RuntimeMaxUse=8M`), `/dev/null` bind mounts over the unit paths in `spikes/s05/masks-base.txt`, and `SYSTEMD_LOG_LEVEL=warning`. These forms ship only after S-05s. The services-on run script runs attachment after `podman start`, so grants attach on every service start (ADR-003 hook, ADR-008 amendment, T4.10).
- T7.4 `services/supervisor.py` via `sv`; `ginn services on|off` (FR-083).
- T7.5 `services/autostart.py` (`down` file semantics, DR-012).
- T7.6 `native/ginn-idle.c` wired into services-off start (FR-018).
- T7.7 Host-only `test_services_real.py`: FR-080, FR-081 AC-1..3, FR-083, FR-084, NFR-003 RSS table, on glibc and musl. FR-080 AC-1 and AC-2 run under the crun argv (ADR-003), which S-01 did not test. The logind mask (`spikes/s05/masks-logind.txt`) is tested here against the FR-080 apps first, and stays out of the default unless they pass. NFR-003 is re-measured per distro with the ADR-003 argv, because the a+b+d margin is thin (ADR-007). It also measures the helpers that NFR-002 and NFR-003 count (`ginn-watch`, `ginn-idle`, `runsv`, `ginn-wait`), which S-05r did not measure, and NFR-021 (five idle realms, Should) with the same RSS harness (ADR-007).
- T7.8 Host-only `tests/integration/test_rebind_real.py`: a stale session path is re-attached after a real logout, with no realm restart (ADR-008 and its amendment, IR-025, FR-084). The helper and its wiring moved to T4.10, and the host gate that proves attachment is in T4.9.

### M8 Rebuild, update, failure handling (FR-021..024, FR-120..123, NFR-030/031, DR-034)
- T8.1 `ops/retry.py` network classifier, 5/15/45 s, `GINN-E-NETWORK`.
- T8.2 `ops/disk.py` estimate x 1.10, `GINN-E-DISK-SPACE` (FR-121).
- T8.3 `ops/rebuild.py` new generation, manifest replay, `GINN-W-REPLAY-MISSING`, atomic swap, fault injection at 50 points (FR-021/022, NFR-031).
- T8.4 `ops/update.py` `update <realm>|--all [--check]`, `broken` only on inconsistent db (FR-023/024, FR-120 AC-2).
- T8.5 `ginn create --resume` from the journal (FR-122).
- T8.6 `test_crash_safety.py` SIGKILL simulation at every journal step (NFR-030).
- T8.7 Kali `--everything` size estimate + confirmation, `GINN-E-OPTION-INVALID` (FR-014).

### M9 Control Plane GUI (FR-100..107, NFR-010/022/050/051)
- T9.1 `gui/backend.py` state reader, `ginn --json` runner with progress lines and SIGINT cancel, `Gio.FileMonitor` refresh; pure-Python tests.
- T9.2 viewmodels dashboard, wizard, permissions, health; FR-101 AC-1/2 and FR-103 AC-1 at viewmodel level.
- T9.3..T9.7 `Adw.Application` views: dashboard, wizard, realm detail with confirmations (FR-102), permissions and health with integrity banner (FR-104), progress with cancel (FR-107); `xvfb-run` smoke tests.
- T9.8 FR-106 parity test from a mapping constant IR-001..006 -> IR-010.
- T9.9 `org.bongbetic.ginn.desktop`, icon from `art/ginnungagap-mark.svg`, `ginn-gui` subpackage.

### M10 Kali, release gates, traceability (FR-014, FR-026/027, section 9, Appendix C, NFR-062/063)
- T10.1 Kali TOML, pasta + NET_RAW argv, help text stating OOS-9 (R-002).
- T10.2 `tools/host-diff.sh` full-filesystem diff around the lifecycle suite (FR-005).
- T10.3 `tools/trace-no-xbps.sh` strace over the integration suite (FR-004).
- T10.4 `docs/verification-report.md` + `tools/measure/*.sh` for NFR-001..011.
- T10.5 Two-user FR-025 test, NFR-SEC-008 audit-log reachability, NFR-SEC-010 network capture allow-list.
- T10.6 `tools/release.sh cut`, signed repo, release notes listing deferred Should FRs (9.8). The release notes state that Intel GPU is supported but untested, and that AMD meets release gate 1(a) (SRS section 9).

Dependencies: M1 before M4 (ADR-004, ADR-006 and ADR-010 shape `runtime/`; ADR-011 shapes the distro verbs in T2.13, `packages/manager.py` in T5.1 and `ops/update.py` in T8.4), S-01 before M7, S-05 before M6/M7 acceptance, S-10 before M4 real-distro tests.

## Testing strategy

- Unit: pytest with `fake_podman.py` for ops logic and the `podman_bin` shim for `podman/cli.py`; fake `sv` shim; temp XDG dirs; `ruff`, `mypy --strict` on `src/ginn` (GUI views excluded).
- Property/fuzz (NFR-SEC-005): hypothesis on DR-002..005 and CLI parsing; 500 examples in CI, 10000 under `GINN_FUZZ=1`; fake podman log must be empty after any rejection.
- Integration (`GINN_INTEGRATION=1`): one file per milestone; markers `host_glibc`, `host_musl`, `host_nvidia` for the section 9 matrix.
- Coverage: `--cov=ginn --cov-fail-under=80`, GUI views omitted (NFR-061).
- Traceability: `@pytest.mark.fr` + `tools/traceability.py --check` (Appendix C). The CI job `traceability` (`.github/workflows/ci.yaml`, lines 76-97) runs `--check`, renders `docs/traceability.md` with `--write`, and uploads it as an artifact. The file stays gitignored as a generated artifact. This closes the fifth-review finding on the traceability document.
- Provenance (NFR-063): CI runs `copydetect` of `src/` against pinned reference sources; any match fails.
- FR-004: PATH shim in every unit run plus strace in M10.
- Crash safety: journal-step fault injection (T8.6) plus host SIGKILL runs in M10.

## Verification per milestone

```
M0  pytest -q; ruff check src tests; mypy src/ginn; tools/traceability.py --check
M1  ls spikes/results/S-*.md                           # one committed result per spike; S-03 (musl) has none yet
M2  pytest tests/unit tests/property -q --cov=ginn --cov-fail-under=80; GINN_FUZZ=1 pytest tests/property -q
M3  pytest tests/unit/test_hostsetup*.py tests/unit/test_doctor*.py tests/unit/test_integrity*.py -q
    sudo ginn host-setup --user $USER; ginn doctor; sudo ginn host-setup --user $USER | grep 'already configured'
M4  GINN_INTEGRATION=1 pytest tests/integration/test_lifecycle_real.py -q
M5  GINN_INTEGRATION=1 pytest tests/integration/test_export_real.py tests/integration/test_aur_real.py -q
M6  pytest tests/unit/test_caps.py -q; GINN_INTEGRATION=1 pytest tests/integration/test_tiers_real.py -q
M7  GINN_INTEGRATION=1 pytest tests/integration/test_services_real.py tests/integration/test_rebind_real.py -q   # glibc and musl
M8  pytest tests/unit/test_rebuild.py tests/unit/test_crash_safety.py -q; GINN_INTEGRATION=1 pytest tests/integration/test_rebuild_real.py -q
M9  xvfb-run -a pytest tests/gui -q; manual NFR-050/051 walkthrough
M10 tools/host-diff.sh -- GINN_INTEGRATION=1 pytest tests/integration -q; tools/trace-no-xbps.sh; tools/provenance-scan.sh; tools/release.sh check
```
