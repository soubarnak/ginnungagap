# ADR-001: Language and GUI toolkit

- Status: Accepted
- Date: 2026-10-12

## Context

CR-009 states that the SRS mandates no implementation language and no GUI toolkit. Both choices are made in this ADR.

The constraints that apply are these.

- The host is Void Linux on x86_64, on both glibc and musl. Its init is runit (CR-002, NFR-062). ginn must not require systemd on the host.
- NFR-001 requires zero ginn processes when no realm runs. The runit user supervisor from Host Setup is the only exception. A resident interpreter therefore cannot stay running between commands.
- NFR-002 caps the idle host memory of a running realm with services off at 8 MiB RSS. An interpreter that runs only during a command adds no idle cost. Its start-up cost counts only at command time.
- NFR-004 requires a p95 launch latency of 300 ms or less for an application in a running realm. The SRS lists this target as a risk (R-006). Interpreter start-up adds to every launch.
- SRS section 4 ranks the driving characteristics as follows: performance efficiency (1), host integrity and isolation (2), recoverability (3), interoperability (4), usability (5). Performance is the main cost of an interpreted language, so this choice needs a stated mitigation.
- The packages python3, python3-gobject, gtk4 and libadwaita exist in the Void repository. This was confirmed on the reference host. Their availability on musl is not yet confirmed. Spike S-03 tests it.

For NFR-004, spike S-02 measures the `podman exec` launch path over 100 runs. If its p95 misses 300 ms, a small C fast-path helper (`ginn-run`, which enters the realm with setns) is added. ADR-006 decides that path.

## Decision

1. The ginn CLI and backend are written in Python 3.12 or later. They use the standard library first. The core package has no third-party runtime dependency. Adding one requires a change to this ADR.
2. The Control Plane is written with GTK4 and libadwaita, bound through PyGObject. It ships as the optional subpackage `ginn-gui`, installed with the `gui` extra.
3. The Control Plane runs as the user, never as root (FR-105). The channel between the GUI and the backend is decided by ADR-009.
4. No Python component stays resident. A component that must stay resident is written in C, as one of the helpers in `native/`. Each helper is at most 200 lines.
5. CI runs ruff and mypy in strict mode on `src/ginn`. View logic lives in viewmodels that can be tested without GTK.

## Alternatives considered

- **Go with gotk4.** Go builds static binaries, which simplify distribution on glibc and musl hosts. Its start-up cost is lower than Python's, which helps NFR-004. Rejected: its GUI tooling is weaker than PyGObject's for this project, and development is slower for this scope. The static-binary and start-up benefits do not justify that cost, because NFR-004 has a C fallback. Go remains viable for helper binaries. ADR-007 decides whether helpers may use a language other than C. Until then, helpers are C, as the roadmap and AGENTS.md specify.
- **Rust with gtk-rs.** Rust gives strong performance and memory safety. Rejected for the same reasons as Go: development speed and weaker GUI tooling for this project. Rust remains viable for helper binaries, subject to ADR-007, as with Go. Until then, helpers are C.

## Consequences

Positive:

- The core depends only on the Void `python3` package.
- The GUI is optional. A host that uses only the CLI needs no GTK packages.
- The standard library keeps the dependency surface small, which keeps the code easier to audit.
- Every package the GUI needs is in the Void repository.
- FR-106 CLI parity is proven by task T9.8, a mapping test from the Control Plane actions to CLI commands. The channel that carries those actions is decided in ADR-009.

Negative:

- Interpreter start-up adds to launch latency. NFR-004 is at risk (R-006). Spike S-02 measures it. If the target is missed, ADR-006 adds the C helper `ginn-run`.
- The interpreter uses memory only while a command runs. A resident interpreter is not allowed, so this cost never falls on the idle budget.
- PyGObject and GTK4 on musl are not yet proven. Spike S-03 must pass before M9. NFR-062 requires the Must-FR suite to pass on musl, and FR-100 to FR-103, FR-105 and FR-106 are Must requirements. If S-03 fails, this ADR is reopened before the GUI milestone.
- Any future request for a resident Python process, such as a daemon, conflicts with NFR-001. It requires a superseding ADR.
- Type annotations are required throughout `src/ginn`, because mypy runs in strict mode.
- Most GUI tests run without a display, because logic sits in viewmodels. Only view tests need `xvfb-run`.
- The minimum supported interpreter is Python 3.12. Older interpreters are not supported.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| CR-002 | The host uses runit and no systemd. The CLI and GUI depend only on packages in the Void repository. |
| CR-009 | This ADR makes the language and GUI toolkit choices that the SRS leaves open. |
| NFR-001 | No Python process stays resident. Only C helpers may stay resident. |
| NFR-002 | With no resident interpreter, the idle budget excludes interpreter memory. |
| NFR-004 | Interpreter start-up is a launch risk. Spike S-02 measures it, and ADR-006 sets the mitigation. |
| NFR-061 | Viewmodels hold GUI logic and are tested without GTK. Ruff and mypy run in CI. |
| NFR-062 | PyGObject and GTK4 on musl are gated by spike S-03 before the GUI milestone. |
| FR-105 | The Control Plane runs as the user and never requests root. |
| FR-106 | This ADR does not decide parity. T9.8 proves it, and ADR-009 decides the channel. |
