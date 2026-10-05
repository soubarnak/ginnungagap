# Releasing the Void port

A release is a tag on the `void` branch (`v0.0.1`), the `spaces` template pointing at GitHub's tarball of that
tag with its sha256, and the packages built from it. The packages are built locally with
`void/tools/xbps-build.sh`; a signed repository can be assembled locally with `release.sh repo` (below). Nothing
is published or uploaded by any script here.

## One command

```
void/tools/release.sh cut            # prints the plan and changes nothing
void/tools/release.sh cut --yes      # does all of it
```

`cut --yes` verifies the pinned distfiles, uses the local tag `v0.0.1` (which must point at HEAD; it makes the tag
when there is none), pushes `void` and the tag, waits for GitHub's tarball, pins its checksum, commits and pushes that
and builds the packages from the committed template (`xbps-build.sh --release`). It is the only thing in the tree
that pushes a tag, and it has not been run: the tag exists only locally. The steps below are the same thing by hand.

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
that other people use does. The key is made once, kept offline, and never committed (`*.pem` and `*.key` are in `.gitignore`):

```
openssl genrsa -aes256 -out ~/.config/ginnungagap/spaces-repo.pem 4096    # asks for a passphrase
chmod 600 ~/.config/ginnungagap/spaces-repo.pem
```

The key on the maintainer's machine was first made without a passphrase (`openssl genrsa -out ... 4096`) so that
the first end-to-end run could be unattended. It has since been encrypted in place with
`openssl rsa -aes256 -in KEY -out KEY.new`, which keeps the same key and therefore the same fingerprint. It is
mode 0600 in a 0700 directory:

* location: `~/.config/ginnungagap/spaces-repo.pem` (outside the repository)
* signed by: `Spaces Void port <soubarnakarmakar@gmail.com>`
* xbps fingerprint: `f4:55:72:f9:ac:23:eb:b3:c3:e3:f8:b3:a9:24:97:39`

`release.sh repo` now needs the passphrase: `xbps-rindex` asks for it, or it is taken from `XBPS_PASSPHRASE`.
`release.sh cut` does not sign anything and does not need it. Nothing is published yet. Before publishing, check
that a client's first sync still shows the fingerprint above. It was used for
`release.sh repo` once, before the key was encrypted: all ten packages (five templates, x86_64 and aarch64) were signed into `dist/repo` (not
committed, not served), and `xbps-install -S` into a scratch root showed the signer and the fingerprint above.

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

## Serving the signed repository without a server

`release.sh repo` writes a flat directory: `x86_64-repodata`, `aarch64-repodata`, the `.xbps` files and their `.xbps.sig2`
files. xbps only needs those files under one URL, and the assets of a GitHub release are exactly that: upload everything in
`dist/repo` to the release of the tag and use `https://github.com/soubarnak/ginnungagap/releases/download/v0.0.2` as the
repository URL. GitHub answers with a redirect to its CDN; xbps follows it (checked against a local server that redirects the
same way: the index, the signature, the key import and an install into a scratch root all worked).

* `releases/latest/download/` follows the newest release automatically, but GitHub's "latest" skips pre-releases, so a
  release has to be created without `--prerelease` (`gh release create vX --latest`). v0.0.3 is the first one that is; the
  earlier two are pre-releases and are reachable only by their own tag URL.
* The set must be the release build of the tag (`xbps-build.sh --release`, and `--release --arch aarch64`): copy those
  packages into a clean directory and give it to `repo --from`. The default `--from` is the build directory, which also holds
  the test builds with higher revisions (`--revision N`), and `repo` takes the newest of each.
* `repo` indexes and signs every architecture on its own (`XBPS_ARCH`): `xbps-rindex -a` skips packages of a foreign
  architecture, so an earlier version of the script produced only `x86_64-repodata`.
* Nothing in the tree uploads anything. `gh release upload v0.0.2 dist/repo/* --clobber` is the whole publishing step, and
  the fingerprint printed by a client's first sync (above) belongs in the README next to the URL.

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
desktop session and stay with the maintainer. The first runs on GitHub: both package builds passed; the unit tests failed because the `void-glibc-full` image has
no `/tmp` (every `tempfile` test); the workflow now creates it (see "CI status" in `void/spike/RESULTS.md`). The
packages are built from the checkout (`xbps-build.sh --committed` makes a `git archive` tarball with its own checksum and
puts it in xbps-src's source cache), so CI does not need the tag or GitHub's tarball.

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

* Publishing: the tag is pushed by `release.sh cut` only, the GitHub release is made by hand, and the signed repository of
  v0.0.2 is its release assets (`gh release upload`); a later release needs its own `repo` run and upload.
* The checks that need a Void host with LXC, root and a desktop session (`m5_check.py` ... `m9_check.py`) are not in CI.
* aarch64 is cross-built, never run, and **cannot be run on the maintainer's x86_64 host**: the ceiling there is the cross
  build and `check_guest_abi.py --target aarch64`. The only way to run it is an aarch64 Void VM under QEMU TCG (install
  the package, run the unit tests and `spaces-void doctor`), which takes hours and has not been started; do it only if
  that is worth it. musl is out of scope (see "Architectures").
