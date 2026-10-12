# Architecture decision records

Architecture decision records (ADRs) record the decisions that shape Ginnungagap (`ginn`). Each ADR states one decision, the context that forced it, the alternatives considered, and the consequences. The requirements come from `GINN-SRS-001-v1.0.md`. The order of work comes from `docs/roadmap.md`.

## Format

- File names are `ADR-NNN-short-title.md`. NNN is a three-digit number. A number is never reused.
- Each ADR follows `TEMPLATE.md` (MADR style): Status, Date, Context, Decision, Alternatives considered, Consequences and Requirement IDs.
- Every ADR cites the SRS IDs it decides for, in its Requirement IDs section. An ADR without SRS IDs is not accepted.
- Trade-offs follow the ranked characteristics in SRS section 4, in this order: performance efficiency, host integrity and isolation, recoverability, interoperability, usability (see Q-015).

## Statuses

- **Proposed**: drafted and under review. The decision is not yet binding.
- **Accepted**: binding. The code follows it. A reversal of the decision needs a new ADR that supersedes it. A change that keeps the decision is an amendment (see Amendments below).
- **Superseded**: replaced by a later ADR. The status line names the replacement. The file stays in place.

An Accepted ADR may carry a qualifier in its status line, such as the spike it was measured on or the requirement IDs it covers. The qualifier names what the evidence covers, and what is pending.

## Amendments

A dated in-place amendment is allowed when the decision stands and only its realisation changes. Each amendment is a dated `Amended:` line under the header, and it states what changed and what did not. A reversal of the decision needs a superseding ADR, and the superseded file stays in place.

"Planned" is not a status. A planned entry only reserves a number and a title in this index. No file exists for a planned entry until its spike or task starts it. The new file then starts with the status Proposed.

## Index

| ADR | Title | Status | Requirement IDs |
|---|---|---|---|
| [ADR-001](ADR-001-language-and-gui-toolkit.md) | Language and GUI toolkit | Accepted | CR-002, CR-009, NFR-001, NFR-002, NFR-004, NFR-061, NFR-062, FR-105, FR-106 |
| [ADR-002](ADR-002-podman-cli-over-rest.md) | Drive Podman through its command line, not the REST API | Accepted | CR-002, CR-003, NFR-001, NFR-002, NFR-004, NFR-040, NFR-042, NFR-061, NFR-SEC-005, NFR-SEC-009, DR-010, FR-011 |
| [ADR-003](ADR-003-services-mode-cgroup-strategy.md) | Services-mode cgroup strategy | Accepted, S-01 on runc and crun (the roadmap argv ran under the pin on both). FR-080 AC-1/AC-2 on crun, and a run from the root-owned `supervisor` leaf on crun, are pending T7.7. | R-001, FR-001, FR-080, FR-081, FR-083, NFR-003 |
| [ADR-004](ADR-004-runtime-root.md) | Runtime directory and Podman storage root | Accepted, logout evidence simulated (S-04s; the exact path `/run/ginn/<uid>` was not run). The real logout run is pending T7.8. | DR-010, DR-033, FR-081, FR-084, FR-123, FR-122, IR-020, CR-003, CR-006, NFR-SEC-005, NFR-040 |
| [ADR-005](ADR-005-image-channels-and-trust.md) | Image channels and Trust Manifest | Accepted | R-003, DR-040, FR-011, FR-023, FR-121, AS-024, CR-007, IR-021, NFR-008, NFR-SEC-004 |
| [ADR-006](ADR-006-development-tier-capabilities.md) | Development-tier capability set without SYS_ADMIN | Accepted | NFR-SEC-002, NFR-SEC-001, NFR-SEC-005, NFR-040, FR-013, FR-021, FR-062, FR-063, FR-085, AS-005, CR-002, CR-003, CR-007, DR-010, IR-040 |
| [ADR-007](ADR-007-idle-memory-budget.md) | Idle memory budget (retitled from "Native helpers and memory budget" on 2026-10-12, because the decision covers only the memory budget) | Accepted, services-off measured (S-05r). Services-on mitigation forms pending S-05s. Helpers pending T7.7, and xdg-dbus-proxy pending T6.5. | NFR-002, NFR-003, NFR-021, FR-068, CR-007 |
| [ADR-008](ADR-008-session-socket-rebind.md) | Session socket rebind for long-running realms, and variable grant attachment (amendment 2026-10-12) | Accepted, simulated (S-04s, S-08s, S-08t; podman 5.8.8 and crun 1.30.1). The rebind order, the identity compare, the batch form and the EINVAL case are pending T4.9 and T4.10, and the real logout run is pending T7.8. The variable grant attachment of the amendment, and its review amendment (attach flags, host-side identity override, X11 exception), are pending the T4.9 host gate. | FR-061, FR-063, FR-064, FR-065, FR-066, FR-081, FR-084, IR-025, NFR-001, DR-010, NFR-SEC-005 |
| ADR-009 | GUI to backend channel | Planned (M9; the roadmap assigns no spike) | FR-105, FR-106, NFR-SEC-009, NFR-010, DR-030 |
| [ADR-010](ADR-010-launch-path.md) | Launch path (renumbered from ADR-006 on 2026-10-12, because ADR-006 went to the Development tier) | Accepted, on runc (crun re-measure, and the cost of the ADR-008 pre-check, in T4.9) | R-006, NFR-001, NFR-004, NFR-005, FR-017, FR-046, FR-048, FR-049, FR-061, FR-082, NFR-SEC-002 |
| [ADR-011](ADR-011-package-manager-semantics.md) | Package manager semantics for refresh, update checks and removal | Accepted | FR-013, FR-023, FR-024, FR-040, FR-041, NFR-060, CR-007 |
| [ADR-012](ADR-012-gpu-nvidia-injection.md) | GPU and NVIDIA injection (renumbered from ADR-008 on 2026-10-12, because ADR-008 went to session socket rebind) | Accepted for the FR-046 renderer check (runc) and the S-06 parts of FR-069. Audio (FR-046 AC-1), menu launch, crun and the CDI env field are pending T6.6 and T5.4. | FR-046, FR-061, FR-069, FR-082, DR-010, IR-025, IR-026, R-005, CR-007 |
| [ADR-013](ADR-013-host-setup-record.md) | Host Setup Record, pre-images and resume | Proposed | DR-009, DR-010, FR-001, FR-002, FR-003, FR-007, NFR-030, NFR-040 |

## Adding an ADR

1. Take the reserved number for the decision, or the next free number.
2. Copy `TEMPLATE.md` to a new file with the matching name.
3. Set the status to Proposed and cite the SRS IDs.
4. When the decision is agreed, change the status to Accepted and update the index.
