# What the Void dev install puts on this machine

Created by `sudo void/tools/dev-install.sh` (idempotent, copies files, never links into the
checkout). Reverted by `sudo void/tools/dev-uninstall.sh` (see the end of this file).

## Files (all root:root)

| Path | What |
|---|---|
| `/usr/lib/python3.14/site-packages/spaces/` | copy of `src/spaces` (35 files, byte-compiled) |
| `/usr/bin/spaces` | console script, `from spaces.__main__ import main` |
| `/usr/bin/spaces.priv` | `#!/bin/sh` wrapper: PATH=`/usr/lib/spaces/void/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`, then `exec /usr/bin/python3 -I -c 'import sys; from spaces.priv import main; sys.exit(main())' "$@"`. This is the path the polkit policy binds to. |
| `/usr/lib/spaces/spaces-pam`, `spaces-broker`, `spaces-system-broker` | host native helpers, built by `make -C native` |
| `/usr/lib/spaces/guest/{pam_spaces.so,spaces,spaces-portal,spaces-secret-helper,spaces-open,spaces-system-broker}` | guest native helpers (glibc <= 2.17 checked by `check_guest_abi.py`) |
| `/usr/lib/spaces/spaces-lxc` | cgroup wrapper from `void/bin/spaces-lxc` |
| `/usr/lib/spaces/void/bin/` | empty shim directory (first on `spaces.priv`'s PATH) |
| `/usr/share/spaces/{pam,keys,repos,portal,system-bridge,systemd}` | data files (31 files); `systemd/run-spaces-proc.mount` has `ConditionVirtualization=container` |
| `/usr/share/polkit-1/actions/org.anatase.spaces.policy` | polkit actions (unmodified upstream file) |
| `/etc/pam.d/spaces` | `data/pam/spaces.system-auth` (includes Void's `system-auth`, pam_unix) |
| `/etc/apparmor.d/spaces-container` | AppArmor profile, loaded with `apparmor_parser -r` (the launcher also loads it on demand) |
| `/etc/spaces/config.json` | version 1, `packages: [fastfetch, screen, tmux, zsh]` for arch/fedora/kali/ubuntu; no mounts, no overlays |
| `/etc/spaces/dev-install.state` | what the installer added (keyring, config, packages); used by the uninstaller |
| `/usr/share/keyrings/ubuntu-archive-keyring.gpg` | from `ubuntu-keyring_2026.08.18.tar.xz` (sha256 `652d6b53...f7c4`); installed only after both Ubuntu archive keys, in particular `F6ECB3762474EDA9D21B7022871920D1991BC93C`, were found in it |

Not installed on purpose: `spaces@.service` systemd units, `.desktop` entries and icons for
distros, `rankmirrors` (Arch), SELinux policy, `/etc/polkit-1/rules.d/*` (wheel is already an
administrator through `/usr/share/polkit-1/rules.d/50-default.rules`).

## xbps packages

`pam-devel` was installed by this milestone (build dependency of `pam_spaces.so`). Everything else
the build and runtime need was already present (gcc, make, pkg-config, glib-devel, python3-Pillow,
python3-rich, python3-textual, polkit, lxc, debootstrap, apparmor, gnupg, curl, runit, elogind,
xdg-dbus-proxy, librsvg-utils, dconf). `dev-install.sh` installs any of them that is missing.
Remove with `sudo xbps-remove pam-devel` or `dev-uninstall.sh --remove-packages` (the latter only
knows about packages recorded in the state file of that install).

## Created at run time

| Path | Created by |
|---|---|
| `/var/lib/spaces/ubuntu/` (rootfs, home, info.json, autostart-users) | `sudo spaces create ubuntu --preset basic` (Ubuntu 26.04 "resolute") |
| `/var/cache/spaces/ubuntu/` | same, persistent apt cache |
| `/etc/sv/spaces-ubuntu/` + `/var/service/spaces-ubuntu` symlink | `LxcBackend.ensure_service` on first start (runit service with a permanent `down` file, plus `log/` svlogd service) |
| `/run/runit/supervise.spaces-ubuntu{,.log}` | runsv |
| `/var/log/spaces/ubuntu/` | svlogd |
| `/run/spaces/` (tmpfs) | launcher: LXC config, ready marker, auth socket, desktop state |
| `/sys/fs/cgroup/spaces/ubuntu/` | launcher, removed on stop; root `cgroup.subtree_control` stays empty |
| `/usr/local/share/applications/spaces-ubuntu-v1-*.desktop`, `spaces-icons/` | application shortcut export (a desktop feature, M4) |

## Services

No service is enabled at boot. `spaces-ubuntu` is supervised by runsvdir but `down` by default;
`spaces enter`, `spaces start` and `sv once` start it, `sv down` stops it.

## Symlinks

`/var/service/spaces-NAME -> /etc/sv/spaces-NAME` (one per created space). No other symlinks.

## Uninstall

```
sudo void/tools/dev-uninstall.sh                    # stop services, remove all installed files; keep spaces
sudo void/tools/dev-uninstall.sh --purge            # additionally delete /var/lib/spaces, /var/cache/spaces, /var/log/spaces
sudo void/tools/dev-uninstall.sh --remove-packages  # additionally xbps-remove packages recorded at install
```

It removes `/var/service/spaces-*`, `/etc/sv/spaces-*`, the AppArmor profile (unloaded first),
the keyring (if this install added it), `config.json` (if unchanged) and the exported shortcuts.
