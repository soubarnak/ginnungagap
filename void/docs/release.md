# Releasing the Void port

A release is a tag on the `void` branch (`v0.0.1`), the `spaces` template pointing at GitHub's tarball of that
tag with its sha256, and the packages built from it. The packages are built locally with
`void/tools/xbps-build.sh`; a signed repository can be assembled locally with `release.sh repo` (below). Nothing
is published or uploaded by any script here.

## The order, and why

The checksum of GitHub's tarball of a tag can only be known once the tag is on GitHub, and the tag's own tree
cannot contain the checksum of itself. So:

1. the commit that gets tagged carries the placeholder checksum (`000...0`) in
   `void/srcpkgs/spaces/template`;
2. the tag is pushed;
3. the real checksum is computed from the downloaded tarball and committed **after** the tag.

That is not a defect: xbps-src reads the template from the void-packages checkout, never from the tarball, and
the tarball only has to match the checksum in the template that is used to build it. The tag therefore stays
reproducible, and the commit after it is the first one on which `xbps-build.sh --release` can succeed.

The other four templates (`ubuntu-keyring`, `spaces-arch-install-scripts`, `spaces-archlinux-keyring`,
`spaces-rankmirrors`) pin upstream tarballs whose checksums are known; `void/tools/release.sh verify`
downloads each distfile and compares it.

## Steps

```
# 0. everything committed on void, tests green
PYTHONPATH=src python3 -m pytest -q
void/tools/release.sh check          # branch, clean tree, versions agree, tag free, xlint, tests
void/tools/release.sh verify         # the pinned upstream distfiles still match

# 1. tag locally (annotated, never pushed by the script)
void/tools/release.sh tag

# 2. push, deliberately and by hand
git push origin void v0.0.1

# 3. pin the checksum of GitHub's tarball
void/tools/release.sh checksum --write   # downloads .../archive/refs/tags/v0.0.1.tar.gz
git commit -am "Pin the v0.0.1 source checksum"
git push origin void

# 4. prove the release builds from the committed template
void/tools/xbps-build.sh --release
sudo xbps-install -S -R ~/.local/share/ginnungagap/void-packages/hostdir/binpkgs spaces
```

A GitHub release object (notes, assets) is optional and separate: `gh release create v0.0.1 ...` from the
pushed tag. The packages do not depend on it, only on the tag's tarball.

If `checksum` later reports a different value for the same tag, GitHub changed how it compresses
archives. Either update the template again (`checksum --write`), or stop depending on GitHub's bytes:
`void/tools/release.sh dist` writes `dist/ginnungagap-0.0.1.tar.gz` from the tag with `git archive | gzip -n`,
which is reproducible; attach it to a GitHub release and point `distfiles` at that asset instead.

## Signed repository

`xbps` verifies repositories and packages with an RSA signature; a local repository needs none, a repository
that other people use does. The key is made once, kept offline, and never committed:

```
openssl genrsa -aes256 -out ~/.config/ginnungagap/spaces-repo.pem 4096    # asks for a passphrase
chmod 600 ~/.config/ginnungagap/spaces-repo.pem
```

xbps shows its own fingerprint of the key (not an `openssl` hash) when a client first syncs the repository; read it
from a scratch root once the repository exists and publish it next to the repository URL:

```
mkdir -p /tmp/fp/var/db/xbps && echo n | XBPS_ARCH=x86_64 xbps-install -r /tmp/fp -R "$PWD/dist/repo" -S
# ... has been RSA signed by "Your Name <you@example.org>"
# Fingerprint: xx:xx:...
```

Then, after `void/tools/xbps-build.sh` (add `--committed` for a release build, `--arch aarch64` for a cross
build; run it once per architecture so both end up in `hostdir/binpkgs`):

```
void/tools/release.sh repo --key ~/.config/ginnungagap/spaces-repo.pem \
        --signedby "Your Name <you@example.org>"          # writes dist/repo, asks the passphrase
```

`repo` copies the newest build of each template's package (per architecture) into `dist/repo` (`--out` to change,
`--from` for another package directory, `--replace` to rebuild an existing one), indexes it with
`xbps-rindex -a`, initialises the signature of the repository (`xbps-rindex --sign`, which records `--signedby`)
and signs every package (`xbps-rindex --sign-pkg`, one `.sig2` file each). The passphrase is asked by
`xbps-rindex`, or read from `XBPS_PASSPHRASE`. Nothing is uploaded: serving `dist/repo` over HTTPS is up to you.
Users add it as `repository=URL` in `/etc/xbps.d/` and `xbps-install -S` shows the signer and the fingerprint once
and asks to trust the key. (Checked locally with a throw-away key: the signed repository installs into a scratch
root after the key is trusted, and a package that was changed by one byte is refused at the hash check.)

## CI

`.github/workflows/void.yaml` runs on pushes and pull requests to `void`:

* `pytest`: the unit tests in `ghcr.io/void-linux/void-glibc-full`, with the tools that tests look for installed (a C
  compiler, `pkg-config` with GIO, `dbus-daemon`, `gpg`, `dconf`, `rsvg-convert`, `script` and `setsid`, bash), so
  that nothing is skipped for want of them; the Ctrl-C test that needs a terminal is deselected as in the template.
* `build`: `xlint` of the templates and the five packages built with `xbps-src` in the same container, with the
  options and setup of void-packages' own `.github/workflows/build.yaml` and `common/travis/prepare.sh`
  (`--privileged`, `/dev` mounted, the `repo-ci` mirror, a `builder` user in `xbuilder`, `XBPS_CHROOT_CMD=uchroot`,
  `binary-bootstrap`). Two entries: `x86_64` with the unit tests run in the build chroot (`-Q`), and `aarch64`,
  cross-built on the x86_64 host without tests (`xbps-src -a aarch64`).

The aarch64 build is the only coverage of that architecture: nothing runs on it, the Arch and Fedora bootstraps
are x86_64 only, and the NVIDIA and desktop paths were never looked at. The `m*_check.py` scripts need LXC, root and a
desktop session and stay with the maintainer. The workflow was checked for syntax only; it has not run on GitHub.

## Architectures

Supported: x86_64 glibc (built and run). Build-only: aarch64 glibc (cross-built in CI). **musl is intentionally
not supported**: the helpers that run inside the guests (`pam_spaces.so`, `spaces`, `spaces-portal`,
`spaces-system-broker`, ...) are linked against glibc and pinned to symbols of glibc 2.17 by
`native/check_guest_abi.py`, because the guests (Ubuntu, Kali, Arch, Fedora) are glibc distributions and load them
into their own processes. The template's `archs="x86_64 aarch64"` names only glibc architectures (xbps-src never
matches a bare name to a `-musl` one), so a musl host cannot build the package at all, and `void-glibc-full` is
the only container the CI uses.

## Bumping the version

`version` in `pyproject.toml` and in `void/srcpkgs/spaces/template` move together (`release.sh check` fails when
they differ); reset `revision=1` and the checksum to the placeholder, add `INSTALL.msg` text if the install
behaviour changed. A packaging-only change keeps the version and raises `revision`.

## Following upstream

`void/tools/rebase-check.sh` fetches `anatase-org/spaces`, lists the new upstream commits, names the files
that both sides changed (the hot ones are `launch.py`, `session.py`, `priv.py`), dry-runs a merge (or
`--rebase`) in a temporary worktree, lists conflicts and runs the tests on a clean result. It does not move
`master` or `void` and pushes nothing. It also lists upstream changes under `native/`, `data/` and
`pyproject.toml`, because `post_install` of the `spaces` template compares `data/portal` and
`data/system-bridge` with what the wheel installs and builds `native/`.

To bring upstream in for real: update `master` from upstream by fast-forward, merge `master` into `void`
(or rebase and force-push if you accept rewriting the branch), run the tests, rebuild with `xbps-build.sh`
and run `void/spike/m8_check.py`.

## Not done

* Publishing: the tag is not pushed by any script, there is no GitHub release and no hosted repository, and the
  repository signing key does not exist yet (the steps above make it).
* The checks that need a Void host with LXC, root and a desktop session (`m5_check.py` ... `m9_check.py`) are not in CI.
* aarch64 is cross-built, never run; musl is out of scope (see "Architectures").
