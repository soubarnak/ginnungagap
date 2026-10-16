![Ginnungagap](art/ginnungagap-mark.svg)

# Ginnungagap

Ginnungagap (`ginn`) gives a Void Linux host isolated, per-distribution environments called Realms, for Arch Linux, Debian, Fedora and Kali Linux. Each Realm runs on rootless Podman under your own user account. You install applications into a Realm and run them as native desktop and terminal applications. Apart from a one-time Host Setup, ginn does not change the host: it writes only to its own paths and never runs the host package manager.

## Status

Version 1.0.0 is the first release. On a Void Linux glibc x86_64 host with rootless Podman, you can create, start, stop, enter, install into, export from, update, rebuild and delete Realms. The Control Plane GUI (`ginn-gui`) is included, and its host run is still open.

Not every requirement of [GINN-SRS-001 v1.0](GINN-SRS-001-v1.0.md) is verified yet. The [v1.0.0 release notes](docs/release-notes/v1.0.0.md) list each release gate that is not met, the Should requirements that are deferred, and the host runs still open. The release repository is unsigned (see [Install](#install)).

## Requirements

- Void Linux on x86_64 with glibc. The template declares a musl package, but musl is not built or tested for 1.0.0.
- Rootless Podman 5.8.8 with crun 1.30.1. These are the versions the 1.0.0 host runs were recorded with. The package also depends on conmon, netavark, aardvark-dns, passt, catatonit, fuse-overlayfs, shadow (`newuidmap` and `newgidmap`), xdg-dbus-proxy, runit, util-linux and containers-common.
- runit as the host init. ginn does not need systemd on the host (CR-002).
- A regular user account. Host Setup adds its subordinate UID and GID ranges to `/etc/subuid` and `/etc/subgid`, once.
- For `ginn-gui`: GTK4, libadwaita and python3-gobject. The `ginn-gui` package depends on them.
- Python 3.12 or newer, for development (see [Development](#development)).

## Install

### From the GitHub release

The v1.0.0 release is at `https://github.com/soubarnak/ginnungagap/releases/tag/v1.0.0`. It has these files:

- `ginn-1.0.0_1.x86_64.xbps` and `ginn-gui-1.0.0_1.x86_64.xbps`: the CLI and the Control Plane GUI.
- `x86_64-repodata`: the repository index for those packages.
- `SHA256SUMS`: checksums of the two packages and the index.
- The source tarball and `template`, the xbps-src template, for building from source.
- `traceability.md`: the requirement matrix.

**The repository is unsigned.** xbps accepts a remote repository only when it is signed, so the release files must go into a local directory, and `xbps-install` reads them from that path. xbps cannot check that the packages came from the maintainer. `SHA256SUMS` only shows that the files match the release. A signed repository will follow once the maintainer has a release signing key (see [docs/packaging.md](docs/packaging.md)).

```sh
mkdir -p "$HOME/ginn-1.0.0" && cd "$HOME/ginn-1.0.0"
# Download the release files listed above into this directory, then check them:
sha256sum -c SHA256SUMS
# Install from the local directory (the GUI is optional):
sudo xbps-install -R "$HOME/ginn-1.0.0" ginn ginn-gui
```

### From the xbps-src template

Copy the release's `template` into `srcpkgs/ginn/template` of a void-packages checkout, then run `./xbps-src pkg ginn`. The template carries the checksum of the release tarball. [docs/packaging.md](docs/packaging.md) describes the build.

### From source (development)

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m ginn --help
```

## Quick start

Run the Host Setup once, as root, for your user. It writes only ginn's own paths and the subordinate ID files. Then check the host:

```sh
sudo ginn host-setup --user "$USER"
ginn doctor
```

Create, start and use an Arch Linux Realm. A new Realm is stopped until you start it or enter it:

```sh
ginn create arch                          # stopped Arch realm, named "arch" (use --name to choose another)
ginn start arch
ginn install arch htop                    # native package manager inside the realm
ginn export --bin arch htop               # launcher for a CLI binary, in $XDG_DATA_HOME/ginn/bin
ginn enter arch                           # login shell as your user (--root gives container root)
ginn exec arch -- uname -a                # one command; ginn returns its exit status
ginn status arch
ginn stop arch
```

Desktop applications installed in a Realm appear in your desktop menu by themselves (FR-044), and `ginn install` and `ginn remove` keep the menu in step.

Permissions: a Realm has one of three tiers, `basic`, `development` or `custom`. Only a Custom Realm takes its own toggles and shares:

```sh
ginn create arch --name lab --tier custom
ginn perms lab --share ~/Music --share-ro ~/Pictures   # the same paths inside the realm
ginn tier lab development                              # change the tier; the next start applies it
```

Services mode runs systemd inside a Realm, supervised by runit on the host. It is off by default:

```sh
ginn services arch on
ginn autostart arch on                    # start it when your runit supervisor starts
ginn services arch off
```

Maintenance:

```sh
ginn update arch                          # native full upgrade of the realm
ginn update --all --check                 # report which realms have updates; change nothing
ginn rebuild arch                         # new root from the pinned image; home and packages are kept
ginn log arch --since 2026-10-01T00:00:00Z
ginn delete arch                          # keeps the Realm Home; --purge-home removes it after confirmation
```

Every command takes `--json` (machine-readable output) and `--verbose` (prints every runtime and package-manager command ginn runs). `ginn COMMAND --help` lists the options. Commands whose names start with `_` are internal: ginn runs them itself.

## Control Plane (ginn-gui)

`ginn-gui` is a GTK4 and libadwaita window for the same operations as the CLI: a realm dashboard, a create wizard, realm actions, the permissions editor, host health and progress with cancel. It runs as your user and calls the `ginn` command for each change, so every action matches its CLI command. It does not install applications into a Realm (OOS-5). Start it with `ginn-gui`, or from your application menu.

## Notes

- **Kali** (`ginn create kali`) creates a Kali Realm. It has its own network namespace with outbound access, and raw sockets work inside that namespace only. Kali Realms cannot use Wi-Fi monitor mode, change the host's network interfaces, or run raw-socket or ARP scans of physical networks (OOS-9). Use them for authorized testing only. `ginn create kali --everything` shows a size estimate and asks before it installs the full set.
- **Realms are always rootless.** ginn never passes `--privileged` to Podman, and it has no shared or system-wide Realms (OOS-3, OOS-10).
- **Services mode** runs on Arch, Debian, Fedora and Kali. For Debian, Fedora and Kali, a services-on create installs the init set (systemd and D-Bus) through a provisioning stage (ADR-015), because their pinned images have no `/sbin/init`. Arch's pinned image already ships systemd. The host gate for that stage has not run yet (see the release notes).
- **Out of scope for 1.0.0**: Ubuntu, non-x86_64 hosts, Flatpak, automatic or scheduled updates, telemetry, and migration from other container tools (SRS section 8).

## Design constraints

The SRS sets these constraints (CR-001 to CR-007). Summary:

- **CR-001** The product is named Ginnungagap and the command is `ginn`.
- **CR-002** The host is Void Linux on x86_64, with glibc or musl. The host init is runit, and ginn does not require systemd on the host.
- **CR-003** Rootless Podman is the only container runtime and the only runtime dependency beyond its helpers; Distrobox is not used or installed.
- **CR-004** All ginn code is new; no code is copied from Distrobox or from the Anatase Spaces project.
- **CR-005** The license is Apache-2.0.
- **CR-006** After Host Setup, ginn never runs the host package manager and writes only to its owned paths.
- **CR-007** A Realm holds only the official minimal image, the Integration Package Set, and the packages requested, with their declared dependencies.

## Development

Requires Python 3.12 or newer. Run the checks from the repository root:

```sh
pytest -q
ruff check src tests
mypy src/ginn
tools/traceability.py --check
```

Integration tests run only with `GINN_INTEGRATION=1` on a Void host with rootless Podman. They create real Realms, so run them on a host where that is acceptable. The packaging steps (the xbps-src template, the release tooling and the unsigned repository) are in [docs/packaging.md](docs/packaging.md). See [AGENTS.md](AGENTS.md) for the full rules.

## License

Apache License 2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE) and [COPYRIGHT](COPYRIGHT).
