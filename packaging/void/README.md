# Void packaging

`srcpkgs/ginn/template` is the xbps-src template for ginn. The `ginn-gui` subpackage is deferred to M9 (FR-100..107): xbps-src rejects an empty subpackage, so the subpackage and its `srcpkgs/ginn-gui` symlink arrive with `src/ginn/gui`.

Build it locally from a void-packages checkout, as a normal user (xbps-src refuses root):

```sh
tools/xbps-build.sh ~/void-packages
```

The script copies these templates into `srcpkgs/`, runs `binary-bootstrap`, and builds `ginn` with the check stage. Set `GINN_MUSL=1` to also build `x86_64-musl`, and `GINN_LOCAL_SOURCE=1` to build from HEAD when no release tag exists yet.

The template keeps `checksum=@CHECKSUM@` until a release is cut. See `docs/packaging.md` for the release procedure.
