# Architecture decision records

Architecture decision records (ADRs) record the decisions that shape Ginnungagap (`ginn`). Each ADR states one decision, the context that forced it, the alternatives considered, and the consequences. The requirements come from `GINN-SRS-001-v1.0.md`. The order of work comes from `docs/roadmap.md`.

## Format

- File names are `ADR-NNN-short-title.md`. NNN is a three-digit number. A number is never reused.
- Each ADR follows `TEMPLATE.md` (MADR style): Status, Date, Context, Decision, Alternatives considered, Consequences and Requirement IDs.
- Every ADR cites the SRS IDs it decides for, in its Requirement IDs section. An ADR without SRS IDs is not accepted.
- Trade-offs follow the ranked characteristics in SRS section 4, in this order: performance efficiency, host integrity and isolation, recoverability, interoperability, usability (see Q-015).

## Statuses

- **Proposed**: drafted and under review. The decision is not yet binding.
- **Accepted**: binding. The code follows it. A change needs a new ADR that supersedes it.
- **Superseded**: replaced by a later ADR. The status line names the replacement. The file stays in place.

"Planned" is not a status. A planned entry only reserves a number and a title in this index. No file exists for a planned entry until its spike or task starts it. The new file then starts with the status Proposed.

## Index

| ADR | Title | Status | Requirement IDs |
|---|---|---|---|
| [ADR-001](ADR-001-language-and-gui-toolkit.md) | Language and GUI toolkit | Accepted | CR-002, CR-009, NFR-001, NFR-002, NFR-004, NFR-061, NFR-062, FR-105, FR-106 |
| [ADR-002](ADR-002-podman-cli-over-rest.md) | Drive Podman through its command line, not the REST API | Accepted | CR-002, CR-003, NFR-001, NFR-002, NFR-004, NFR-040, NFR-042, NFR-061, NFR-SEC-005, NFR-SEC-009, DR-010, FR-011 |
| ADR-003 | Services-mode cgroup strategy | Planned (decided by spike S-01 in M1) | R-001, FR-080, FR-081 |
| [ADR-004](ADR-004-runtime-root.md) | Runtime directory and Podman storage root | Accepted | DR-010, DR-033, FR-081, FR-084, FR-123 |
| [ADR-005](ADR-005-image-channels-and-trust.md) | Image channels and Trust Manifest | Accepted | R-003, DR-040, FR-011, FR-023, FR-121, AS-024, CR-007, IR-021, NFR-008, NFR-SEC-004 |
| [ADR-006](ADR-006-development-tier-capabilities.md) | Development-tier capability set without SYS_ADMIN | Accepted | NFR-SEC-002, NFR-SEC-001, NFR-SEC-005, NFR-040, FR-013, FR-021, FR-062, FR-063, FR-085, AS-005, CR-002, CR-003, CR-007, DR-010, IR-040 |
| ADR-010 | Launch path (renumbered from ADR-006 on 2026-10-12, because ADR-006 went to the Development tier; roadmap line 173 still says ADR-006) | Planned (decided by spike S-02 in M1) | R-006, NFR-004, FR-048, FR-049 |
| ADR-007 | Native helpers and memory budget | Planned (decided by spike S-05 in M1) | NFR-002, NFR-003, FR-067 |
| ADR-008 | GPU and NVIDIA injection | Planned (decided by spike S-06 in M1) | FR-046, FR-069, R-005 |
| ADR-009 | GUI to backend channel | Planned (M9; the roadmap assigns no spike) | FR-105, FR-106, NFR-SEC-009, NFR-010, DR-030 |
| [ADR-011](ADR-011-package-manager-semantics.md) | Package manager semantics for refresh, update checks and removal | Accepted | FR-013, FR-023, FR-024, FR-040, FR-041, NFR-060, CR-007 |

## Adding an ADR

1. Take the reserved number for the decision, or the next free number.
2. Copy `TEMPLATE.md` to a new file with the matching name.
3. Set the status to Proposed and cite the SRS IDs.
4. When the decision is agreed, change the status to Accepted and update the index.
