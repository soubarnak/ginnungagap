# Releasing the Void port

A release is a tag on the `void` branch (`v0.0.1`), the `spaces` template pointing at GitHub's tarball of that
tag with its sha256, and the packages built from it. Nothing here is signed or published to an xbps
repository; the packages are built locally with `void/tools/xbps-build.sh`.

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

* Signing of releases or packages, an xbps repository, CI (the checks need a Void host with LXC, root and a
  desktop session, so `m9_check.py`/`m8_check.py` run on the maintainer's machine only).
* `aarch64` and musl hosts; the guest helpers are checked against glibc 2.17 on x86_64 only.
