# Packaging ginn for Void Linux

ginn ships as Void Linux xbps packages (AS-003). The packages declare Podman and its helpers as dependencies (DEP-1), so ginn never calls the host package manager itself. The package template lives in `packaging/void/srcpkgs/ginn/template`. It declares `crun` as the OCI runtime (ADR-004; corrected in R8, 0d7e52e). The package installs `README.md` as documentation and `NOTICE` as a license file (`vlicense`, Apache-2.0 §4(d)). It does not install `LICENSE`: Void ships the Apache-2.0 text under `/usr/share/licenses/Apache-2.0`, which covers §4(a), and xlint rejects a `vlicense` of that license.

Two packages are built from that template:

- `ginn`: the CLI, with the Python package and the `ginn` command.
- `ginn-gui`: the Control Plane GUI, with GTK4 and libadwaita. It depends on `ginn` at the same version, `python3-gobject`, `gtk4` and `libadwaita`. It moves the GUI module, the `ginn-gui` command, the desktop entry and the icon out of `ginn`. `packaging/void/srcpkgs/ginn-gui` is a link to `ginn`.

The package is built for `x86_64` (glibc) and `x86_64-musl` (NFR-062).

The `ginn-gui` subpackage (the Control Plane GUI, FR-100..107) is defined in the same template. It packages the GUI module in `src/ginn/gui`, with the desktop entry and the icon. xbps-src rejects an empty subpackage, so it needed `src/ginn/gui` to exist first.

## Install from the release

The GitHub release of each version carries the xbps packages, the repository index, `SHA256SUMS`, the source tarball, the xbps-src template (`template`) and `traceability.md`. The repository is **unsigned**. xbps accepts a remote repository only when it is signed (`xbps-install(1)`: remote repositories must be signed using `xbps-rindex`), so the release files are used as a local directory.

1. Download every release file into an empty directory, then check the checksums:

   ```sh
   mkdir -p "$HOME/ginn-1.0.0" && cd "$HOME/ginn-1.0.0"
   sha256sum -c SHA256SUMS
   ```

2. Install from that directory. The Control Plane GUI is the second package, `ginn-gui`:

   ```sh
   sudo xbps-install -R "$HOME/ginn-1.0.0" ginn ginn-gui
   ```

   Nothing in an unsigned repository lets xbps check who built the packages. `SHA256SUMS` shows only that the files match the release.

3. Prepare the host once. This is a root step and it is idempotent:

   ```sh
   sudo ginn host-setup --user $USER
   ginn doctor
   ```

## Signed repository (not yet)

No release signing key exists for 1.0.0. When the maintainer has one (see Signing key), `GINN_SIGNING_KEY` makes `tools/release.sh repo` sign the packages and each index (release step 6). Then the install section gains a signed route: a `repository=` line in `/etc/xbps.d/20-ginn.conf` that names the release URL, and the key fingerprint recorded here. The first install asks to import the key, and the fingerprint shown must match the recorded one.

## Build locally

You need a checkout of `void-linux/void-packages` on a Void host, or in a Void container. Run the script as a normal user: xbps-src refuses to run as root. It also needs user namespaces (the default `uunshare` chroot) or `uchroot`.

```sh
tools/xbps-build.sh ~/void-packages                  # x86_64 glibc
GINN_MUSL=1 tools/xbps-build.sh ~/void-packages      # also x86_64-musl
GINN_LOCAL_SOURCE=1 tools/xbps-build.sh ~/void-packages   # build from HEAD, no tag needed
```

`tools/xbps-build.sh` copies the template into `srcpkgs/`, runs `binary-bootstrap` and builds with `-Q`. The check stage runs the unit tests from the release tarball, except the repo hygiene test, which needs git metadata and runs in CI instead. The built packages appear in `~/void-packages/hostdir/binpkgs/`.

The `-Q` check stage is not a lint. The template lint is `xlint`, which ships in the void-packages `xtools` tool and is not run here.

The template checksum must match the source archive. Without `GINN_LOCAL_SOURCE=1` the template needs a real checksum, which exists only after the release tag is pushed.

## Run the checks

The CI workflow (`.github/workflows/ci.yaml`) runs these jobs on every push to `void`, on every version tag (`v*`) and on every pull request:

- `test`: the unit suite, ruff, mypy and coverage on the Void glibc and musl containers (NFR-061, NFR-062).
- `provenance`: the code-similarity scan against the reference sources (NFR-063). It reads the reference URLs from `PROVENANCE_REPOS` in that job. It uploads the `provenance-report` artifact even when the gate fails. The report shows reference code, so it is for human triage only. See `docs/provenance.md`.
- `package`: builds the glibc package with `tools/xbps-build.sh` inside a privileged Void container, as a non-root user, and uploads the `.xbps` files as a workflow artifact. On a version tag it also builds the unsigned repository with `tools/release.sh repo` and uploads it as the `ginn-unsigned-repo-x86_64-glibc` artifact. The privileged container matches the Void project's own CI. It builds from HEAD (`GINN_LOCAL_SOURCE=1`) because the release tag may not exist yet. The musl target is not built in CI yet.

To run the provenance scan locally, pass the reference URLs yourself. An entry is `URL` or `URL@REF`, where `REF` is a branch, tag or commit SHA:

```sh
PROVENANCE_REPOS="<reference-url>[@REF] <reference-url>[@REF]" \
    PROVENANCE_REPORT_DIR="$HOME/provenance-report" tools/provenance-scan.sh
```

The scan prints a table of every scanned file and the commit of each reference. It exits 1 when a file is over the gate and 2 on a configuration error. It copies the HTML report to `PROVENANCE_REPORT_DIR` only when that variable is set, and the report shows reference code, so do not publish it. Details are in `docs/provenance.md`.

## Release procedure

Versions follow the project version in `pyproject.toml`. The package version is that version exactly, so a release cannot carry the `.dev0` marker.

1. Set the release version in `pyproject.toml` without `.dev0` (for example `0.1.0`) and update the template `version=` to match. Commit.
2. Check the release state:

   ```sh
   tools/release.sh check
   ```

   It fails when the template and `pyproject.toml` versions differ (a `.dev0` marker counts as a difference), when the tag already exists, or when the working tree has changes.

3. Tag the release:

   ```sh
   tools/release.sh cut
   git push origin v<version>
   ```

4. Write the checksum of the GitHub tag archive into the template, then commit it:

   ```sh
   tools/release.sh checksum https://github.com/soubarnak/ginnungagap/archive/refs/tags/v<version>.tar.gz
   ```

5. Build the glibc package. The musl target is not built for 1.0.0:

   ```sh
   tools/xbps-build.sh ~/void-packages
   ```

6. Build the repository. It is unsigned unless `GINN_SIGNING_KEY` names a private key. The script indexes each package under its own architecture, writes `SHA256SUMS` beside the indexes, and, with a key, signs the packages and each architecture's index. The key is never committed, and `*.pem` and `*.key` are ignored by git:

   ```sh
   tools/release.sh repo ~/void-packages/hostdir/binpkgs ./release-repo                 # unsigned
   GINN_SIGNING_KEY=/secure/path/ginn-release.pem \
       tools/release.sh repo ~/void-packages/hostdir/binpkgs ./release-repo             # signed
   ```

   The script runs only in the CI build container or by the maintainer. The test suite and ginn never run it.

7. Create the GitHub release for the tag. Upload every file in `./release-repo` and `packaging/void/srcpkgs/ginn/template` (named `template`). GitHub serves the tag's source tarball itself. An unsigned repository is for download and local use only (see Install from the release).

GitHub regenerates tag archives on request. If a checksum stops matching, attach a fixed source tarball as a release asset and point `distfiles` at it instead.

## Signing key

No key exists for 1.0.0, and the repository is unsigned. The maintainer generates a release key once, outside the repository:

```sh
openssl genrsa -out /secure/path/ginn-release.pem 4096
```

Keep the key offline or in a secret store. Before the first signed release, record its fingerprint in this document and in the install section.
