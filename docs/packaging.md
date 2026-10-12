# Packaging ginn for Void Linux

ginn ships as Void Linux xbps packages (AS-003). The packages declare Podman and its helpers as dependencies (DEP-1), so ginn never calls the host package manager itself. The package template lives in `packaging/void/srcpkgs/ginn/template`.

Two packages are built from that template:

- `ginn`: the CLI, with the Python package and the `ginn` command.
- `ginn-gui`: the Control Plane GUI subpackage (GTK4 and libadwaita). It moves the GUI module, the desktop entry and the icons out of `ginn`.

Both are built for `x86_64` (glibc) and `x86_64-musl` (NFR-062).

## Install from the release repository

The packages are published as assets of each GitHub release, with signed repository metadata (NFR-SEC-004).

1. Add the repository:

   ```sh
   sudo tee /etc/xbps.d/20-ginn.conf <<'EOF'
   repository=https://github.com/soubarnak/ginnungagap/releases/latest/download
   EOF
   ```

2. Install the packages. The first install asks to import the release signing key. Compare the fingerprint it shows with the one below before you accept it:

   Release signing key fingerprint: `<FINGERPRINT: set at the first release>`

   ```sh
   sudo xbps-install -S ginn ginn-gui
   ```

3. Prepare the host once. This is a root step and it is idempotent:

   ```sh
   sudo ginn host-setup --user $USER
   ginn doctor
   ```

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

The CI workflow (`.github/workflows/ci.yaml`) runs these jobs on every push to `void` and on every pull request:

- `test`: the unit suite, ruff, mypy and coverage on the Void glibc and musl containers (NFR-061, NFR-062).
- `provenance`: the code-similarity scan against the reference sources (NFR-063). It needs the reference URLs in the `PROVENANCE_REPOS` environment value of that job.
- `package`: builds the glibc package with `tools/xbps-build.sh` inside a privileged Void container, as a non-root user, and uploads the `.xbps` files as a workflow artifact. The privileged container matches the Void project's own CI. It builds from HEAD (`GINN_LOCAL_SOURCE=1`) because the release tag may not exist yet. The musl target is not built in CI yet.

To run the provenance scan locally, pass the reference URLs yourself:

```sh
PROVENANCE_REPOS="<reference-url> <reference-url>" tools/provenance-scan.sh
```

The scan prints the commit of each reference. It exits non-zero on any match. It writes the HTML report only when `PROVENANCE_REPORT` is set, and that report shows reference code, so do not publish it.

## Release procedure

Versions follow the project version in `pyproject.toml`. The package version is the same version without the `.dev0` marker.

1. Set the release version in `pyproject.toml` (for example `0.1.0`) and update the template `version=` to match. Commit.
2. Check the release state:

   ```sh
   tools/release.sh check
   ```

   It fails when the template and `pyproject.toml` versions differ, when the tag already exists, or when the working tree has changes.

3. Tag the release:

   ```sh
   tools/release.sh cut
   git push origin v<version>
   ```

4. Write the checksum of the GitHub tag archive into the template, then commit it:

   ```sh
   tools/release.sh checksum https://github.com/soubarnak/ginnungagap/archive/refs/tags/v<version>.tar.gz
   ```

5. Build both targets:

   ```sh
   GINN_MUSL=1 tools/xbps-build.sh ~/void-packages
   ```

6. Sign the packages and build the repository. The key is never committed, and `*.pem` and `*.key` are ignored by git:

   ```sh
   GINN_SIGNING_KEY=/secure/path/ginn-release.pem \
       tools/release.sh repo ~/void-packages/hostdir/binpkgs ./release-repo
   ```

7. Create the GitHub release for the tag. Upload every file in `./release-repo` as a release asset, so that `latest/download/` serves the repository files.

GitHub regenerates tag archives on request. If a checksum stops matching, attach a fixed source tarball as a release asset and point `distfiles` at it instead.

## Signing key

Generate the release key once, outside the repository:

```sh
openssl genrsa -out /secure/path/ginn-release.pem 4096
```

Keep the key offline or in a secret store. Record its fingerprint in this document, in the install section above, before the first release.
