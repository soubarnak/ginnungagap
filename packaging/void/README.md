# Void packaging

`srcpkgs/ginn/template` is the xbps-src template for ginn. It builds two packages:

- `ginn`: the CLI, the Python package, and the five native helpers in `/usr/libexec/ginn` (installed by `make -C native install`).
- `ginn-gui`: the Control Plane (FR-100..107). `srcpkgs/ginn-gui` is a symlink to `ginn`, as xbps-src expects for a subpackage. The subpackage owns the `ginn/gui` module, the `ginn-gui` script, the desktop entry `org.bongbetic.ginn.desktop` and the icon `org.bongbetic.ginn.svg`. The last two come from `src/ginn/gui/data/`. The icon is a copy of `art/ginnungagap-mark.svg`, and the desktop file is not in the wheel.

Build it locally from a void-packages checkout, as a normal user (xbps-src refuses root):

```sh
tools/xbps-build.sh ~/void-packages
```

The script copies these templates into `srcpkgs/`, runs `binary-bootstrap`, and builds `ginn` with the check stage. Set `GINN_MUSL=1` to also build `x86_64-musl`, and `GINN_LOCAL_SOURCE=1` to build from HEAD when no release tag exists yet.

The template keeps `checksum=@CHECKSUM@` until a release is cut. See `docs/packaging.md` for the release procedure.
