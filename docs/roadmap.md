<!-- Copied from the approved plan on 2026-10-12; the plan is the roadmap of record until M0 completes. -->
# Ginnungagap (`ginn`): clean-room roadmap from the Spaces fork to GINN-SRS-001 v1.0

## Context

The repo on branch `void` is an AGPL-3.0 fork of Anatase Spaces (COPYRIGHT Antheas Kapenekakis): ~19k lines Python, ~9k lines C, LXC backend run as root through polkit (`pkexec spaces.priv`), guests boot systemd, zero podman in any runtime path. `GINN-SRS-001-v1.0.md` (approved 2026-10-12) demands something incompatible with that code: rootless Podman as the only runtime (CR-003), zero code from Anatase or Distrobox (CR-004, NFR-063), Apache-2.0 (CR-005), command `ginn` (CR-001), Void glibc and musl with runit on the host (CR-002).

Gap summary (current vs SRS):

| Area | Current fork | SRS |
|---|---|---|
| Runtime | root `lxc-start`, polkit, AppArmor | rootless podman, no root after Host Setup (FR-001, FR-105) |
| Guest init | always systemd | per-realm services mode: systemd or minimal init (FR-080..083) |
| Realm build | `pacstrap`/`debootstrap`/`dnf5` into rootfs | verified official image + Integration Package Set (FR-010..013, DR-040) |
| Host writes | `/var/lib/spaces`, `/etc/spaces`, `/usr/local/share/applications`, sudoers | Owned Paths only, integrity fingerprint (FR-005, FR-006, DR-010) |
| Permissions | presets basic/develop/custom, host config mounts | tiers with fixed grants, Agent Proxy, D-Bus filter (FR-060..068) |
| GUI | Textual TUI | GTK Control Plane with CLI parity (FR-100..107) |
| Records | `info.json` | state machine, journal, manifest, audit log (DR-020, DR-050, FR-122..125) |
| License | AGPL-3.0 | Apache-2.0 |

Decisions made by the user in this session (do not re-open):

1. Clean-room rewrite. Existing code is a behavioural reference only, never copied.
2. Replace this repo's tree in place on branch `void`. History keeps the fork.
3. Python >= 3.12, GTK4/libadwaita via PyGObject for the Control Plane, podman driven through its CLI.
4. Phased roadmap to v1; tasks sized for `haiku-coder` (1-4 files, one proving test). `opus-reviewer` reviews each task. See `CLAUDE.local.md`.

Host facts measured on this machine (Void glibc x86_64): podman 5.8.8 rootless, cgroup manager `cgroupfs`, cgroup v2 root with empty `subtree_control` (no delegation, so R-001 is real), OCI runtime runc (crun packaged), netavark + pasta present, subuid range `100000:65536` (below AS-005's 524288). Every required package exists in the Void repo: podman, crun, conmon, passt, netavark, aardvark-dns, catatonit, fuse-overlayfs, xdg-dbus-proxy, python3-gobject, gtk4, libadwaita, python3-pytest, python3-hypothesis.

SRS clarifications to raise with the product owner during M1 (the plan proceeds under the stated assumption):

- CL-1 `$XDG_RUNTIME_DIR/ginn` (DR-010) vanishes at logout under elogind; services-on realms (FR-081/084) need a session-independent runtime dir. Assume Host Setup creates `/run/ginn/<uid>`; S-04 confirms.
- CL-2 FR-085 dockerd in a realm with shared host netns (AS-006) has no NET_ADMIN; assume rootless-mode dockerd. S-07 decides.
- CL-3 AS-007 "realm root maps to the unprivileged host user": with `--userns=keep-id` realm root maps to the first subordinate UID. Still unprivileged; confirm wording.
- CL-5 Session sockets bound at start go stale after logout while a services-on realm keeps running. S-08 decides rebind vs restart-required.

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
  runtime/      spec.py mounts.py env.py launch.py enter.py                 FR-061..065, FR-048/049
  hostsetup/    setup.py subids.py cgroups.py runit_system.py record.py undo.py   FR-001..003
  doctor/       checks.py report.py                                         FR-007
  integrity/    fingerprint.py state.py                                     FR-006
  packages/     manager.py manifest.py aur.py kali.py                       FR-040..043, FR-014
  export/       desktop.py icons.py bins.py sync.py                         FR-044..047
  services/     runit_user.py supervisor.py autostart.py                    FR-080..084
  security/     caps.py dbus_filter.py agent.py nvidia.py                   NFR-SEC-002, FR-067..069
  ops/          create.py rebuild.py update.py delete.py retry.py disk.py recovery.py   FR-010/019/021/023, FR-120..122
  gui/          app.py backend.py viewmodels/*.py views/*.py                FR-100..107
native/   ginn-wait.c ginn-watch.c ginn-idle.c ginn-agent-filter.c ginn-run.c  (each <= 200 lines)
spikes/   docs/adr/  docs/errors.md  docs/traceability.md  docs/capabilities.md  docs/reference/
packaging/void/srcpkgs/ginn/template  tools/{release.sh,xbps-build.sh,provenance-scan.sh,traceability.py}
tests/{unit,property,integration,gui,fakes,shims,fixtures}
```

Key design choices, one reason each:

- Podman CLI with `--format json`, not libpod REST: REST needs a resident `podman system service`, which breaks NFR-001.
- Dedicated `--root $XDG_DATA_HOME/ginn/podman --runroot <runtime>/podman`: isolates realms from the user's own podman; is an Owned Path.
- `--userns=keep-id:uid=<uid>,gid=<gid>`: Realm Home files owned by the host uid; services-on adds `--user 0:0` so systemd is PID 1.
- Pull by digest `repo@sha256:...` from `trust.toml`: mismatch leaves no layers (FR-011 AC-1 intrinsic).
- Container name `ginn-<realm>-g<N>`, record stores the ID; rebuild = atomic record swap to the new generation (DR-034).
- Services off: `--init --init-path /usr/bin/catatonit` (FR-082). Services on: `--systemd=always --cgroupns private --cgroup-parent /ginn/<uid>/<realm> --user 0:0 --stop-signal SIGRTMIN+3 --stop-timeout 30` (ADR-003 after S-01).
- Network: non-Kali `--network host` (AS-006); Kali `--network pasta --cap-add NET_RAW` (FR-026/027).
- No resident Python (NFR-002 8 MiB). Small C helpers: `ginn-wait` (what runit supervises; pidfd on conmon, SIGTERM -> `podman stop`), `ginn-watch` (inotify on realm `/usr/share/applications`, execs `ginn _sync`), `ginn-idle` (60 s idle stop, FR-018), `ginn-agent-filter` (ssh-agent allow-list: identities, sign, extension), `ginn-run` (fast launch via setns if S-02 shows `podman exec` misses 300 ms). All policy stays in Python.
- GPG: bind host `S.gpg-agent.extra` (restricted by GnuPG design) as the realm's `S.gpg-agent` (FR-067 AC-2).
- D-Bus: `xdg-dbus-proxy` (Void package, a dependency not copied code) `--filter --talk=org.freedesktop.Notifications --talk=org.freedesktop.portal.*`; Custom tier appends names (FR-068).
- NVIDIA: ginn generates a CDI JSON under `$XDG_DATA_HOME/ginn/cdi/` from host driver files; `--cdi-spec-dir` + `--device nvidia.com/gpu=all` (FR-069, R-005).
- GUI <-> backend: GUI reads the state dir and runs `ginn --json <cmd>` as a subprocess (same uid), progress as JSON lines, SIGINT = cancel; `Gio.FileMonitor` on the realm records for NFR-010. No socket, so NFR-SEC-009 holds by 0700 Owned Paths.
- Integrity: sha256 over sorted stat tuples of AS-020 paths, stat-only so 5000 files <= 1 s (NFR-011); pending warning blocks mutating ops until `ginn integrity --ack`.
- Host Setup (root, FR-001): subids >= 524288, `/etc/sv/ginn-<user>` run script (create + chown delegated cgroup `/sys/fs/cgroup/ginn/<uid>`, create `/run/ginn/<uid>`, `setpriv` to the user, `exec runsvdir $XDG_STATE_HOME/ginn/sv`), symlink into `/var/service`, Host Setup Record `/etc/ginn/setup-<user>.json` with pre-images for undo.
- One choke point `ops/_mutating.py`: lock, integrity before/after, journal, audit entry for every mutating op (DR-032, FR-123, FR-124).

## Spikes (M1), each writes a section of `spikes/RESULTS.md` and closes an ADR

| ID | Risk | Question | Pass criterion |
|---|---|---|---|
| S-01 | R-001 | systemd PID 1 in rootless podman with cgroupfs manager under a root-created delegated cgroup; runc and crun | `systemctl is-system-running` = running/degraded, zero failed default units |
| S-02 | R-006 | `podman exec` vs C `ginn-run` setns launch latency, 100 runs | p95 table; pick path meeting 300 ms (NFR-004) |
| S-03 | NFR-062 | musl Void VM: rootless podman, pasta, S-01 repeat, PyGObject import | Must-FR smoke passes |
| S-04 | CL-1 | `/run/ginn/<uid>` from runit service vs `loginctl enable-linger` | realm survives logout, visible from session `podman ps` |
| S-05 | NFR-002/003 | idle RSS: conmon + catatonit + pause + helpers + dbus-proxy; services-on Debian | <= 8 MiB / <= 40 MiB or named mitigations |
| S-06 | FR-046/069 | GPU on Wayland and X11 rootless; hand-written CDI for NVIDIA | non-software renderer; `nvidia-smi` shows host driver |
| S-07 | FR-062 AC-3, FR-085 | nested rootless podman and dockerd with `--device /dev/fuse --security-opt unmask=/proc/*` | inner `podman run alpine true` exits 0; exact extra opts recorded in `docs/capabilities.md` |
| S-08 | CL-5 | stale session sockets after re-login | rebind via `open_tree`/`move_mount` or documented restart |
| S-09 | FR-027 | Kali `--network pasta --cap-add NET_RAW` | raw socket works; host iface change denied |
| S-10 | R-003 | image channels and digests for arch/debian/fedora/kali; which are signed | `trust.toml` v1 and refresh procedure |

Gate: if S-01 fails on both runtimes, stop and escalate before M7 (release gate 9.3).

## Milestones

Task format: id, name, files, proving test, host-only flag. Every test carries `@pytest.mark.fr("FR-xxx")`. Host-only tests run with `GINN_INTEGRATION=1` on a Void host with rootless podman.

### M0 Repo reset, skeleton, CI, packaging
- T0.1 Reset per the procedure below; new `LICENSE` (Apache-2.0), `NOTICE`, `COPYRIGHT`, `README.md`, `AGENTS.md`; `test_repo_hygiene.py` asserts no `spaces`/`distrobox` identifiers and Apache headers.
- T0.2 `pyproject.toml`, `src/ginn/__main__.py`, `cli/main.py` with `--json --verbose --version`; `ginn --version` exit 0, unknown command exit 2 `GINN-E-USAGE`.
- T0.3 `errors.py` + `tools/gen_errors_doc.py` -> `docs/errors.md`; test: every `GINN-[EW]-*` code in the SRS exists in the catalogue.
- T0.4 `.github/workflows/ci.yaml`: pytest as non-root on `void-glibc` and `void-musl` containers, ruff, mypy, coverage >= 80 % non-GUI, traceability check, provenance job.
- T0.5 `tools/traceability.py` + `conftest.py` `fr` marker; fails when a Must FR has no test.
- T0.6 `packaging/void/srcpkgs/ginn/template` (depends: podman crun conmon netavark aardvark-dns passt catatonit fuse-overlayfs shadow xdg-dbus-proxy runit util-linux; subpackage `ginn-gui`), `tools/xbps-build.sh`, `tools/release.sh` (process modelled on the old signed-repo release, rewritten).
- T0.7 `tests/shims/xbps-fail/` PATH shim; `test_fr004_no_xbps.py` asserts no `xbps-*` call during the suite (FR-004).
- T0.8 ADR-001 Python + GTK4/libadwaita, ADR-002 podman CLI over REST, ADR template.

### M1 Spikes S-01..S-10
Deliverables: `spikes/*.sh|py`, `spikes/RESULTS.md`, ADR-003..009, `docs/capabilities.md` draft (per-tier argv, device and capability set for NFR-SEC-002), `trust.toml` v1. All host-only. M2 and M3 run in parallel with M1.

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
- T3.3 `hostsetup/runit_system.py` renders `/etc/sv/ginn-<user>/{run,finish,log/run}`; golden file + shellcheck.
- T3.4 `hostsetup/record.py`, `setup.py`: root check `GINN-E-SETUP-ROOT`, idempotent "already configured", record with pre-images; tests under a `--root-dir` prefix (FR-001 AC-2, FR-002).
- T3.5 `hostsetup/undo.py` `GINN-E-UNDO-REALMS-EXIST` (FR-003).
- T3.6 `doctor/checks.py`: podman rootless, subids, runtime dir, user runsvdir, cgroup delegation, userns sysctl, overlay/fuse, free disk, NVIDIA-on-musl informational; every FAIL prints its fix command (FR-007, FR-069 AC-2, NFR-041).
- T3.7 `doctor/report.py` text + `--json`.
- T3.8 `integrity/fingerprint.py`, `state.py`, `ginn integrity [--ack]`, `GINN-W-HOST-CHANGED`; 5000-file timing test (NFR-011).
- T3.9 `ops/_mutating.py` choke point; ordering test (DR-032).

### M4 Realm lifecycle for arch, debian, fedora (FR-010..013, FR-015..017, FR-019, FR-020, FR-025, FR-026, FR-048, FR-049, FR-082, NFR-005, NFR-SEC-001/004)
- T4.1 `runtime/spec.py` RealmSpec -> `podman create` argv, Basic + services off; golden argv; assert `--privileged` never present.
- T4.2 `runtime/mounts.py` Basic binds, missing-folder skip (FR-064), identical paths (FR-065).
- T4.3 `runtime/env.py` session env discovery (Wayland, X11, PipeWire/Pulse, LANG, TZ).
- T4.4 `ops/create.py`: disk precheck hook, pull by digest with retry hook, create gen-1, start, realm user account, Integration Package Set, `--with`, record, audit; full rollback (FR-010 AC-1..3, FR-011, FR-013, FR-015).
- T4.5 `ops/lifecycle.py` start/stop/status/list `--json` with disk usage (FR-016, FR-020).
- T4.6 `runtime/enter.py` enter/exec/`--root`, exit-code passthrough, `$HOME` = host home path, auto-start (FR-017, FR-042, FR-048, FR-049).
- T4.7 `ops/delete.py` with `--purge-home` confirmation (FR-019, AS-014).
- T4.8 Owned Paths 0700, `GINN-E-NO-SUCH-REALM` (FR-025).
- T4.9 `tests/integration/conftest.py` + `test_lifecycle_real.py`: create timed (NFR-007), start p95 <= 2 s (NFR-005), exec, no `systemd` process (FR-082), delete. Host-only.

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
- T6.5 `security/dbus_filter.py` xdg-dbus-proxy argv and lifecycle (FR-068).
- T6.6 `security/nvidia.py` detection, CDI generation, `ldconfig` once per driver version, musl informational (FR-069).
- T6.7 Realm `/etc/subuid|subgid` for nested podman in Development tier (FR-062 AC-3).
- Host-only `test_tiers_real.py` (FR-061 AC-1/2, FR-062 AC-1..4, FR-063 AC-1).

### M7 Services mode, runit supervision, autostart (FR-080..085, NFR-003, NFR-033, AS-028)
- T7.1 services-on argv variant per ADR-003.
- T7.2 `native/ginn-wait.c`.
- T7.3 `services/runit_user.py` per-realm service dir `$XDG_STATE_HOME/ginn/sv/<realm>/{run,finish,down}`; `finish` counts crashes, 5 in 60 s -> `down` + state `failed` (FR-081 AC-3).
- T7.4 `services/supervisor.py` via `sv`; `ginn services on|off` (FR-083).
- T7.5 `services/autostart.py` (`down` file semantics, DR-012).
- T7.6 `native/ginn-idle.c` wired into services-off start (FR-018).
- T7.7 Host-only `test_services_real.py`: FR-080, FR-081 AC-1..3, FR-083, FR-084, NFR-003 RSS table, on glibc and musl.

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
- T10.6 `tools/release.sh cut`, signed repo, release notes listing deferred Should FRs (9.8).

Dependencies: M1 before M4 (ADR-004/006 shape `runtime/`), S-01 before M7, S-05 before M6/M7 acceptance, S-10 before M4 real-distro tests.

## Repo reset procedure (T0.1, branch `void`)

Remove with `git rm -r`: `src/ tests/ data/ native/ selinux/ pkg/ dist/ void/ art/distros/ sync.sh Containerfile.sync spaces.spec spaces-git.spec MANIFEST.in requirements.in requirements.txt conftest.py readme.md pyproject.toml AGENTS.md COPYRIGHT LICENSE .github/workflows/void.yaml`. Remove untracked `__pycache__/`.

Keep: `GINN-SRS-001-v1.0.md` (add to git), `art/ginnungagap-mark.svg`, `art/ginnungagap-wordmark-*.svg`, `art/favicon.svg`, `art/social-preview.png` (Bongbetic work, commit 733f2b8). Drop `art/distros/` (upstream distro logos generated with the Spaces mark). Untracked `.claude/`, `CLAUDE.local.md` stay.

Add: `LICENSE` Apache-2.0, `NOTICE`, `COPYRIGHT` "Copyright 2026 Bongbetic", `README.md`, `AGENTS.md` (clean-room rules: never open Distrobox or Anatase sources, cite SRS IDs in commits and tests, never call `xbps-*`, write only Owned Paths, task format above), `pyproject.toml` (name `ginn`, scripts `ginn` and `ginn-gui`, optional group `gui`), `.gitignore` rewritten, `docs/adr/ADR-001..009`, generated `docs/errors.md` and `docs/traceability.md`, `docs/capabilities.md`, `docs/reference/lxc-port-lessons.md` (one page of newly written notes: elogind v1 hierarchy, AppArmor broke nested podman, SHIFT_BASE 100000 collided with rootless subuids, musl helper constraint; no text copied from `void/`).

## Testing strategy

- Unit: pytest with `fake_podman.py` for ops logic and the `podman_bin` shim for `podman/cli.py`; fake `sv` shim; temp XDG dirs; `ruff`, `mypy --strict` on `src/ginn` (GUI views excluded).
- Property/fuzz (NFR-SEC-005): hypothesis on DR-002..005 and CLI parsing; 500 examples in CI, 10000 under `GINN_FUZZ=1`; fake podman log must be empty after any rejection.
- Integration (`GINN_INTEGRATION=1`): one file per milestone; markers `host_glibc`, `host_musl`, `host_nvidia` for the section 9 matrix.
- Coverage: `--cov=ginn --cov-fail-under=80`, GUI views omitted (NFR-061).
- Traceability: `@pytest.mark.fr` + `tools/traceability.py --check` (Appendix C).
- Provenance (NFR-063): CI clones Distrobox and anatase-org/spaces at pinned commits and runs `copydetect` against `src/`; any match fails.
- FR-004: PATH shim in every unit run plus strace in M10.
- Crash safety: journal-step fault injection (T8.6) plus host SIGKILL runs in M10.

## Verification per milestone

```
M0  pytest -q; ruff check src tests; mypy src/ginn; tools/traceability.py --check
M1  bash spikes/run-all.sh > spikes/RESULTS.md        # host; S-01 setup step needs root
M2  pytest tests/unit tests/property -q --cov=ginn --cov-fail-under=80; GINN_FUZZ=1 pytest tests/property -q
M3  pytest tests/unit/test_hostsetup*.py tests/unit/test_doctor*.py tests/unit/test_integrity*.py -q
    sudo ginn host-setup --user $USER; ginn doctor; sudo ginn host-setup --user $USER | grep 'already configured'
M4  GINN_INTEGRATION=1 pytest tests/integration/test_lifecycle_real.py -q
M5  GINN_INTEGRATION=1 pytest tests/integration/test_export_real.py tests/integration/test_aur_real.py -q
M6  pytest tests/unit/test_caps.py -q; GINN_INTEGRATION=1 pytest tests/integration/test_tiers_real.py -q
M7  GINN_INTEGRATION=1 pytest tests/integration/test_services_real.py -q   # glibc and musl
M8  pytest tests/unit/test_rebuild.py tests/unit/test_crash_safety.py -q; GINN_INTEGRATION=1 pytest tests/integration/test_rebuild_real.py -q
M9  xvfb-run -a pytest tests/gui -q; manual NFR-050/051 walkthrough
M10 tools/host-diff.sh -- GINN_INTEGRATION=1 pytest tests/integration -q; tools/trace-no-xbps.sh; tools/provenance-scan.sh; tools/release.sh check
```

## Execution notes

- Each task goes to `haiku-coder` with: task id, files, SRS IDs, proving test, and the clean-room rule. `opus-reviewer` reviews the diff before merge. The orchestrator never writes code.
- Commit per task on branch `void`; commit message cites the task id and FR IDs. Git identity for this GitHub remote: `soubarnak` / `soubarna@live.in` (repo-local config).
- First concrete step after approval: T0.1 repo reset, then M0 tasks, then M1 spikes on this host (S-01 needs one sudo step).
