# ADR-002: Drive Podman through its command line, not the REST API

- Status: Accepted
- Date: 2026-10-12

## Context

CR-003 makes rootless Podman the only container runtime. ginn needs one interface to Podman. There are two candidates:

- the `podman` command line, or
- the libpod REST API, which `podman system service` serves on a local socket.

The REST API needs a long-running service process. On a systemd host, a user unit would socket-activate that process on demand. Void has no systemd (CR-002), so ginn or the runit user supervisor would have to start the service and keep it alive. That process would be resident.

NFR-001 counts processes from ginn executables, so a Podman service does not count literally. Its intent is zero ginn-owned processes when no realm runs. A service that ginn starts and keeps alive is that kind of process in substance. NFR-002 also counts every process "attributable to ginn and the runtime" in the idle budget of 8 MiB RSS, so the service would count there.

The REST API also adds a local socket. Any process that can reach the socket can start and stop containers as the user. NFR-SEC-009 requires the Control Plane channel to accept only peers running as the same user. NFR-SEC-009 governs that channel, not the Podman socket, but the Podman socket would need the same same-user peer check. That check would be a new requirement on ginn to implement and test.

The command line has no resident component. Each operation runs one `podman` process, which exits when the operation ends. Output that ginn parses can be requested as JSON with `--format json`. The adapter can be tested with a record and replay shim (NFR-061), so the tests need no running container runtime.

The SRS ranks performance efficiency first (SRS section 4), so idle cost decides the trade-off. The command line has zero idle cost. Its cost is one process start per user operation, which the launch path measurement in spike S-02 covers. Recoverability ranks third. With no service process, there is no service crash to recover from.

DR-010 makes Podman per-user storage an Owned Path. Ginn's realms need storage that the user's own Podman does not share.

## Decision

1. ginn controls Podman only through its command line. Any command whose output ginn parses uses `--format json`.
2. Every call goes through the adapter Protocol in `podman/adapter.py`. Runtime, operations and tests depend on the Protocol and not on subprocess code. `podman/cli.py` implements it. `tests/fakes/fake_podman.py` is an in-memory fake for tests.
3. Each call is an argv list. ginn never builds a shell string and never uses `shell=True` (NFR-SEC-005).
4. Each call passes `--root` and `--runroot` for ginn's own storage. The default `--root` is `$XDG_DATA_HOME/ginn/podman`, inside the Owned Paths (DR-010). ADR-004 confirms the exact `--root` and `--runroot` paths.
5. With `--verbose`, ginn prints each argv before it runs (NFR-042).
6. Only `podman/models.py` parses Podman output. Only `podman/cli.py` maps Podman failures to `GINN-E-*` codes.
7. A REST implementation may be added later behind the same Protocol, through a superseding ADR. This ADR does not provide one.

## Alternatives considered

- **libpod REST API through `podman system service`.** Benefit: one persistent connection and no process start per call. Rejected: the service must be resident, which conflicts with NFR-001 and counts toward NFR-002. It also adds a local socket that needs a same-user peer check. Void has no systemd socket activation to remove the resident process.

## Consequences

Positive:

- ginn runs no process between commands. There is no service to start, supervise or recover.
- No extra socket is opened.
- `--verbose` prints the exact argv of each call. A user can copy and rerun it by hand.
- The adapter is tested with the replay shim and the in-memory fake, without a container runtime (NFR-061).
- ginn's images and containers are kept apart from the user's own Podman storage. Neither side sees the other's objects.

Negative:

- Each operation starts one `podman` process. This cost is part of the launch path. Spike S-02 measures it against NFR-004. If the target is missed, ADR-006 can route the hot launch path through the C helper `ginn-run`.
- Separate storage means an image that the user already has is stored again for ginn. Disk accounting (FR-121, DR-015) counts ginn's copy.
- ginn depends on the flags and output format of the Podman command line. A Podman upgrade that changes either one breaks the adapter. The change is contained in `podman/cli.py` and `podman/models.py`. Recorded outputs in the replay shim are refreshed by hand when Podman's output changes.
- ginn owns the mapping from Podman exit codes and messages to `GINN-E-*` codes. This mapping is required by NFR-040.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| CR-002 | The host has no systemd, so no socket activation is available. A REST service would have to be resident. |
| CR-003 | Rootless Podman is the only runtime. The command line is the only interface ginn uses. |
| NFR-001 | ginn starts no service process. A resident REST service would conflict with the intent of NFR-001. |
| NFR-002 | A resident REST service would count toward the idle budget, because NFR-002 covers processes attributable to the runtime. |
| NFR-004 | The cost of each `podman` call is part of the launch path that spike S-02 measures. |
| NFR-040 | `podman/cli.py` maps every Podman failure to a `GINN-E-*` code. |
| NFR-042 | `--verbose` prints every Podman argv. |
| NFR-061 | The adapter Protocol, the replay shim and the in-memory fake make the adapter testable without a runtime. |
| NFR-SEC-005 | Every call uses an argv list. No shell is involved. |
| NFR-SEC-009 | A REST socket would need the same same-user peer check. NFR-SEC-009 itself governs only the Control Plane channel. |
| DR-010 | `--root` is inside the Owned Paths. The user's own Podman storage is separate. |
| FR-011 | Image pulls by digest go through the adapter. The check against the Trust Manifest is ginn's own step before use. |
