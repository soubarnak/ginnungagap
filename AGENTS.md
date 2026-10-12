# AGENTS.md

Instructions for coding agents working in this repository.

## What this project is

Ginnungagap (`ginn`) is a Void Linux tool. It creates per-distribution Realms on rootless Podman and exports their applications to the host desktop, while the host stays unchanged after a one-time Host Setup. Behaviour, requirement IDs and constraints are defined in `GINN-SRS-001-v1.0.md`, the single source of truth. The task order is in `docs/roadmap.md`.

## Clean-room rules

- Never open, read, or copy from the source code of Distrobox or of the Anatase Spaces project, or from this repo's pre-reset git history (any commit before the T0.1 reset). Do not run `git show` or `git log -p` on those commits.
- The Anatase Docs FAQ cited as SRS REF-1 may be read; it is documentation, not code.
- Design only from `GINN-SRS-001-v1.0.md` and public specifications (XDG Base Directory, freedesktop Desktop Entry, OCI runtime, Podman CLI).
- Write every file from scratch. Do not paste text from other sources, even short snippets.
- The provenance scan (CI, NFR-063) will fail (added in T0.4) on any match against the reference sources. The repo hygiene test fails on the forbidden identifiers.

## Spec discipline

- Every commit message cites its task ID and the FR, NFR, DR, CR, IR or AS IDs it implements.
- Every test carries `@pytest.mark.fr("<ID>")` naming the requirement it proves.
- Every ADR in `docs/adr/` cites the requirement IDs it decides for.
- Build only what the SRS asks for. Do not build the items in SRS section 8 (Out of Scope).

## Hard runtime rules

- Never invoke `xbps-*` commands (FR-004). T0.7 adds a PATH shim that fails any `xbps-*` call in the test suite.
- After Host Setup, write only to Owned Paths (DR-010, CR-006).
- Realms are always rootless. Never pass `--privileged` to Podman. Never run a Realm as host root (CR-003, OOS-3, OOS-10).
- Pass package names, paths and other input to subprocesses as argv lists. Never build a shell string or use `shell=True` (NFR-SEC-005).
- Every mutating operation goes through the single choke point `ops/_mutating.py` (DR-032, FR-123, FR-124).

## Layout

Target layout of `src/ginn/`, one line per package. Packages appear as their tasks land:

```
__main__.py  entry point; errors.py holds the GINN-E error catalogue (NFR-040)
cli/         argument parsing, output, exit codes (IR-010, IR-011)
domain/      names, packages, paths, enums, realm and state rules (DR-002..013, DR-020)
distros/     per-distribution TOML data and loader (NFR-060, FR-013)
trust/       pinned image digest manifest (DR-040)
store/       Owned Paths, atomic records, locks, journal, audit log (DR-010, DR-035, FR-122..124)
podman/      adapter Protocol and the podman CLI implementation
runtime/     podman argv for create, mounts, env, launch and enter (FR-048/049, FR-061..065)
hostsetup/   one-time Host Setup, sub-IDs, cgroup delegation, runit service, undo (FR-001..003)
doctor/      health checks and report (FR-007)
integrity/   host change fingerprint (FR-006)
packages/    package manager verbs, install manifest, AUR, Kali (FR-014, FR-040..043)
export/      desktop entries, icons, bin launchers, sync (FR-044..047)
services/    runit supervision of services-on realms, autostart (FR-080..084)
security/    capability sets, D-Bus filter, Agent Proxy, NVIDIA (NFR-SEC-002, FR-067..069)
ops/         create, rebuild, update, delete, retry, disk, recovery (FR-010/019/021/023, FR-120..122)
gui/         Control Plane, GTK4 and libadwaita (FR-100..107)
```

Also: `native/` (small C helpers, each at most 200 lines), `packaging/` (Void xbps-src template), `spikes/` (M1 spike scripts and results), `tests/` (unit, property, integration, gui, fakes, shims, fixtures), `tools/` (release, packaging and check scripts), `docs/` (roadmap, ADRs, error catalogue, traceability, capabilities, reference notes).

## How to run checks

Until M0 completes, only `pytest -q` is guaranteed to work.

```sh
pytest -q
ruff check src tests
mypy src/ginn
tools/traceability.py --check
GINN_INTEGRATION=1 pytest tests/integration -q   # Void host with rootless Podman only
```

Host-only tests run only when `GINN_INTEGRATION=1` is set, on a Void host with rootless Podman.

## Task dispatch format

Tasks are listed in `docs/roadmap.md`. Each task is dispatched with:

- `id`: milestone-numbered, for example `T2.5`
- `name`: short task title
- `files`: one to four paths
- `SRS IDs`: the requirements it implements
- `proving test`: the test file and its `fr` marker
- `host-only`: yes when the proving test needs `GINN_INTEGRATION=1` on a Void host
