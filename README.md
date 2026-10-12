![Ginnungagap](art/ginnungagap-mark.svg)

# Ginnungagap

Ginnungagap (`ginn`) gives a Void Linux host isolated, per-distribution environments called Realms, for Arch Linux, Debian, Fedora and Kali Linux. Each Realm runs on rootless Podman under your own user account. You install applications into a Realm and run them as native desktop and terminal applications. Apart from a one-time Host Setup, ginn does not change the host: it writes only to its own paths and never runs the host package manager.

## Status

Ginnungagap is a clean-room rewrite in progress against [GINN-SRS-001 v1.0](GINN-SRS-001-v1.0.md). The work is ordered in [docs/roadmap.md](docs/roadmap.md). The project is not yet usable: it cannot create or run a Realm yet.

## Design constraints

The SRS sets these constraints (CR-001 to CR-007). Summary:

- **CR-001** The product is named Ginnungagap and the command is `ginn`.
- **CR-002** The host is Void Linux on x86_64, with glibc or musl. The host init is runit, and ginn does not require systemd on the host.
- **CR-003** Rootless Podman is the only container runtime. No other container tool is used or installed.
- **CR-004** All code is new. No code is copied from other projects.
- **CR-005** The license is Apache-2.0.
- **CR-006** After Host Setup, ginn never runs the host package manager and writes only to its owned paths.
- **CR-007** A Realm holds only the official minimal image, the Integration Package Set, and the packages requested, with their declared dependencies.

## Development

Requires Python 3.12 or newer.

```sh
pytest -q
ruff check src tests
mypy src/ginn
tools/traceability.py --check
```

Several of these checks are wired up by later M0 tasks. Integration tests run only with `GINN_INTEGRATION=1` on a Void host with rootless Podman. See [AGENTS.md](AGENTS.md) for the full rules.

## License

Apache License 2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE) and [COPYRIGHT](COPYRIGHT).
