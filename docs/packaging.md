# Packaging ginn for Void Linux

ginn ships as Void Linux xbps packages (AS-003). The packages declare Podman and its helpers as dependencies (DEP-1), so ginn never calls the host package manager itself. The package template lives in `packaging/void/srcpkgs/ginn/template`. It declares `crun` as the OCI runtime (ADR-004; corrected in R8, 0d7e52e). The package installs `README.md` as documentation and `NOTICE` as a license file (`vlicense`, Apache-2.0 §4(d)). It does not install `LICENSE`: Void ships the Apache-2.0 text under `/usr/share/licenses/Apache-2.0`, which covers §4(a), and xlint rejects a `vlicense` of that license.

Two packages are built from that template:

- `ginn`: the CLI, with the Python package and the `ginn` command.
- `ginn-gui`: the Control Plane GUI, with GTK4 and libadwaita. It depends on `ginn` at the same version, `python3-gobject`, `gtk4` and `libadwaita`. It moves the GUI module, the `ginn-gui` command, the desktop entry and the icon out of `ginn`. `packaging/void/srcpkgs/ginn-gui` is a link to `ginn`.

The package is built for `x86_64` (glibc) and `x86_64-musl` (NFR-062).

The `ginn-gui` subpackage (the Control Plane GUI, FR-100..107) is defined in the same template. It packages the GUI module in `src/ginn/gui`, with the desktop entry and the icon. xbps-src rejects an empty subpackage, so it needed `src/ginn/gui` to exist first.

## Install from the release

Every GitHub release carries a complete signed xbps repository: the packages with their `.sig2` signatures, the signed `x86_64-repodata` index, `SHA256SUMS`, the xbps-src template (`template`) and `traceability.md`. xbps reads the repository straight from the newest release, so no other hosting is used (no gh-pages branch, no GitHub Pages).

1. Add the repository once, then install. The Control Plane GUI is the second package, `ginn-gui`:

   ```sh
   echo 'repository=https://github.com/soubarnak/ginnungagap/releases/latest/download' | sudo tee /etc/xbps.d/20-ginn.conf
   sudo xbps-install -S ginn ginn-gui
   ```

2. The first `-S` shows the signing key fingerprint and asks to import it. The fingerprint must match this one. Plain `-S` shows it before the import. `-y` skips the check:

   ```
   fa:d1:00:32:ce:b1:b9:fd:73:05:05:76:75:54:37:25
   ```

3. Updates arrive with `sudo xbps-install -Su`.
4. Prepare the host once. This is a root step and it is idempotent:

   ```sh
   sudo ginn host-setup --user $USER
   ginn doctor
   ```

The packages are built for glibc x86_64 only. The template lists `x86_64-musl` in `archs`, but the release workflow does not build it, so the release carries `x86_64-repodata` and no other index. A noarch package would be indexed under all nine Void architectures (`tools/release.sh repo` does this); ginn is not noarch, because it builds the native helpers.

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
- `provenance`: the code-similarity scan against the reference sources (NFR-063). It reads the reference URLs from `PROVENANCE_REPOS` in that job. It uploads the `provenance-report` artifact even when the gate fails. The report shows reference code, so it is for human triage only. See `docs/provenance.md`.
- `package`: a build check. It builds the glibc package with `tools/xbps-build.sh` inside a privileged Void container, as a non-root user, and uploads the `.xbps` files as a workflow artifact. The artifact is unsigned and is not a release. The privileged container matches the Void project's own CI. It builds from HEAD (`GINN_LOCAL_SOURCE=1`). The musl target is not built in CI yet.

The release workflow (`.github/workflows/release.yml`) runs only on a pushed tag `ginn-v*` (see Release procedure).

To run the provenance scan locally, pass the reference URLs yourself. An entry is `URL` or `URL@REF`, where `REF` is a branch, tag or commit SHA:

```sh
PROVENANCE_REPOS="<reference-url>[@REF] <reference-url>[@REF]" \
    PROVENANCE_REPORT_DIR="$HOME/provenance-report" tools/provenance-scan.sh
```

The scan prints a table of every scanned file and the commit of each reference. It exits 1 when a file is over the gate and 2 on a configuration error. It copies the HTML report to `PROVENANCE_REPORT_DIR` only when that variable is set, and the report shows reference code, so do not publish it. Details are in `docs/provenance.md`.

## Release procedure

Releases are tag-driven. Never build or upload release assets by hand. The version is the project version in `pyproject.toml`, without the `.dev0` marker, and it equals the template `version=`. The tag is `ginn-vX.Y.Z` and must equal that version: the workflow fails otherwise.

1. Set the release version in `pyproject.toml` and in the template `version=`. Commit.
2. Check the release state:

   ```sh
   tools/release.sh check
   ```

   It fails when the template and `pyproject.toml` versions differ (a `.dev0` marker counts as a difference), when the tag `ginn-v<version>` already exists, or when the working tree has changes.

3. Push the commit, then the tag. Pushing needs the maintainer's explicit go-ahead each time:

   ```sh
   git push origin void
   tools/release.sh cut          # creates the annotated tag ginn-v<version> locally
   git push origin ginn-v<version>
   ```

4. The workflow `.github/workflows/release.yml` does the rest, in four jobs:
   - `verify`: `tools/release.sh verify-tag` compares the tag with `pyproject.toml` and the template.
   - `test`: the full suite, ruff, mypy and the traceability check in the Void glibc container.
   - `release`: builds the packages with `tools/xbps-build.sh` (from the tagged commit, `GINN_LOCAL_SOURCE=1`), then `tools/release.sh repo` with the key. For each architecture it runs `XBPS_ARCH=<arch> xbps-rindex -a` on the packages, signs the index (`xbps-rindex --privkey KEY --signedby "Bongbetic <soubarna@live.in>" -s .`) and signs each package (`-S`, which writes the `.sig2`). It writes `SHA256SUMS` and attaches everything to the GitHub release, with `docs/release-notes/v<version>.md` as the notes when that file exists. A noarch package would be indexed for `x86_64`, `x86_64-musl`, `i686`, `aarch64`, `aarch64-musl`, `armv7l`, `armv7l-musl`, `armv6l` and `armv6l-musl`; ginn is arch-specific, so only `x86_64-repodata` is built.
   - `bump`: runs `tools/release.sh bump`, which writes the `version` and the sha256 `checksum` of the GitHub tag archive into `packaging/void/srcpkgs/ginn/template`, sets the package names in the README, and commits both straight to `void` (no pull request).

The release `latest` must carry the full index and the packages, because users read `releases/latest/download`. A release is complete only when the `release` job succeeds.

`packaging/void/srcpkgs/ginn/update` lets `./xbps-src update-check ginn` and Void's update bot find new tags: it reads `releases/latest` and matches the number after `releases/tag/ginn-v`.

GitHub regenerates tag archives on request. If a checksum stops matching, attach a fixed source tarball as a release asset and point `distfiles` at it instead.

To test the repository steps without a release, run `tools/release.sh repo <binpkgs-dir> <empty-out-dir>` in a Void container. Without `GINN_SIGNING_KEY` it builds an unsigned repository, which xbps refuses as a remote repository; use it for a local check only.

## Signing key

The repository is signed with an RSA 4096 key shared with the maintainer's other xbps repositories, so users accept one fingerprint: `fa:d1:00:32:ce:b1:b9:fd:73:05:05:76:75:54:37:25`. Changing the key forces every user to import a new one, so do not replace it.

- The private key stays on the maintainer's machine (`~/.local/share/taema-release/privkey.pem`, mode 600) and is never committed. `*.pem` and `*.key` are ignored by git.
- The workflow reads it from the Actions secret `XBPS_PRIVKEY`. It writes it to a mode 600 temporary file, mounts it read-only into the build container and removes it afterwards. Set the secret once:

  ```sh
  gh secret set XBPS_PRIVKEY < ~/.local/share/taema-release/privkey.pem
  ```
