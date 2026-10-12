# Ginnungagap — Software Requirements Specification

**Document ID:** GINN-SRS-001 · **Status:** APPROVED v1.0 · **Product owner:** Bongbetic · **Command name:** `ginn`

| Version | Date | Author | Status | Changes |
|---|---|---|---|---|
| 0.1 | 2026-10-12 | SRS Architect (for Soubarna Karmakar, Bongbetic) | DRAFT | Initial draft from elicitation Q-001…Q-020; contradictions C-1…C-7 resolved |
| 1.0 | 2026-10-12 | SRS Architect; approved by Soubarna Karmakar (Bongbetic) | **APPROVED** | AS-002…AS-028 signed off; no requirement text changed |

---

## 1. Introduction

### 1.1 Purpose
This SRS specifies **Ginnungagap** (`ginn`), a tool for Void Linux that lets a user create isolated, per-distribution environments (**Realms**) for Arch Linux, Debian, Fedora and Kali Linux, install individual applications from those distributions, and use them as native desktop and terminal applications — without modifying the Void host system. It is the single source of truth for downstream design (HLD, ADRs, API contracts, data schema, test plan, task contracts). Every downstream document shall cite the requirement IDs it implements.

### 1.2 Scope
In scope for v1:
- A `ginn` command-line interface for the full realm lifecycle and for application installation.
- A graphical **Control Plane** for realm creation, activation, configuration and maintenance (application installation is CLI-only).
- Realms for Arch, Debian, Fedora (Must) and Kali (Should), built on rootless Podman, using new code with no code taken from Distrobox or Anatase.
- Native runit compatibility on the host, with systemd-dependent applications supported inside realms.
- Host write confinement: after one-time Host Setup, ginn never modifies the Void system.

Out-of-scope items are listed in Section 8.

### 1.3 Definitions
All domain terms (Realm, Owned Paths, Tier, Services Mode, Install Manifest, and others) are defined in **Appendix B**. Terms in that glossary carry the defined meaning wherever they appear.

### 1.4 References
| Ref | Document |
|---|---|
| REF-1 | Anatase Docs FAQ — "What is Spaces?" (https://docs.anatase.org) — behavioural model for realms |
| REF-2 | freedesktop.org Desktop Entry Specification |
| REF-3 | XDG Base Directory Specification |
| REF-4 | Elicitation Log, Appendix A of this document |

---

## 2. Overall Description

### 2.1 Product perspective
Ginnungagap is a standalone Bongbetic product. It is functionally modelled on Anatase **Spaces** (REF-1) but targets a mutable, runit-based host (Void Linux) and uses rootless Podman as its only container runtime. Distrobox is neither a dependency nor a code source (C-1 → Q-001).

```
Void Linux host (runit, unmodified after Host Setup)
 ├── ginn CLI ─┐
 ├── ginn GUI ─┼─► rootless Podman ─► Realm "arch"    (Basic, services off)
 │             │                    ─► Realm "debian"  (Development, services on → systemd inside)
 │             │                    ─► Realm "fedora"
 │             │                    ─► Realm "kali"    (isolated network)
 └── runit user supervisor ─► supervises services-on realms
```

### 2.2 User personas & roles
| Persona | Description | Privilege |
|---|---|---|
| **Desktop user** | Void user who wants apps from other distributions (AUR apps, `.deb`/`.rpm` vendor builds) | Unprivileged; owns their realms |
| **Developer** | Uses Development-tier realms for toolchains, CUDA/ROCm, Docker, VMs | Unprivileged |
| **Security practitioner** | Uses a Kali realm with optional full toolset | Unprivileged |
| **Host administrator** | Runs one-time Host Setup with root | Root, once per user |

Roles: there is one runtime role, **Realm Owner**. Each realm belongs to exactly one host user. No shared or system-wide realms exist (Q-012).

### 2.3 Operating environment
| Item | Requirement |
|---|---|
| Host OS | Void Linux, x86_64, glibc **and** musl variants (Q-009) |
| Init | runit (host). No systemd on host. |
| Display | Wayland and X11, any desktop environment (Q-014) |
| Container runtime | Podman, rootless only (Q-001, Q-016) |
| Realm libc | glibc (all realm distributions) |
| GPU | AMD and Intel (glibc and musl hosts); NVIDIA proprietary (glibc hosts only — no musl driver exists) (Q-010) |

### 2.4 Assumptions (AS-###)
All assumptions were signed off by Soubarna Karmakar on 2026-10-12 (AS-001 during elicitation; AS-002…AS-028 at v1.0 approval).

| ID | Assumption | Status |
|---|---|---|
| AS-001 | One isolated distribution environment is called a **Realm**. | Accepted (batch 1) |
| AS-002 | No fixed delivery date; release is gated only by Section 9. | Accepted (2026-10-12) |
| AS-003 | ginn is distributed as a Void package (xbps) whose declared dependencies include Podman and its runtime helpers, so ginn itself never needs to call the host package manager. | Accepted (2026-10-12) |
| AS-004 | Host Setup can be reversed with `ginn host-setup --undo` (Should). | Accepted (2026-10-12) |
| AS-005 | Host Setup allocates a subordinate UID/GID range of at least 524,288 IDs per user, enough for nested rootless containers (Q-013). | Accepted (2026-10-12) |
| AS-006 | Non-Kali realms share the host network namespace so services on `localhost` work both ways without port mapping (performance driver #1). Kali uses an isolated network namespace (Q-016). | Accepted (2026-10-12) |
| AS-007 | No `sudo` is installed in realms (Feature 3). Root inside a realm is obtained with `ginn enter --root` / `ginn exec --root`; realm root maps to the unprivileged host user. | Accepted (2026-10-12) |
| AS-008 | Terminal (CLI) binaries are exported only on explicit request, into a ginn-owned bin directory. ginn never edits shell startup files; the user adds that directory to `PATH`. | Accepted (2026-10-12) |
| AS-009 | Host fonts and host timezone are made available read-only inside every realm instead of installing font/timezone packages (Feature 3). | Accepted (2026-10-12) |
| AS-010 | In Development tier, `~/.bashrc` and `~/.zshrc` are shared **read-only** (prevents a realm from planting commands that run on the host — STRIDE tampering). | Accepted (2026-10-12) |
| AS-011 | The never-shareable path list (Q-017) is extended to: `$HOME` itself, all Owned Paths, `~/.local/share/applications`, `~/.config/autostart`, and write access to any shell startup file. Rationale: each is a path from which a realm could get code executed on the host. | Accepted (2026-10-12) |
| AS-012 | Realm apps reach the host session D-Bus only through a filter allowing notifications and desktop portals. Custom tier may widen it. | Accepted (2026-10-12) |
| AS-013 | New realms default to Basic tier, services off, autostart off. | Accepted (2026-10-12) |
| AS-014 | Deleting a realm keeps its Realm Home unless `--purge-home` is given and confirmed. | Accepted (2026-10-12) |
| AS-015 | A services-off realm stops automatically 60 s after its last user process exits. | Accepted (2026-10-12) |
| AS-016 | Services-on realms have an autostart toggle (default off); when on, the realm starts when the user's runit supervisor starts. | Accepted (2026-10-12) |
| AS-017 | Every Control Plane lifecycle action has an equivalent CLI command (scriptable, agent-usable). | Accepted (2026-10-12) |
| AS-018 | Long operations (create, rebuild, update) show progress and can be cancelled; cancel follows the same rollback rules as failure. | Accepted (2026-10-12) |
| AS-019 | Desktop export/unexport completes within 5 s of a package transaction. | Accepted (2026-10-12) |
| AS-020 | Clarifications of Q-002: (a) Podman's per-user storage and the user runit supervision directory count as ginn-owned; (b) the host integrity check fingerprints `/etc`, `/var/db/xbps`, `/var/service`, `/usr/local` and `/opt` before and after every mutating operation. | Accepted (2026-10-12) |
| AS-021 | Audit log rotates at 10 MiB, keeping 5 rotated files. | Accepted (2026-10-12) |
| AS-022 | ginn sends no telemetry and makes no network connection except to image registries, distribution repositories and the AUR. | Accepted (2026-10-12) |
| AS-023 | Numeric values not set during elicitation are SRS Architect defaults: NFR-009, NFR-011, NFR-020…NFR-063, the 30 s stop timeout (FR-016), and the 5/15/45 s retry delays (FR-120). | Accepted (2026-10-12) |
| AS-024 | A Debian realm tracks the current Debian stable release; a Fedora realm tracks the current Fedora release at creation time. Moving to a new major release is done by rebuild. | Accepted (2026-10-12) |
| AS-025 | `ginn list`, `ginn status`, `ginn doctor` and `ginn update --check` support `--json` output. | Accepted (2026-10-12) |
| AS-026 | No migration from existing Distrobox/Toolbx containers; no per-realm CPU/memory quotas in v1. | Accepted (2026-10-12) |
| AS-027 | `ginn install` accepts plain package names only; globs, version constraints, file paths and capability "provides" are rejected (users can use the native package manager inside the realm for those). | Accepted (2026-10-12) |
| AS-028 | A crashed services-on realm is restarted by the runit supervisor after 5 s; after 5 crashes within 60 s it is left stopped and marked failed. | Accepted (2026-10-12) |

### 2.5 Dependencies
| ID | Dependency | Nature |
|---|---|---|
| DEP-1 | Podman (rootless) and its helpers (OCI runtime, conmon, rootless networking helper, `newuidmap`/`newgidmap`) | Runtime, host package deps of ginn (AS-003) |
| DEP-2 | Kernel: unprivileged user namespaces, overlay or FUSE overlay filesystem, cgroup v2 | Host kernel |
| DEP-3 | An `XDG_RUNTIME_DIR` provider (elogind, turnstile or equivalent) | Host session |
| DEP-4 | Official container image channels for Arch, Debian, Fedora, Kali | External, network |
| DEP-5 | Distribution package repositories and the AUR | External, network |
| DEP-6 | runit user-level supervision (established by Host Setup) | Host |

---

## 3. Functional Requirements

Format: **ID · Title · Priority**, description, acceptance criteria (Given/When/Then), source.

### 3.1 Epic A — Host Setup & Host Integrity

#### FR-001 · One-time Host Setup · Must
`sudo ginn host-setup --user <name>` prepares the host for that user's rootless realms: subordinate UID/GID ranges (AS-005), a user-level runit supervision directory for services-on realms, and any cgroup v2 delegation required by FR-080. It records a Host Setup Record listing every host path it wrote.
- **AC-1** Given a Void host with the ginn package installed and no prior setup for user `alice`, When root runs `ginn host-setup --user alice`, Then `/etc/subuid` and `/etc/subgid` each contain a range of ≥ 524,288 IDs for `alice`, the Host Setup Record lists every host path written, and `ginn doctor` run as `alice` reports all checks passing.
- **AC-2** Given the same host, When a non-root user runs `ginn host-setup`, Then the command exits non-zero with error `GINN-E-SETUP-ROOT` and writes nothing.
- Source: Q-002, Q-003, Q-013

#### FR-002 · Host Setup idempotent · Must
- **AC-1** Given Host Setup already completed for `alice`, When it is run again, Then no host file changes (byte-identical before/after) and the command reports "already configured".
- Source: Q-002

#### FR-003 · Host Setup undo · Should
- **AC-1** Given Host Setup completed for `alice` and `alice` has no realms, When root runs `ginn host-setup --undo --user alice`, Then every path in the Host Setup Record is restored to its pre-setup content or removed.
- **AC-2** Given `alice` still has realms, When undo is run, Then it exits non-zero with `GINN-E-UNDO-REALMS-EXIST` and changes nothing.
- Source: AS-004

#### FR-004 · No host package management · Must
ginn never invokes any host package-manager command (`xbps-*`).
- **AC-1** Given the full automated FR test suite, When every ginn command is executed under process tracing, Then no `xbps-*` executable is ever launched by ginn.
- Source: Q-002

#### FR-005 · Host write confinement · Must
After Host Setup, every file ginn creates, modifies or deletes on the host is inside the Owned Paths (DR-010).
- **AC-1** Given a host snapshot taken after Host Setup, When the full lifecycle test suite runs (create, install, export, rebuild, update, delete for every distribution) and a second snapshot is taken, Then a full-filesystem diff shows changes only inside Owned Paths and inside tier-shared paths written by realm applications.
- Source: Q-002, AS-020

#### FR-006 · Host integrity check · Must
Before and after every mutating ginn operation, ginn computes the Host Integrity Fingerprint (AS-020). Any difference is shown as a blocking warning listing each changed path; the user must acknowledge it before ginn accepts another mutating operation. The event is written to the Audit Log. `ginn integrity` runs the check on demand.
- **AC-1** Given a realm `arch`, When a file under `/etc` is altered by a test harness during `ginn install arch htop`, Then ginn shows `GINN-W-HOST-CHANGED` listing that path, writes an audit entry, and refuses the next mutating command until `ginn integrity --ack` is run.
- **AC-2** Given an unchanged host, When `ginn integrity` runs, Then it exits 0 and reports "host unchanged since baseline".
- Source: Q-002, Q-019

#### FR-007 · Doctor · Must
`ginn doctor` checks: Podman present and rootless-functional; subordinate ID mapping; `XDG_RUNTIME_DIR`; user runit supervisor running; cgroup v2 delegation (for services mode); user namespaces enabled; overlay/FUSE availability; free disk in Owned Paths. Each failing check prints the exact command(s) that fix it.
- **AC-1** Given a host where `/etc/subuid` lacks an entry for the user, When `ginn doctor` runs, Then the subordinate-ID check fails and the output contains the exact `ginn host-setup` command to fix it, and exit code is non-zero.
- **AC-2** Given a fully configured host, When `ginn doctor` runs, Then every check reports PASS and exit code is 0.
- Source: Q-019

### 3.2 Epic B — Realm Lifecycle

#### FR-010 · Create realm · Must (arch, debian, fedora) / Should (kali)
`ginn create <distro> [--name <realm>] [--tier basic|development|custom] [--services on|off] [--with <pkg>…]`. Defaults: name = distro ID, tier = Basic, services = off (AS-013). The realm is built from the verified official image (FR-011) plus the Integration Package Set (FR-013) and nothing else, plus any `--with` packages and their declared dependencies.
- **AC-1** Given a configured host and no realm named `arch`, When `ginn create arch` completes, Then realm `arch` exists in state `stopped`, tier Basic, services off, and its installed-package list equals (image package list ∪ Integration Package Set).
- **AC-2** Given the same, When `ginn create debian --with htop` completes, Then the installed-package list equals (image ∪ Integration Package Set ∪ htop ∪ htop's dependency closure as resolved by apt).
- **AC-3** Given a realm named `arch` already exists, When `ginn create arch` runs, Then it fails with `GINN-E-NAME-TAKEN` and the existing realm is unchanged.
- Source: Q-004, Feature 3, Q-017

#### FR-011 · Image verification · Must
Before use, each image is verified against the Trust Manifest (DR-040). On failure, creation aborts and nothing is installed.
- **AC-1** Given a Trust Manifest entry for `fedora` and a tampered image whose digest differs, When `ginn create fedora` runs, Then it fails with `GINN-E-IMAGE-UNVERIFIED`, no realm entry exists afterwards, and no image layers remain in storage.
- Source: Q-016, Q-019

#### FR-012 · Native package signatures always enforced · Must
ginn never disables or bypasses the realm package manager's signature verification (pacman keyring, apt signed repositories, dnf gpgcheck).
- **AC-1** Given a realm whose repository serves a package with an invalid signature, When `ginn install` targets that package, Then installation fails with the package manager's signature error surfaced as `GINN-E-PKG-SIGNATURE`.
- **AC-2** Given any realm, When its package-manager configuration is inspected after create, rebuild and update, Then signature verification is enabled.
- Source: Q-016

#### FR-013 · Integration Package Set · Must
The Integration Package Set per distribution is fixed, documented, and limited to: CA certificates, and locale data for the host's `LANG`. GPU userspace libraries for AMD/Intel enter a realm only as declared dependencies of installed packages; NVIDIA libraries are provided per FR-069. Fonts and timezone come from the host read-only (AS-009).
- **AC-1** Given a newly created realm of each distribution, When its package list is diffed against its image, Then the only additions are the documented Integration Package Set for that distribution.
- Source: Q-004, Q-010, AS-009

#### FR-014 · Kali "everything" option · Should
`ginn create kali --everything`, or the equivalent Control Plane option, installs the `kali-linux-everything` metapackage. Before any download, ginn shows the estimated download and installed size and requires explicit confirmation. The option is valid only for distribution `kali`.
- **AC-1** Given no Kali realm, When `ginn create kali --everything` runs, Then an estimated size is shown and nothing is downloaded until the user confirms.
- **AC-2** Given confirmation, When creation completes, Then `kali-linux-everything` is installed and recorded in the Install Manifest.
- **AC-3** Given distro `debian`, When `--everything` is passed, Then it fails with `GINN-E-OPTION-INVALID`.
- Source: Feature (Kali), Q-005

#### FR-015 · Multiple realms per distribution · Must
- **AC-1** Given realm `debian` exists, When `ginn create debian --name debian-dev` runs, Then both realms exist with independent roots and homes.
- Source: Q-017

#### FR-016 · Start and stop · Must
`ginn start <realm>` / `ginn stop <realm>`. Stop terminates all realm processes; with services on, systemd inside the realm is shut down cleanly first.
- **AC-1** Given a stopped realm, When `ginn start` runs, Then the realm reaches `running` within NFR-005.
- **AC-2** Given a running services-on realm, When `ginn stop` runs, Then systemd inside performs an orderly shutdown and the realm reaches `stopped` within 30 s, or it is force-stopped and the forced stop is logged.
- Source: Q-014

#### FR-017 · Auto-start on use · Must
Launching an exported application, or running `ginn enter`/`ginn exec`, starts a stopped realm automatically.
- **AC-1** Given realm `arch` is stopped and has exported app "Firefox (arch)", When the user launches it from the desktop menu, Then the realm starts and Firefox opens without further user action.
- Source: Q-006

#### FR-018 · Auto-stop idle services-off realm · Should
- **AC-1** Given a running services-off realm, When its last user process exits, Then the realm stops 60 s later (±5 s) unless a new process starts in that window.
- Source: AS-015

#### FR-019 · Delete realm · Must
`ginn delete <realm>` stops the realm if running, then removes the realm root, its exported entries, its supervision entry and its Install Manifest. The Realm Home is kept unless `--purge-home` is given and confirmed (AS-014).
- **AC-1** Given realm `fedora` with 3 exported apps, When `ginn delete fedora` completes, Then no `ginn-fedora-*` desktop entry remains and the realm no longer appears in `ginn list`, and its Realm Home still exists.
- **AC-2** Given the same, When `ginn delete fedora --purge-home` is confirmed, Then the Realm Home is also removed.
- Source: Q-019, AS-014

#### FR-020 · List and status · Must
`ginn list` and `ginn status <realm>` show name, distribution, state (DR-020), tier, services mode, disk usage, number of exported apps.
- **AC-1** Given two realms, When `ginn list --json` runs, Then the output is valid JSON containing every listed field for both realms.
- Source: Q-014, AS-025

#### FR-021 · Rebuild preserving home · Must
`ginn rebuild <realm>` builds a new realm root from a freshly verified image beside the old one, reinstalls every package in the Install Manifest (including AUR packages per FR-043 and the Kali everything metapackage), keeps the Realm Home, keeps tier and services settings, and swaps in the new root in one atomic step (DR-034).
- **AC-1** Given realm `arch` whose Install Manifest contains `firefox` (repo) and one AUR test package, and a file `~/notes.txt` in its Realm Home, When `ginn rebuild arch` completes, Then the new root contains both packages, `~/notes.txt` is unchanged, and exported entries are regenerated.
- **AC-2** Given a rebuild that fails midway, When the failure occurs, Then the old realm remains usable and unchanged.
- Source: Q-011, Q-018

#### FR-022 · Rebuild replay failures · Must
- **AC-1** Given a manifest package no longer available upstream, When rebuild runs, Then all other packages are installed, the swap completes, and the report lists the missing package with `GINN-W-REPLAY-MISSING`.
- Source: Q-011

#### FR-023 · Manual update · Must
`ginn update <realm>` or `ginn update --all` runs the realm's native full upgrade. Realms are never updated automatically.
- **AC-1** Given a realm with outdated packages, When no update command is issued for 30 days, Then no package in the realm changes.
- **AC-2** Given the same, When `ginn update arch` runs, Then the native full upgrade is performed and logged.
- Source: Q-011

#### FR-024 · Update availability check · Should
`ginn update --check [<realm>]` reports, per realm, whether updates exist. The Control Plane runs this check when it opens and when the user requests a refresh — never on a background timer.
- **AC-1** Given a realm with available updates, When the Control Plane opens, Then that realm shows an "updates available" indicator.
- Source: Q-011, NFR-001

#### FR-025 · Per-user realm isolation · Must
- **AC-1** Given users `alice` and `bob` each with realms, When `bob` runs `ginn list` or tries `ginn enter alice-realm`, Then he sees only his own realms and the enter command fails with `GINN-E-NO-SUCH-REALM`.
- Source: Q-012

#### FR-026 · Realm networking · Must
Non-Kali realms share the host network namespace (AS-006). Kali realms use an isolated network namespace with outbound access, and receive raw-socket capability inside that namespace only (FR-027).
- **AC-1** Given a running Development-tier Debian realm, When a server inside it listens on `127.0.0.1:8080`, Then a host browser reaches it at `http://127.0.0.1:8080`.
- Source: AS-006, Q-016

#### FR-027 · Kali network isolation · Should
- **AC-1** Given a Kali realm, When a raw-socket tool runs inside it as realm root, Then it can open raw sockets within the realm's own network namespace.
- **AC-2** Given the same, When it tries to change host network interfaces (for example, enabling monitor mode), Then the operation is denied.
- Source: Q-016 (C-7)

### 3.3 Epic C — Applications & Desktop Integration

#### FR-040 · Install packages · Must
`ginn install <realm> <pkg>…` installs packages with the realm's native package manager, resolving only their declared dependencies. Package names must satisfy DR-003.
- **AC-1** Given realm `fedora`, When `ginn install fedora gimp` completes, Then the package set grows by exactly gimp and its dnf-resolved dependency closure.
- **AC-2** Given `ginn install fedora 'gimp*'`, When validated, Then it fails with `GINN-E-PKG-NAME-INVALID` before any package manager runs (AS-027).
- Source: Feature 1, Feature 3, Q-006

#### FR-041 · Remove packages · Must
- **AC-1** Given realm `fedora` with gimp installed and exported, When `ginn remove fedora gimp` completes, Then gimp is uninstalled and its exported entry is removed per FR-045.
- Source: Q-006

#### FR-042 · Native package manager access · Must
The user can run the realm's own package manager directly. `ginn enter --root <realm>` and `ginn exec --root <realm> -- <cmd>` give a root shell or command inside the realm. No `sudo` package is installed (AS-007).
- **AC-1** Given realm `arch`, When the user runs `ginn exec --root arch -- pacman -S --noconfirm htop`, Then htop is installed and recorded in the Install Manifest.
- Source: Q-006, AS-007

#### FR-043 · AUR transient build · Must
`ginn install arch --aur <pkg>…` installs the build toolchain (`base-devel` and the package's make-dependencies), builds the package as an unprivileged realm user, installs it, then removes everything installed only for the build. No AUR helper application is installed.
- **AC-1** Given realm `arch` with package set S, When `ginn install arch --aur <pkg>` completes, Then the package set equals S ∪ {pkg} ∪ (pkg's runtime dependency closure), and no `base-devel` member absent from S remains.
- **AC-2** Given the build fails, When the failure occurs, Then the realm package set is restored to S.
- Source: Q-007 (C-6)

#### FR-044 · Automatic desktop export · Must
When any package transaction — via `ginn install` or the native package manager — adds a desktop entry inside a realm, ginn exports it to the host menu within NFR-009. Exported names carry the realm name, for example "Firefox (arch)". Icons are copied into Owned Paths and referenced from there.
- **AC-1** Given realm `arch`, When `pacman -S firefox` completes inside it, Then within 5 s a host menu entry "Firefox (arch)" exists as `~/.local/share/applications/ginn-arch-firefox.desktop`.
- **AC-2** Given Firefox is exported from both `arch` and `fedora`, When the menu is viewed, Then two distinct entries "Firefox (arch)" and "Firefox (fedora)" exist.
- Source: Q-006

#### FR-045 · Automatic unexport · Must
- **AC-1** Given exported "Firefox (arch)", When firefox is removed inside the realm by any method, Then within 5 s the entry and its copied icon are removed.
- Source: Q-006

#### FR-046 · Exported app launch · Must
An exported entry runs the application inside its realm as the user, with the realm's tier permissions, display (Wayland or X11), audio and GPU access per FR-061.
- **AC-1** Given a Basic-tier realm with an exported GUI app, When it is launched from the host menu on a Wayland session, Then it renders, plays audio, and uses the GPU (verified by a GL renderer string that is not a software renderer).
- **AC-2** Given the same on an X11 session, Then the same holds.
- Source: Q-006, Q-008, Q-010

#### FR-047 · CLI binary export · Should
`ginn export --bin <realm> <binary>` creates a launcher in the ginn bin directory inside Owned Paths; `ginn unexport --bin` removes it. Nothing is exported automatically.
- **AC-1** Given realm `debian` with `rg` installed, When `ginn export --bin debian rg` runs and the ginn bin directory is on `PATH`, Then running `rg --version` on the host executes rg inside the realm.
- Source: AS-008

#### FR-048 · Enter realm · Must
`ginn enter <realm>` opens an interactive shell inside the realm as the user, with `$HOME` set to the Realm Home mounted at the same absolute path as the host home.
- **AC-1** Given host user `alice` with home `/home/alice`, When `ginn enter arch` runs, Then `echo $HOME` inside prints `/home/alice` and `ls ~` shows the Realm Home contents, not the host home.
- Source: Q-008

#### FR-049 · Execute command · Must
`ginn exec <realm> -- <cmd> [args…]` runs a single command inside the realm and returns its exit code.
- **AC-1** Given realm `debian`, When `ginn exec debian -- sh -c 'exit 7'` runs, Then ginn exits with code 7.
- Source: Q-006

### 3.4 Epic D — Permissions

#### FR-060 · Permission tiers · Must
Each realm has exactly one tier: Basic, Development or Custom. Default is Basic (AS-013).
- **AC-1** Given `ginn create arch --tier development`, When status is queried, Then tier = Development.
- Source: Q-008

#### FR-061 · Basic tier grants · Must
Basic grants exactly: `~/Downloads` (read-write); external drive mount roots `/run/media/$USER`, `/media`, `/mnt` (read-write); display socket(s); audio socket(s); GPU device nodes; host fonts and timezone (read-only).
- **AC-1** Given a Basic realm, When the user writes a file to `~/Downloads` inside it, Then the file appears in the host `~/Downloads`.
- **AC-2** Given the same, When the user lists `~/Documents` inside the realm, Then it is not the host's `~/Documents` (absent or realm-local).
- Source: Q-008

#### FR-062 · Development tier grants · Must
Development grants Basic plus: `~/Projects` (read-write); SSH agent and GPG agent access through the Agent Proxy (FR-067); `~/.bashrc` and `~/.zshrc` read-only (AS-010); nested rootless containers; `/dev/kvm`.
- **AC-1** Given a Development realm, When `ssh -T git@<test-server>` runs inside it with a key loaded in the host agent, Then authentication succeeds and no private key file exists anywhere in the realm.
- **AC-2** Given the same, When the realm tries to write `~/.bashrc`, Then the write fails (read-only).
- **AC-3** Given the same with services off, When `podman run --rm <image> true` runs inside, Then it exits 0.
- **AC-4** Given the same on a KVM-capable host, When a VM is started with KVM acceleration inside the realm, Then it starts with acceleration enabled.
- Source: Q-008, Q-013

#### FR-063 · Custom tier · Must
Custom lets the user toggle each individual permission from FR-061/FR-062 and add extra shared paths, each validated against DR-004.
- **AC-1** Given a Custom realm, When the user adds `~/Music` read-only, Then it appears read-only inside the realm at `~/Music` after the next start.
- **AC-2** Given the same, When the user adds `~/.ssh`, Then it is rejected with `GINN-E-PATH-FORBIDDEN`.
- Source: Q-008, Q-017, AS-011

#### FR-064 · Missing tier folders · Must
If a tier folder (`~/Downloads`, `~/Projects`) does not exist on the host, ginn does not create it; it skips that share and reports it.
- **AC-1** Given no host `~/Projects`, When a Development realm starts, Then it starts, `~/Projects` is not created on the host, and `ginn status` shows "share skipped: ~/Projects (missing)".
- Source: Q-002

#### FR-065 · Identical paths · Must
Every shared path appears inside the realm at the same absolute path as on the host.
- **AC-1** Given shared `/run/media/alice/USB`, When listed inside the realm, Then the same path shows the same files.
- Source: Q-008

#### FR-066 · Permission changes · Must
Changes to tier or permissions apply at the next realm start. If the realm is running, ginn reports that a restart is required and offers it.
- **AC-1** Given a running Basic realm, When the tier changes to Development, Then `ginn status` shows "restart required", and after restart `~/Projects` is present.
- Source: Q-008

#### FR-067 · Agent Proxy · Must
The Agent Proxy lets a Development realm list keys and request signatures from the host SSH agent, and use the host GPG agent in restricted mode (sign/decrypt; no key export or import). Requests to add, remove, export or lock keys are refused. The proxy runs only while at least one Development realm is running.
- **AC-1** Given a Development realm, When `ssh-add -D` runs inside it, Then the request is refused and host agent keys are unchanged.
- **AC-2** Given the same, When `gpg --export-secret-keys` runs inside it, Then no secret key material is returned.
- Source: Q-008, Q-016

#### FR-068 · Filtered session D-Bus · Should
Realm applications reach the host session bus only through a filter allowing desktop notifications and desktop portals (AS-012).
- **AC-1** Given a Basic realm, When an app inside calls the Secret Service API on the session bus, Then the call is denied.
- **AC-2** Given the same, When it sends a desktop notification, Then it is shown on the host.
- Source: AS-012

#### FR-069 · NVIDIA host driver injection · Must
On glibc hosts with the NVIDIA proprietary driver, ginn makes the host's matching NVIDIA userspace libraries and device nodes available read-only inside realms with GPU permission, without installing any additional host package.
- **AC-1** Given a glibc host with an NVIDIA GPU and driver, When `nvidia-smi` runs in a Basic realm, Then it reports the GPU and the host driver version.
- **AC-2** Given a musl host, When `ginn doctor` runs, Then it reports "NVIDIA unsupported on musl" as informational, not as a failure.
- Source: Q-010

### 3.5 Epic E — Services (systemd inside, runit outside)

#### FR-080 · Services mode on · Must
With services on, systemd runs as the realm's init and unmodified systemd unit files work (`systemctl enable --now`).
- **AC-1** Given a services-on Debian realm, When `ginn exec --root debian -- systemctl enable --now <unit>` runs for an installed service, Then `systemctl is-active <unit>` inside returns `active`.
- **AC-2** Given the same, When the realm restarts, Then the enabled unit is active again.
- Source: Q-003 (Feature 2)

#### FR-081 · runit supervision of services-on realms · Must
Each running services-on realm is supervised by the user's runit supervisor, visible with runit's own status tools, and restarted per AS-028.
- **AC-1** Given a running services-on realm, When the user queries runit status for its supervision entry, Then it shows the realm as up.
- **AC-2** Given the same, When the realm's container process is killed, Then runit restarts it within 10 s.
- **AC-3** Given 5 crashes within 60 s, Then the realm is left stopped and `ginn status` shows state `failed`.
- Source: Q-003, AS-028

#### FR-082 · Services mode off · Must
With services off, the realm runs a minimal init process and no systemd process exists inside it.
- **AC-1** Given a services-off realm running an app, When processes inside are listed, Then no process named `systemd` exists.
- Source: Q-003

#### FR-083 · Toggle services mode · Must
`ginn services <realm> on|off` changes the mode. It takes effect at the next start; installed packages and Realm Home are unchanged.
- **AC-1** Given a services-off realm with htop installed, When services are turned on and the realm restarts, Then htop is still installed and systemd is PID 1 inside.
- Source: Q-003

#### FR-084 · Autostart · Should
- **AC-1** Given a services-on realm with autostart on, When the user's runit supervisor starts (at login or at boot), Then the realm starts without user action.
- Source: AS-016

#### FR-085 · Docker daemon in realm · Should
- **AC-1** Given a services-on Development realm with Docker installed and enabled, When `docker run --rm hello-world` runs inside, Then it exits 0.
- Source: Q-013

### 3.6 Epic F — Control Plane (GUI)

#### FR-100 · Realm dashboard · Must
Shows every realm of the current user with the fields of FR-020 and an "updates available" indicator (FR-024).
- **AC-1** Given 3 realms, When the Control Plane opens, Then all 3 are shown with name, distribution, state, tier, services mode, disk usage and exported-app count.
- Source: Q-014

#### FR-101 · Create realm wizard · Must
Steps: distribution → name (validated live against DR-002) → tier → services mode → (Kali only) "everything" option with size estimate. Produces the same result as FR-010/FR-014.
- **AC-1** Given the wizard, When the user types `Arch!`, Then the name field shows the DR-002 error and the Create button is disabled.
- **AC-2** Given the wizard with distribution Debian, Then the "everything" option is not shown.
- Source: Q-014, Q-017

#### FR-102 · Realm actions · Must
Start, stop, rebuild, update, delete, services on/off, autostart toggle. Rebuild and delete require confirmation; delete offers a separate, unchecked-by-default "also delete home data" choice.
- **AC-1** Given a realm, When the user clicks Delete and confirms without ticking "also delete home data", Then the outcome equals FR-019 AC-1.
- Source: Q-014, AS-014

#### FR-103 · Permissions editor · Must
Shows the tier and, for Custom, every permission toggle and shared path list, validating paths against DR-004.
- **AC-1** Given a Custom realm, When the user adds `/etc` as a shared path, Then the editor shows `GINN-E-PATH-FORBIDDEN` and does not save it.
- Source: Q-008, Q-017

#### FR-104 · Host health view · Should
Shows `ginn doctor` results and any unacknowledged host-integrity warning, with an acknowledge action.
- **AC-1** Given an unacknowledged `GINN-W-HOST-CHANGED`, When the Control Plane opens, Then a blocking banner lists the changed paths.
- Source: Q-019, FR-006

#### FR-105 · Unprivileged GUI · Must
The Control Plane runs as the user, never requests root after Host Setup, and only talks to ginn running as the same user (NFR-SEC-009).
- **AC-1** Given a configured host, When every Control Plane action is exercised, Then no privilege-escalation prompt appears and no process runs as root.
- Source: Q-014, Q-016

#### FR-106 · CLI parity · Must
Every Control Plane action has an equivalent CLI command producing the same result.
- **AC-1** Given the Control Plane action inventory (IR-001…IR-006), When each is mapped to the CLI inventory (IR-010), Then every action has a CLI equivalent.
- Source: AS-017

#### FR-107 · Progress and cancel · Should
Create, rebuild and update show progress and a Cancel action; cancel rolls back as in FR-120.
- **AC-1** Given a create in progress, When the user cancels, Then the partial realm is rolled back and no realm entry remains.
- Source: AS-018

### 3.7 Epic G — Failure Handling & Records

#### FR-120 · Network failure · Must
On network failure during create, rebuild, update or install, ginn retries 3 times with delays of 5 s, 15 s and 45 s. If all retries fail: create and rebuild are rolled back completely (rebuild keeps the old root per DR-034); install and update stop and report, leaving the realm as the native package manager left it, and the realm is marked `broken` only if the package manager reports an inconsistent package database.
- **AC-1** Given network loss during `ginn create fedora`, When 3 retries fail, Then the partial realm and downloaded layers are removed, exit code is non-zero with `GINN-E-NETWORK`, and an audit entry is written.
- **AC-2** Given network loss during `ginn update debian`, When 3 retries fail, Then ginn exits with `GINN-E-NETWORK`, the realm remains usable in its prior state or a consistent partially-upgraded state, and `ginn status` reports which happened.
- Source: Q-019

#### FR-121 · Disk space precheck · Must
Before create, rebuild, update or install, ginn estimates the space needed; if free space in Owned Paths is below estimate × 1.10, it refuses before starting.
- **AC-1** Given 1 GiB free and an estimated need of 1 GiB, When `ginn create arch` runs, Then it fails with `GINN-E-DISK-SPACE` showing needed and available space, and nothing is downloaded.
- Source: Q-019

#### FR-122 · Interrupted operations · Must
If create is interrupted (crash, power loss, kill), the realm is in state `broken` at next invocation, and ginn offers `ginn create --resume <realm>` or `ginn delete <realm>`.
- **AC-1** Given `ginn create debian` killed with SIGKILL mid-download, When `ginn list` next runs, Then `debian` shows state `broken` with both recovery commands.
- Source: Q-018

#### FR-123 · One change at a time per realm · Must
Only one mutating operation may run on a realm at a time. A second is rejected immediately (not queued), naming the running operation.
- **AC-1** Given `ginn rebuild arch` running, When `ginn install arch htop` is issued from another terminal or the Control Plane, Then it fails with `GINN-E-REALM-BUSY: rebuild in progress`.
- Source: Q-018

#### FR-124 · Audit Log · Must
Every realm lifecycle operation, permission change, services change, export/unexport and host-integrity event is appended to the Audit Log (DR-050).
- **AC-1** Given a tier change on realm `arch`, When the Audit Log is read, Then an entry exists with timestamp, user, realm, operation, before/after permissions and outcome.
- Source: Q-016

#### FR-125 · Log viewer · Should
`ginn log [<realm>] [--since <time>]` shows Audit Log entries.
- **AC-1** Given entries for 2 realms, When `ginn log arch` runs, Then only `arch` entries are shown in chronological order.
- Source: Q-016

---

## 4. Non-Functional Requirements

### Driving architectural characteristics (ranked)
Downstream ADRs use this order to settle trade-offs (Q-015).

| Rank | Characteristic | Why it drives |
|---|---|---|
| 1 | **Performance efficiency** | "No additional system overhead" — idle cost, launch latency, native-speed execution |
| 2 | **Host integrity & isolation** | Void core must stay unchanged; realms must not reach host secrets |
| 3 | **Recoverability** | A broken realm is rebuilt in minutes with home data kept |
| 4 | **Interoperability** | runit host, systemd apps, Wayland/X11, desktop menus, GPU stacks |
| 5 | **Usability** | Spaces-like simplicity; GUI for lifecycle |

### 4.1 Performance
| ID | Requirement | Target | Verification | Priority |
|---|---|---|---|---|
| NFR-001 | ginn processes when no realm is running | 0 processes from ginn executables (the runit user supervisor from Host Setup is excluded and must use ≤ 1 MiB RSS) | Process listing on an idle configured host after all realms stop | Must |
| NFR-002 | Host memory for an idle running realm, services off | ≤ 8 MiB RSS total (all host-side and in-realm processes attributable to ginn and the runtime, excluding user apps) | RSS sum after 60 s idle, median of 10 runs | Must |
| NFR-003 | Host memory for an idle running realm, services on | ≤ 40 MiB RSS total (as above, including systemd and its default units) | As NFR-002 | Must |
| NFR-004 | Added launch latency, app in already-running realm | p95 ≤ 300 ms from launcher invocation to app process start inside realm | 100 launches of a trivial binary, timed | Must |
| NFR-005 | Start time of stopped realm | p95 ≤ 2 s to state `running` | 50 start cycles per distribution | Must |
| NFR-006 | Compute and GPU throughput vs native | Within 3% | 7-Zip built-in benchmark (CPU) and glmark2 (GPU), median of 5 runs, realm vs same build on host | Must |
| NFR-007 | Realm creation time (no `--with`, no Kali everything) | ≤ 5 min on a 100 Mbps link | Timed create per distribution, 3 runs | Must |
| NFR-008 | Disk use of a new realm (no `--with`) | ≤ official image size + 200 MiB | Storage accounting after create | Must |
| NFR-009 | Export/unexport latency after a package transaction | ≤ 5 s | FR-044/FR-045 tests, 20 runs | Must |
| NFR-010 | Control Plane staleness after a CLI operation completes | ≤ 1 s (see DR-030) | Scripted CLI op with GUI state probe, 50 runs | Must |
| NFR-011 | Host integrity check overhead | ≤ 1 s per mutating operation on a host with ≤ 5,000 files in fingerprint scope | Timed `ginn integrity`, 20 runs | Must |

### 4.2 Security
Threat analysis (STRIDE) — assets, actors and trust boundaries:

| Asset | Actor | Trust boundary |
|---|---|---|
| Void host system files | Malicious or compromised package inside a realm (primary) | Realm ↔ host (user namespace, mounts) |
| Host user's private files, SSH/GPG keys | Another local user | User ↔ user |
| Realm Homes (may hold credentials) | Tampered image or mirror | Registry/repository ↔ ginn |
| Audit Log | Local process impersonating the ginn backend | Control Plane ↔ ginn backend |

| STRIDE | Threat | Requirements |
|---|---|---|
| Spoofing | Forged image or package | FR-011, FR-012, NFR-SEC-004 |
| Spoofing | Other process drives ginn backend | NFR-SEC-009 |
| Tampering | Realm modifies host system or host-executed files | FR-005, FR-006, NFR-SEC-001, NFR-SEC-002, NFR-SEC-006 |
| Repudiation | Permission change without trace | FR-124, NFR-SEC-008 |
| Information disclosure | Realm reads host home, keys, keyring, other users' realms | FR-061, FR-067, FR-068, FR-025, NFR-SEC-006 |
| Denial of service | Realm fills disk | FR-121; CPU/RAM quotas out of scope (AS-026) |
| Elevation of privilege | Realm gains host root or host-user code execution | NFR-SEC-001, NFR-SEC-002, NFR-SEC-003, AS-011 |

| ID | Requirement | Verification | Priority |
|---|---|---|---|
| NFR-SEC-001 | Realms always run rootless. No realm is ever started in privileged mode, in any tier. | Inspect runtime configuration of every realm in the test suite; zero privileged flags | Must |
| NFR-SEC-002 | Capabilities added beyond the runtime's rootless defaults are limited to: raw-socket capability for Kali (own network namespace only) and the enumerated set required by Development-tier nested containers and `/dev/kvm`. The enumerated set is fixed in the HLD and tested. | Per-tier capability dump compared to the HLD list | Must |
| NFR-SEC-003 | ginn ships no setuid or setgid executables of its own. | File-mode scan of the installed ginn package | Must |
| NFR-SEC-004 | Images are used only if they match the Trust Manifest; package-manager signature checks are never disabled. | FR-011, FR-012 tests | Must |
| NFR-SEC-005 | All user input is validated at entry against DR-001…DR-005; invalid values are rejected before any side effect. Package names and paths are passed as single arguments, never through a shell. | Fuzz test of every CLI argument and GUI field with 10,000 generated inputs including shell metacharacters; zero side effects on rejection | Must |
| NFR-SEC-006 | Never-shareable paths (DR-004) cannot be shared in any tier, including Custom. | Attempt to share each forbidden path via CLI and GUI | Must |
| NFR-SEC-007 | Agent Proxy never transfers private key material into a realm. | FR-067 tests plus a search of realm filesystem for key files after signing tests | Must |
| NFR-SEC-008 | Audit Log is append-only from ginn's side and not reachable from any realm (it lives in Owned Paths, which are never shareable). | Attempt to modify the log from inside a Custom realm with maximum permissions | Must |
| NFR-SEC-009 | The Control Plane ↔ backend channel accepts only peers running as the same host user. | Connect as a different user; connection refused | Must |
| NFR-SEC-010 | No telemetry; network connections go only to image registries, distribution repositories and the AUR (AS-022). | Network capture during the full test suite | Must |

### 4.3 Scalability
| ID | Requirement | Target | Verification | Priority |
|---|---|---|---|---|
| NFR-020 | Realms per user | ≥ 10 realms without degrading NFR-004/NFR-005 | Create 10 realms, re-run NFR-004/005 | Should |
| NFR-021 | Concurrently running realms | ≥ 5, each within NFR-002/NFR-003 | Run 5 idle realms, measure | Should |
| NFR-022 | Control Plane load time | ≤ 1 s to show the dashboard with 20 realms | Timed open, 20 runs | Should |

### 4.4 Availability & reliability
| ID | Requirement | Target | Verification | Priority |
|---|---|---|---|---|
| NFR-030 | Crash safety | After SIGKILL of ginn at any point in any operation, no Realm Home loses data and every realm is in a state from DR-020 within 5 s of the next ginn command | Fault-injection test: kill at 50 random points per operation type | Must |
| NFR-031 | Rebuild atomicity | 0 cases in which a failed rebuild leaves the old realm unusable | Fault-injection during rebuild, 50 runs | Must |
| NFR-032 | Rebuild time | ≤ 10 min for a realm with ≤ 50 manifest packages (no AUR, no Kali everything) on 100 Mbps | Timed rebuild, 3 runs per distribution | Should |
| NFR-033 | Supervised restart | Restart within 10 s of a services-on realm crash; give up after 5 crashes in 60 s (AS-028) | FR-081 tests | Must |

### 4.5 Observability
| ID | Requirement | Target | Verification | Priority |
|---|---|---|---|---|
| NFR-040 | Error reporting | 100% of failures exit non-zero with a documented `GINN-E-*` code and a one-line remediation | Error-code catalogue vs test suite coverage | Must |
| NFR-041 | Doctor coverage | Every dependency in §2.5 and every Host Setup item has a doctor check | Checklist audit | Must |
| NFR-042 | Diagnostic verbosity | `--verbose` prints every runtime and package-manager command ginn runs | Test run with `--verbose` | Should |
| NFR-043 | Audit completeness | 100% of operations listed in FR-124 produce exactly one entry each | Operation/entry count comparison over the test suite | Must |

### 4.6 Usability & accessibility
| ID | Requirement | Target | Verification | Priority |
|---|---|---|---|---|
| NFR-050 | First realm via GUI | ≤ 5 user actions from opening the Control Plane to starting creation of an Arch realm with defaults | Scripted walkthrough | Should |
| NFR-051 | Keyboard and screen reader | Every Control Plane action reachable by keyboard; every control has an accessible name | Keyboard-only walkthrough; accessibility tree inspection | Should |
| NFR-052 | CLI help | Every command and option has `--help` text with at least one example | Help-text audit | Must |

### 4.7 Maintainability & portability
| ID | Requirement | Target | Verification | Priority |
|---|---|---|---|---|
| NFR-060 | Distribution definitions as data | Adding a new distribution (Ubuntu in a later release, for example) requires no change to lifecycle code — only a new distribution definition and Trust Manifest entry | Add a test-only distribution definition in CI | Must |
| NFR-061 | Automated test coverage | ≥ 80% line coverage of non-GUI code; every Must FR has ≥ 1 automated acceptance test | Coverage report; traceability table (Appendix C) | Must |
| NFR-062 | Portability | Full Must-FR suite passes on Void glibc x86_64 and Void musl x86_64 (NVIDIA tests glibc only) | CI matrix | Must |
| NFR-063 | Clean-room provenance | 0 code fragments copied from Distrobox or Anatase | Code-similarity scan against both upstreams before each release | Must |

**Evolvability notes (anticipated change):** new distributions (Ubuntu deferred), aarch64, Trust Manifest updates as upstream image channels change, and the GUI toolkit (chosen by ADR). Designs should isolate these behind stable boundaries.

---

## 5. Interface Requirements

### 5.1 User interfaces

**Control Plane screens**

| ID | Screen | Contents | FRs |
|---|---|---|---|
| IR-001 | Dashboard | Realm list with FR-020 fields, updates indicator, Create button | FR-100, FR-024 |
| IR-002 | Create wizard | Distribution, name, tier, services, Kali "everything" with size estimate | FR-101, FR-014 |
| IR-003 | Realm detail | Status, disk, exported apps, actions | FR-102 |
| IR-004 | Permissions editor | Tier, Custom toggles, shared paths | FR-103 |
| IR-005 | Host health | Doctor results, integrity warnings, acknowledge | FR-104 |
| IR-006 | Progress dialog | Progress and Cancel for long operations | FR-107 |

The Control Plane has no application-installation control (Section 8).

**CLI command inventory (IR-010)**

| Command | FR |
|---|---|
| `ginn host-setup [--undo] --user <name>` (root) | FR-001–003 |
| `ginn doctor [--json]` | FR-007 |
| `ginn integrity [--ack]` | FR-006 |
| `ginn create <distro> [--name] [--tier] [--services] [--with <pkg>…] [--everything] [--resume]` | FR-010, FR-014, FR-122 |
| `ginn list [--json]` · `ginn status <realm> [--json]` | FR-020 |
| `ginn start <realm>` · `ginn stop <realm>` | FR-016 |
| `ginn enter [--root] <realm>` · `ginn exec [--root] <realm> -- <cmd>` | FR-042, FR-048, FR-049 |
| `ginn install <realm> [--aur] <pkg>…` · `ginn remove <realm> <pkg>…` | FR-040, FR-041, FR-043 |
| `ginn export --bin <realm> <binary>` · `ginn unexport --bin <realm> <binary>` | FR-047 |
| `ginn tier <realm> basic\|development\|custom` · `ginn perms <realm> …` | FR-060–066 |
| `ginn services <realm> on\|off` · `ginn autostart <realm> on\|off` | FR-083, FR-084 |
| `ginn rebuild <realm>` · `ginn update <realm>\|--all [--check]` | FR-021–024 |
| `ginn delete <realm> [--purge-home]` | FR-019 |
| `ginn log [<realm>] [--since <time>]` | FR-125 |

IR-011: Exit codes — 0 success; non-zero failure with a `GINN-E-*` code on stderr; `ginn exec` returns the inner command's exit code.

### 5.2 External interfaces consumed
| ID | Interface | Use |
|---|---|---|
| IR-020 | Podman (rootless) | Sole container runtime (CR-003) |
| IR-021 | Official image channels for Arch, Debian, Fedora, Kali | Base images, verified per DR-040 |
| IR-022 | Distribution repositories via native package managers inside realms | Package install/update |
| IR-023 | AUR (inside Arch realms only) | `--aur` builds |
| IR-024 | runit user supervision | Services-on realm supervision |
| IR-025 | Wayland and X11 display sockets; PipeWire/PulseAudio sockets | Display and audio for realm apps |
| IR-026 | GPU device nodes; NVIDIA host userspace libraries (glibc) | GPU access |
| IR-027 | Host SSH agent and GPG agent sockets | Via Agent Proxy only |
| IR-028 | Host session D-Bus | Via filter only (AS-012) |
| IR-029 | freedesktop Desktop Entry spec, XDG Base Directory spec | Menu export, Owned Paths |

### 5.3 Interfaces exposed
| ID | Interface | Notes |
|---|---|---|
| IR-030 | CLI (IR-010), including `--json` outputs | Stable contract for scripts and agents (AS-025); to be formalized downstream |
| IR-031 | Control Plane ↔ backend local channel | Same-user only (NFR-SEC-009); to be formalized downstream |
| IR-032 | Exported desktop entries `ginn-<realm>-<app>.desktop` | Host menu integration |

### 5.4 Hardware / OS interfaces
| ID | Interface |
|---|---|
| IR-040 | x86_64 CPU; `/dev/kvm` when present (Development tier) |
| IR-041 | `/dev/dri/*`; NVIDIA device nodes (glibc hosts) |
| IR-042 | Kernel: user namespaces, overlay or FUSE overlay, cgroup v2 |
| IR-043 | Removable media mount roots `/run/media/$USER`, `/media`, `/mnt` |

---

## 6. Data Requirements

### 6.1 Entity inventory & relationships
| ID | Entity | Key attributes | Relationships |
|---|---|---|---|
| DR-001 | **Realm** | name (DR-002), distro (DR-005), tier, permission set, services mode, autostart, everything flag, state (DR-020), image reference + verified digest, created/updated timestamps | 1 Realm → 1 Realm Home, 1 Install Manifest, 0..n Exported Entries, 0..n Audit Entries |
| DR-006 | **Realm Home** | path inside Owned Paths | Belongs to 1 Realm; survives rebuild |
| DR-007 | **Install Manifest** | entries: package name, source (repo \| aur \| metapackage), install method (ginn \| native) | Belongs to 1 Realm; drives rebuild |
| DR-008 | **Exported Entry** | realm, source desktop ID, host entry file, icon copy, or bin launcher | Belongs to 1 Realm |
| DR-009 | **Host Setup Record** | user, subordinate ID range, every host path written, timestamp | 1 per configured user |
| DR-010 | **Owned Paths** | `$XDG_DATA_HOME/ginn`, `$XDG_CONFIG_HOME/ginn`, `$XDG_STATE_HOME/ginn`, `$XDG_RUNTIME_DIR/ginn`, Podman per-user storage, user runit supervision directory, and files matching `ginn-*.desktop` in `$XDG_DATA_HOME/applications` | The only host locations ginn writes after Host Setup |
| DR-040 | **Trust Manifest** | per distribution: image source, verification material (a pinned digest, a signing key, or both) | Shipped with ginn; read-only at runtime |
| DR-050 | **Audit Entry** | timestamp (UTC, ms), user, realm, operation, parameters, before/after permission diff, outcome, error code | Append-only |

**Field validity rules (domain primitives — invalid values cannot be created):**

| ID | Field | Rule | Null allowed |
|---|---|---|---|
| DR-002 | Realm name | `^[a-z][a-z0-9-]{0,31}$`; unique per user; reserved: `ginn`, `host`, `root`, `all` | No |
| DR-003 | Package name | Arch/AUR: `^[a-z0-9@_+][a-z0-9@._+-]{0,127}$`; Debian/Kali: `^[a-z0-9][a-z0-9.+-]{1,127}$`; Fedora: `^[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}$`. Globs, versions, paths, provides rejected (AS-027). | No |
| DR-004 | Shared path | Absolute, normalized, symlinks resolved; final target strictly under `$HOME`, `/run/media`, `/media` or `/mnt`. Never shareable: `/`, `/etc`, `/usr`, `$HOME` itself, `~/.ssh`, `~/.gnupg`, all Owned Paths, `~/.local/share/applications`, `~/.config/autostart`; shell startup files never writable (AS-011). | No |
| DR-005 | Distribution ID | Enum: `arch`, `debian`, `fedora`, `kali` | No |
| DR-011 | Tier | Enum: `basic`, `development`, `custom` | No |
| DR-012 | Services mode / autostart | Booleans; autostart may be true only when services mode is on | No |
| DR-013 | Everything flag | Boolean; may be true only when distro = `kali` | No |

### 6.2 Volumes & growth
| ID | Item | Expected |
|---|---|---|
| DR-014 | Realms per user | Typical 1–5; designed for ≥ 10 (NFR-020) |
| DR-015 | Disk per realm | Image + ≤ 200 MiB at creation; grows with user installs. Kali everything: tens of GB (exact estimate shown before download, FR-014) |
| DR-016 | Audit Log | Small (one line per operation); bounded by AS-021 |

### 6.3 Retention & deletion
| ID | Requirement |
|---|---|
| DR-017 | Realm Home is retained on rebuild and on delete; removed only by `--purge-home` with confirmation. |
| DR-018 | Partial artifacts from failed or cancelled operations are removed during rollback (FR-120, FR-107). |
| DR-019 | Audit Log rotates at 10 MiB, keeping 5 rotated files (AS-021). |

### 6.4 Privacy / compliance classification
| ID | Data | Class | Rule |
|---|---|---|---|
| DR-021 | Realm Home | **Sensitive** (may hold shell history, tokens, credentials) | Never shared between realms; never exported; inside Owned Paths |
| DR-022 | SSH/GPG private keys | **Secret** | Never enter a realm (NFR-SEC-007) |
| DR-023 | Audit Log | Internal | Records operations and paths only, never file contents or package contents |
| DR-024 | Telemetry | None collected | AS-022 |

### 6.5 Migration / seed data
| ID | Requirement |
|---|---|
| DR-025 | Seed: the Trust Manifest and per-distribution Integration Package Set definitions ship with ginn. |
| DR-026 | No migration from Distrobox/Toolbx containers (AS-026). |

### 6.6 Consistency & durability expectations (per workflow)
| ID | Workflow | Guarantee (business terms) |
|---|---|---|
| DR-030 | Any CLI change, then viewing the Control Plane | **Read-your-writes:** once a CLI command reports success, the Control Plane shows the new state within 1 s (NFR-010). |
| DR-031 | Refreshing or reopening the Control Plane | **Monotonic reads:** the Control Plane never shows a realm state older than one it has already shown. |
| DR-032 | Audit Log and operation history | **Consistent prefix:** an operation's completion is never recorded or shown before its start; entries appear in the order operations started. |
| DR-033 | Concurrent changes to one realm | **Single writer:** one mutating operation per realm; others rejected (FR-123). |
| DR-034 | Rebuild | **Atomic swap:** the new root replaces the old in one step; until then the old realm is intact. |
| DR-035 | Any successful operation | **Durability:** state is flushed to disk before success is reported; it survives power loss. |

**Realm state model (DR-020)**

| State | Allowed next states |
|---|---|
| `creating` | `stopped`, `broken`, removed (rollback or cancel) |
| `stopped` | `running`, `rebuilding`, `updating`, `deleting` |
| `running` | `stopped`, `rebuilding`, `updating`, `failed`, `deleting` (stops first) |
| `updating` | previous state (`stopped` or `running`), `broken` (FR-120) |
| `rebuilding` | previous state (success or failure; old root intact on failure) |
| `failed` | `running`, `stopped`, `deleting` |
| `broken` | `creating` (resume), `deleting` |
| `deleting` | (removed) |

---

## 7. Constraints

| ID | Constraint | Source |
|---|---|---|
| CR-001 | Product name **Ginnungagap**; command name `ginn`. | User |
| CR-002 | Host platform is Void Linux, x86_64, glibc and musl; host init is runit. ginn must not require systemd on the host. | User, Q-009 |
| CR-003 | Rootless Podman is the only container runtime and the only runtime dependency beyond its helpers. Distrobox is not used or installed. | Q-001 |
| CR-004 | All ginn code is new. No code is copied from Distrobox (GPL-3.0) or Anatase (AGPL-3.0). | User, Q-001 |
| CR-005 | License: Apache-2.0. | Q-020 |
| CR-006 | After Host Setup, ginn never invokes the host package manager and writes only to Owned Paths. | Q-002 |
| CR-007 | Realm contents are limited to: official minimal image + Integration Package Set + requested packages and their declared dependencies. | Feature 3, Q-004 |
| CR-008 | Owner: Bongbetic. No fixed deadline (AS-002). | User profile, AS-002 |
| CR-009 | This SRS mandates no implementation language or GUI toolkit; those are ADR decisions. | Q-014 |

---

## 8. Out of Scope (v1 — agents must not build these)

| # | Exclusion | Source |
|---|---|---|
| OOS-1 | Ubuntu realms | Q-005 |
| OOS-2 | aarch64 and any non-x86_64 host | Q-009 |
| OOS-3 | Shared or system-wide realms; rootful realms | Q-012, Q-016 |
| OOS-4 | Making the Void root filesystem read-only | Q-002 |
| OOS-5 | Installing applications from the Control Plane | Q-014 |
| OOS-6 | Automatic or scheduled realm updates | Q-011 |
| OOS-7 | Hosts other than Void Linux | Q-020 |
| OOS-8 | Flatpak management | Q-020 |
| OOS-9 | Kali tools needing host-level network control: Wi-Fi monitor mode, host interface changes, raw-socket or ARP scanning of physical networks | Q-016 (C-7) |
| OOS-10 | Privileged mode for any realm | Q-016 |
| OOS-11 | Migration from Distrobox/Toolbx; per-realm CPU/memory quotas | AS-026 |
| OOS-12 | Telemetry | AS-022 |
| OOS-13 | Automatic export of CLI binaries; editing shell startup files | AS-008 |

---

## 9. Acceptance & Release Criteria

SRS approval (signatures on this document) is separate from release acceptance below.

v1 is released when all of the following hold:
1. Every **Must** FR passes its automated acceptance tests on: (a) Void glibc x86_64 with AMD or Intel GPU, (b) Void musl x86_64 with AMD or Intel GPU, (c) Void glibc x86_64 with NVIDIA GPU (FR-069 and GPU tests).
2. Every **Must** NFR meets its target, recorded in a signed verification report.
3. **Services gate:** FR-080 and FR-081 pass on both glibc and musl hosts (resolves R-001).
4. **Host integrity gate:** the FR-005 full-filesystem diff shows zero changes outside Owned Paths and tier-shared paths.
5. **Provenance gate:** NFR-063 similarity scan shows no copied code.
6. **Security gate:** NFR-SEC-001…010 verified; the FR-062/FR-067 key-leak tests pass.
7. No open defect of severity 1 or 2.
8. Every Should FR is either passing or explicitly deferred in the release notes with its ID.

---

## 10. Open Questions

| ID | Question | Blocks |
|---|---|---|
| — | None open. | — |

---

## Appendix A. Elicitation Log

| Q | Question | Answer | Produced IDs |
|---|---|---|---|
| Q-001 | Core architecture (C-1) | Podman only runtime; ginn reimplements Distrobox's role in new code; Distrobox not installed | CR-003, CR-004, FR-010 |
| Q-002 | Meaning of "immutable" (C-3) | One-time Host Setup; afterwards writes only to own folders + `~/.local/share/applications`, verified by checksum | FR-001–006, CR-006, DR-010, AS-020 |
| Q-003 | systemd support (Feature 2) | Per-realm services mode: real systemd inside; runit supervises the container; minimal init when off | FR-080–083, FR-001 |
| Q-004 | Meaning of "base" (C-2, C-5) | Official minimal image + fixed integration set + app and dependencies | FR-010, FR-013, CR-007 |
| Q-005 | Distribution scope | Arch, Debian, Fedora Must; Kali Should with everything option; Ubuntu out | FR-014, FR-027, OOS-1 |
| Q-006 | Install model | `ginn install` or native package manager; auto export/unexport | FR-040–045, FR-049 |
| Q-007 | AUR (C-6) | Transient build toolchain, removed after build; no helper app | FR-043 |
| Q-008 | Permission tiers | Spaces-equivalent Basic/Development/Custom; realm has own home | FR-060–067, FR-048 |
| Q-009 | Platform | x86_64; glibc and musl hosts | CR-002, NFR-062 |
| Q-010 | GPU | AMD/Intel via realm dependencies; NVIDIA host libs injected read-only; no NVIDIA on musl | FR-013, FR-069 |
| Q-011 | Rebuild and updates | Home kept; manifest replayed; manual updates only; GUI shows availability | FR-021–024 |
| Q-012 | Multi-user | Per-user rootless realms; invisible to others | FR-025, OOS-3 |
| Q-013 | Docker/VMs in realms | Development tier only; Docker daemon needs services on | FR-062, FR-085, AS-005 |
| Q-014 | GUI requirements | Wayland and X11, any DE, unprivileged, toolkit by ADR, lifecycle scope only | FR-100–105, CR-009, OOS-5 |
| Q-015 | Overhead targets and ranking | Accepted as proposed. *Rationale: characteristic catalogue and least-worst trade-off ranking per ref-architectural-characteristics.* | NFR-001–008, §4 ranking |
| Q-016 | Security baseline (STRIDE) and C-7 | Rootless, never privileged, verified images, agent proxy, audit log, same-user GUI; Kali raw network in own namespace only | NFR-SEC-001–010, FR-011, FR-012, FR-027, FR-067, FR-124, OOS-9 |
| Q-017 | Input validity | Realm name, distro, package, path, tier rules; no empty values. *Rationale: domain-primitive validation per ref-secure-by-design.* | DR-002–005, DR-011–013, NFR-SEC-005 |
| Q-018 | GUI/CLI agreement and data safety | Read-your-writes ≤ 1 s, monotonic reads, single writer, atomic rebuild, broken state. *Rationale: anomaly-based elicitation per ref-ddia-consistency.* | DR-030–035, FR-122, FR-123, NFR-010 |
| Q-019 | Failure behaviour | 3 retries then rollback; signature abort; disk precheck +10%; doctor; integrity warning; delete removes menu entries | FR-006, FR-007, FR-019, FR-120, FR-121 |
| Q-020 | Non-goals and constraints | Exclusion list accepted; Apache-2.0; timeline not given (→ AS-002) | §8, CR-005, CR-008 |

**Contradictions resolved:** C-1 → Q-001 · C-2 → Q-004 · C-3 → Q-002 · C-4 → Q-001, Q-015 · C-5 → Q-004 · C-6 → Q-007 · C-7 → Q-016.

---

## Appendix B. Domain Glossary

| Term | Definition |
|---|---|
| **Agent Proxy** | ginn component giving Development realms restricted access to the host SSH and GPG agents (FR-067). |
| **Audit Log** | Append-only record of ginn operations (DR-050). |
| **Control Plane** | The ginn graphical interface for realm lifecycle (§3.6). |
| **Development tier** | Permission tier for development work (FR-062). |
| **Basic tier** | Default permission tier for applications (FR-061). |
| **Custom tier** | Tier with individually chosen permissions (FR-063). |
| **Distribution ID** | One of `arch`, `debian`, `fedora`, `kali` (DR-005). |
| **Everything option** | Kali-only creation option installing `kali-linux-everything` (FR-014). |
| **Spaces** | Anatase Linux's per-distribution environment feature; the behavioural model for Realms (REF-1). |
| **Official image** | A distribution's minimal container image from the source named in the Trust Manifest (DR-040). |
| **Exported Entry** | A host desktop entry or bin launcher that runs something inside a realm (DR-008). |
| **Host** | The Void Linux system on which ginn runs. |
| **Host Integrity Fingerprint** | Hash snapshot of the AS-020 path set, compared before/after mutating operations (FR-006). |
| **Host Setup** | One-time root operation preparing the host for a user (FR-001). |
| **Host Setup Record** | List of host paths written by Host Setup (DR-009). |
| **Install Manifest** | Record of packages explicitly installed in a realm, used by rebuild (DR-007). |
| **Integration Package Set** | Fixed per-distribution packages added to every realm: CA certificates and locale data (FR-013). |
| **Minimal init** | Small init process used in services-off realms (FR-082). |
| **Mutating operation** | Any operation changing a realm, its permissions, exports or the Audit Log, other than read-only queries. |
| **Owned Paths** | The only host locations ginn writes after Host Setup (DR-010). |
| **Realm** | An isolated environment of one distribution belonging to one user (AS-001, DR-001). |
| **Realm Home** | A realm's persistent home directory, mounted at the host home path inside the realm (DR-006). |
| **Realm Owner** | The host user a realm belongs to. |
| **Rebuild** | Re-creating a realm root from a fresh image while keeping its home and replaying its manifest (FR-021). |
| **Services mode** | Per-realm setting: on = systemd as realm init; off = minimal init (FR-080–083). |
| **Tier** | A realm's permission level: Basic, Development or Custom (DR-011). |
| **Trust Manifest** | Shipped list of image sources and verification material (DR-040). |
| **runit user supervisor** | User-level runit supervision established by Host Setup, used for services-on realms (FR-081). |

---

## Appendix C. Traceability Hooks (to be filled downstream)

| FR | Design doc § | Test ID |
|---|---|---|
| FR-001 | | |
| FR-002 | | |
| FR-003 | | |
| FR-004 | | |
| FR-005 | | |
| FR-006 | | |
| FR-007 | | |
| FR-010 | | |
| FR-011 | | |
| FR-012 | | |
| FR-013 | | |
| FR-014 | | |
| FR-015 | | |
| FR-016 | | |
| FR-017 | | |
| FR-018 | | |
| FR-019 | | |
| FR-020 | | |
| FR-021 | | |
| FR-022 | | |
| FR-023 | | |
| FR-024 | | |
| FR-025 | | |
| FR-026 | | |
| FR-027 | | |
| FR-040 | | |
| FR-041 | | |
| FR-042 | | |
| FR-043 | | |
| FR-044 | | |
| FR-045 | | |
| FR-046 | | |
| FR-047 | | |
| FR-048 | | |
| FR-049 | | |
| FR-060 | | |
| FR-061 | | |
| FR-062 | | |
| FR-063 | | |
| FR-064 | | |
| FR-065 | | |
| FR-066 | | |
| FR-067 | | |
| FR-068 | | |
| FR-069 | | |
| FR-080 | | |
| FR-081 | | |
| FR-082 | | |
| FR-083 | | |
| FR-084 | | |
| FR-085 | | |
| FR-100 | | |
| FR-101 | | |
| FR-102 | | |
| FR-103 | | |
| FR-104 | | |
| FR-105 | | |
| FR-106 | | |
| FR-107 | | |
| FR-120 | | |
| FR-121 | | |
| FR-122 | | |
| FR-123 | | |
| FR-124 | | |
| FR-125 | | |

---

## Appendix D. Risk Register

| ID | Risk | Impact | Mitigation | Owner |
|---|---|---|---|---|
| R-001 | systemd as init inside a rootless container on a non-systemd (runit) host may lack cgroup v2 delegation, so services mode fails or degrades. | FR-080/081 fail on some hosts | Host Setup establishes delegation (FR-001); doctor check (FR-007); release gate §9.3; early technical spike before HLD | HLD author |
| R-002 | Rootless Kali cannot do raw-socket/ARP scans of physical networks or monitor mode. | Reduced Kali usefulness vs a native Kali install | Accepted (Q-016, OOS-9); document clearly in Kali help text | Product |
| R-003 | Upstream image channels may not offer signatures; digest pinning then requires frequent Trust Manifest updates. | Stale bases or create failures | Trust Manifest update process defined in an ADR; base is upgraded on first update | HLD author |
| R-004 | False host-integrity warnings if the user runs `xbps` while a ginn operation is in progress. | User confusion | Warning text names this cause; acknowledgement flow (FR-006) | Product |
| R-005 | NVIDIA host library injection can break after a host driver update while a realm is running. | GPU apps fail until realm restart | Doctor detects version mismatch; restart restores | HLD author |
| R-006 | NFR-004 (≤ 300 ms launch delay) may be tight for runtime exec paths. | Missed performance target | Measure in the first prototype; ADR on launch path | HLD author |
| R-007 | Desktop environments differ in how quickly they notice new menu entries. | NFR-009 appears unmet even when files are written | Test against KDE Plasma, GNOME, Xfce; NFR-009 measures file presence | QA |
| R-008 | Apps needing session D-Bus services outside the filter (AS-012) break in Basic tier. | App failures | Custom tier can widen the filter; doctor/log reports denied calls | Product |
| R-009 | Clean-room rule (CR-004) is hard to prove if developers know Distrobox internals. | Licensing exposure | NFR-063 scan; design from this SRS and public specs only | Engineering lead |
