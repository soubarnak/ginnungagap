# Spaces on Void Linux

This is the user guide for the Void port of Spaces (the `void` branch of ginnungagap, a fork of
[anatase-org/spaces](https://github.com/anatase-org/spaces)). The upstream program is a wrapper
around `systemd-nspawn`; Void has no systemd, so this port runs the same spaces (Ubuntu, Kali,
Arch, Fedora) in LXC containers supervised by runit, with elogind for logins, polkit for
authorisation and AppArmor instead of SELinux. The CLI (`spaces create`, `enter`, `configure`,
`delete`) and the guest integration (host PAM for `sudo`, desktop forwarding, shortcuts, portals,
GPU) are upstream's.

Status: installable as xbps packages built with `xbps-src` (M8), hardened in M9 (the LXC monitor is out of the
guest's reach, the AppArmor profile reviewed) and ready to be tagged `v0.0.1` (`void/docs/release.md`). The
development install (`void/tools/dev-install.sh`) still exists, for hacking on the sources only.
Tested on Void x86_64 (glibc), runit, elogind 252, kernel 7.x, LXC 6.0, niri.

## Install

There is no Void repository for Spaces yet: build the packages from this repository with
[void-packages](https://github.com/void-linux/void-packages)' `xbps-src` and install them from the
local repository that it produces. You need `git`, membership of the `xbuilder` group (xbps-src
uses unprivileged user namespaces; `bubblewrap` must be installed) and about 2 GB of space (the void-packages checkout and chroot take under 1 GB, plus downloads).

```
git clone <this repo> ginnungagap && cd ginnungagap && git checkout void
void/tools/xbps-build.sh                       # clones void-packages (shallow) into
                                               # ~/.local/share/ginnungagap/void-packages, bootstraps it,
                                               # builds all five packages, prints the repository path
sudo xbps-install -S -R ~/.local/share/ginnungagap/void-packages/hostdir/binpkgs spaces
```

To keep the repository for later `xbps-install -u`, add it to xbps' configuration:

```
echo 'repository=/home/YOU/.local/share/ginnungagap/void-packages/hostdir/binpkgs' |
    sudo tee /etc/xbps.d/20-spaces-local.conf
```

(local repositories need no signature). `xbps-build.sh` builds the current working tree, including
uncommitted files (`--committed` builds `HEAD`), `--check` runs the unit tests in the build chroot,
`--revision N` builds revision N (used to test upgrades), `--lint` only runs `xlint`. It renders the
checksum of that tree into a copy of the `spaces` template inside the void-packages checkout; the
committed template (`void/srcpkgs/spaces/template`) names the release tarball
`https://github.com/soubarnak/ginnungagap/archive/refs/tags/v${version}.tar.gz`; its checksum is the all-zero
placeholder until the tag is pushed and `void/tools/release.sh checksum --write` has pinned the real one
(why in that order: `void/docs/release.md`). `xbps-build.sh --release` builds exactly that committed template.

```
sudo xbps-remove spaces                        # stops the spaces, removes services and the profile;
                                               # /var/lib/spaces, /var/cache/spaces, /var/log/spaces stay
sudo xbps-remove -o                            # then also drop the private helper packages, if wanted
sudo xbps-install -u spaces                    # upgrade (after a new build); running spaces keep running
```

### The packages

| Package | What |
|---|---|
| `spaces` | the program: Python package, `/usr/bin/{spaces,spaces.priv,spaces-void,spaces-session-env,ubuntu,fedora,kali,arch-linux}`, native helpers and guest helpers in `/usr/lib/spaces`, data in `/usr/share/spaces`, polkit policy, four menu entries `/usr/share/applications/spaces-{ubuntu,arch,fedora,kali}.desktop` with their icons, `/etc/pam.d/spaces`, the AppArmor profile, `/etc/spaces/void.json`, the (unlinked) `spaces-autostart` runit service, documentation |
| `ubuntu-keyring` | `/usr/share/keyrings/ubuntu-archive-keyring.gpg` (debootstrap's default for Ubuntu); same name and path as a future Void package, which would simply replace it |
| `spaces-arch-install-scripts` | pacstrap, arch-chroot and genfstab under `/usr/lib/spaces/void/arch-install-scripts` (private path and name) |
| `spaces-archlinux-keyring` | the Arch keyring files under `/usr/share/spaces/void/archlinux-keyring`; its INSTALL hook creates the host pacman keyring in `/var/lib/spaces/.host/arch/gnupg` |
| `spaces-rankmirrors` | pacman-contrib's `rankmirrors` at `/usr/lib/spaces/rankmirrors` |

The Fedora bootstrap image is not packaged: the `dnf5` shim downloads and verifies it on first use
(`/var/lib/spaces/.host/fedora`). `spaces` depends on `lxc apparmor polkit elogind libelogind
eudev-libudev runit debootstrap pacman bash coreutils util-linux tar xz gnupg curl sudo dconf
xdg-dbus-proxy librsvg-utils python3-Pillow python3-rich python3-textual` besides the four packages
above and the automatic Python and shared-library dependencies. `x86_64` glibc is the supported
platform (the guest helpers are checked against glibc 2.17, which is also why musl is unsupported); `aarch64` is
cross-built in CI and has never been run.

What the hooks do (shown once as INSTALL.msg): INSTALL loads the AppArmor profile (`apparmor_parser -r`,
only when AppArmor is enabled; an older `spaces-container` profile and its file are dropped), runs `spaces-void sync-config` and seeds `/etc/pacman.d/mirrorlist` when
it is absent; none of it can fail the transaction. On an upgrade the profile is reloaded, the generated
configuration refreshed and a linked `spaces-autostart` restarted; running spaces are not touched
(they keep their loaded profile until they restart). REMOVE (not on upgrades) stops every space,
unlinks and deletes the per-space runit services after making `runsv` and `svlogd` exit, unloads the
profile, deletes `/etc/spaces/config.json` if it is still the generated one and prints that the spaces
are kept. `xbps-remove` mentions that `/var/lib/spaces` and friends are not empty: that is intended.

### Development install (not for normal use)

```
sudo void/tools/dev-install.sh        # copies the working tree to the system paths, idempotent
sudo void/tools/dev-uninstall.sh      # revert; spaces and /var/lib/spaces/.host are kept
sudo void/tools/dev-uninstall.sh --purge   # also delete every space, caches, logs and .host
```

It replaces files the package owns: never run it over the package (remove the package first). The
installer records what it added in `/etc/spaces/dev-install.state`; the file list is
`void/spike/INSTALLED.md` (section "Development install"). Differences from the package: it
installs build tools and `pam-devel`, byte-compiles in place, fetches the Ubuntu keyring, the Arch
tools and `rankmirrors` itself, and does not need the xbps dependencies to be declared.

| Where | What |
|---|---|
| `/usr/lib/python3.*/site-packages/spaces/` | the Python package |
| `/usr/bin/spaces`, `spaces.priv`, `spaces-session-env`, `spaces-void` | CLI, the root helper behind polkit, the session publisher, the administration CLI |
| `/usr/bin/{ubuntu,fedora,kali,arch-linux}` | entry commands (never `/usr/bin/arch`: coreutils owns it) |
| `/usr/share/applications/spaces-*.desktop`, `/usr/share/icons/hicolor/256x256/apps/spaces-*.png` | one menu entry per distribution (`Terminal=true`, `Exec=` the entry command), see "Menu entries" |
| `/usr/lib/spaces/` | native helpers, guest helpers, the cgroup wrapper `spaces-lxc`, `spaces-nvidia-sync`, shims for `pacstrap`/`dnf5` |
| `/usr/share/spaces/` | data, `config.base.json`, `void/shell/spaces.{sh,fish}` (opt-in) |
| `/etc/sv/spaces-autostart/` | the autostart runit service (installed, not linked) |
| `/etc/apparmor.d/lxc-spaces-container`, `/etc/pam.d/spaces`, the polkit policy | security glue |
| `/etc/spaces/config.json` | generated, see "Configuration" |
| `/var/lib/spaces/NAME/`, `/var/cache/spaces/NAME/`, `/var/log/spaces/NAME/` | a space, its package cache, its log |
| `/etc/sv/spaces-NAME`, `/var/service/spaces-NAME` | one runit service per space, created on first start, always `down` until started |

## Creating and using spaces

```
sudo spaces create ubuntu --preset basic      # also: arch, fedora, kali; --preset develop
spaces enter ubuntu                           # login shell in the space (starts it, ~4 s cold)
spaces enter ubuntu -- id                     # run one command as yourself
sudo spaces enter ubuntu --root -- apt-get update   # as root (administrator authentication)
sudo spaces configure ubuntu --user USER      # give another user (default: the caller) access; interactive
sudo spaces delete ubuntu                     # delete it; its runit service is removed with it
```

Flags verified on this machine: `create --preset basic`, `enter`, `enter -- CMD`, `enter --root -- CMD`, `start`.
`configure`, `delete` and `enter --graphical SPACE -- CMD` (set by the desktop shortcuts, goes before the space name)
are listed in `spaces --help` but were not exercised in M7 (`configure` asks questions in a TUI).

`create` and `configure` need an administrator (polkit asks; `sudo` also works). `enter` and `start`
as yourself need no password. A space is a runit service that is `down` by default and started
with `sv once`; `sudo sv down /var/service/spaces-NAME` stops it (gracefully, `lxc-stop -t 30`).
Once started it runs until stopped or until shutdown.

By default `create` and `configure` enable "autostart at login" for your user (upstream's
`spaces@NAME` user unit). On Void this only records your user name in
`/var/lib/spaces/NAME/autostart-users`; it does nothing until you link the autostart service
(next section). `--no-enable` skips the recording. To opt in for real: `sudo ln -s /etc/sv/spaces-autostart /var/service/`
and `sudo spaces-void autostart enable NAME` (add `--boot` to start at boot rather than at login); to opt out of a
space: `sudo spaces-void autostart disable NAME`. Every `spaces configure` records your user again.

### Entry commands

`ubuntu`, `fedora`, `kali` and `arch-linux` are installed in `/usr/bin`. `ubuntu` alone opens a
shell, `ubuntu apt list --installed` or `ubuntu -- id` runs a command in the space (one leading
`--` is accepted, upstream's wrapper would pass it on).

Opt-in shell snippet `/usr/share/spaces/void/shell/spaces.sh` (bash, zsh, other POSIX shells) defines
the functions `arch`, `ubuntu`, `fedora`, `kali` and friendly hints for `apt`, `apt-get` and `dnf`
that point at the space, but only where the host has no such command. `pacman` and `xbps-*` are never
touched (the host has pacman as a dependency of the Arch bootstrap). `arch` exists only as this
function, because `/usr/bin/arch` is coreutils. Enable it, your choice:

```
# 1. just for you, in ~/.bashrc or ~/.zshrc
[ -r /usr/share/spaces/void/shell/spaces.sh ] && . /usr/share/spaces/void/shell/spaces.sh
# 2. for every bash user
sudo ln -s /usr/share/spaces/void/shell/spaces.sh /etc/bash/bashrc.d/spaces.sh
```

`SPACES_NO_HINTS=1` keeps the functions and drops the hints. A fish version is
`void/shell/spaces.fish` (copy it to `~/.config/fish/conf.d/`; not tested here, no fish on the dev machine).
Nothing in your dotfiles is edited by the installer.

## Services and opt-in integration

| What | How |
|---|---|
| Autostart | `sudo ln -s /etc/sv/spaces-autostart /var/service/` (unlink the same way to turn it off; Void's usual convention) |
| Desktop environment publishing (exact, no `/proc` scan) | niri: `spawn-at-startup "spaces-session-env" "publish"` in `~/.config/niri/config.kdl`; sway/others: run `spaces-session-env publish` at login |
| Shell snippet | see above |

### Autostart

```
spaces-void autostart list                       # what is enabled, and whether the service is linked
sudo spaces-void autostart enable ubuntu         # start ubuntu when you log in
sudo spaces-void autostart enable ubuntu --user bob
sudo spaces-void autostart enable ubuntu --boot  # start at boot (no user mounts yet, as upstream)
sudo spaces-void autostart disable ubuntu [--boot] [--user bob]
```

`spaces-void` re-runs itself through sudo when it needs root. Semantics, mirroring upstream's
`spaces@NAME` user unit (`WantedBy=default.target`) and `systemctl enable` for boot:

* The `spaces-autostart` service watches elogind. At its start and whenever a login changes: for every
  space with the boot flag (`/var/lib/spaces/NAME/autostart-boot`) it runs `sv once` once per boot;
  for every user in `autostart-users` whose state is `active`, `online` or `lingering` it runs `sv once`
  on each space of that user that is not running, once per login (a new session id, or the start of
  lingering).
* A deliberate `sudo sv down` is therefore not undone until that user's next login, nor by a restart of
  the daemon (its per-boot state is in `/run/spaces/autostart/state.json`). A reboot starts over.
* Linking the service starts the matching spaces within seconds (measured: 4 s); without the link nothing
  happens, whatever is enabled. Logs: `/var/log/spaces/autostart/current`.
* A space must not be called `autostart` (it would collide with the service directory).

## Configuration

`/etc/spaces/config.json` (guest packages, extra mounts and overlays per distro) is generated, never
edit it. `spaces-nvidia-sync` rebuilds it before every space start from `/usr/share/spaces/config.base.json`,
the NVIDIA userspace of the host and your `/etc/spaces/void.json`; `sudo spaces-void sync-config` runs it
by hand. A `config.json` that was edited by hand is kept and the new text goes to `config.json.new`.

`/etc/spaces/void.json`:

```json
{
  "desktop_flavor": "auto",
  "distros": {"ubuntu": {"packages": ["htop"]}}
}
```

Packages apply when a space is created (and at its rebuild).

### Desktop flavour

The guests carry KDE's polkit agent and portal backend (the launcher parses the polkit-kde log line,
so they stay). `desktop_flavor` adds a GTK look on a non-KDE host:

* `auto` (default): `XDG_CURRENT_DESKTOP` of the creating user's active graphical elogind session. KDE or
  Plasma gives `kde`; niri, sway, Hyprland, GNOME, XFCE and everything else give `gtk`. With no session
  to look at (boot) the last answer is kept (`/var/lib/spaces/.host/desktop-flavor`), else `gtk`.
* `gtk`: adds to every distro's packages: icon theme (`adwaita-icon-theme`), `gnome-themes-extra` (Debian
  family and Arch), `adw-gtk-theme` / `adw-gtk3-theme` (Arch, Fedora; not packaged on Ubuntu/Kali),
  `xdg-desktop-portal-gtk`, GSettings schemas and dconf, and the GTK3 platform theme for Qt:
  `qt5-gtk-platformtheme` + `qt6-gtk-platformtheme` on Ubuntu and Kali; on Arch and Fedora the Qt
  GTK3 plugin ships inside `qt6-base` / `qt6-qtbase-gui`, so `gtk3` is the package.
* `kde`: adds nothing.

Into an existing space: `sudo spaces-void install-flavor ubuntu [--flavor gtk]` (runs the distro's
non-interactive install as root through `spaces enter --root`, starting the space the normal way, and
prints what it installed). The host exports `QT_QPA_PLATFORMTHEME=gtk3`; without the plugin Qt silently
falls back to its generic theme, with it Qt applications follow the GTK theme.

## Menu entries

Each of the four distributions has a launcher in the host's application menu (`Space (Ubuntu)`, `Space (Arch
Linux)`, `Space (Fedora)`, `Space (Kali Linux)`). It runs the entry command in a terminal (`Terminal=true`,
`Exec=/usr/bin/ubuntu`, ...), which starts the space on first use and gives a login shell in it. They are
`void/data/applications/spaces-*.desktop`; upstream's own entries (`data/applications`, three distributions, calling
`spaces enter`) are not installed. The icons are upstream's launcher icons from `data/icons` (the logo of the
distribution plus the Spaces mark, made by `art/distros/generate.sh` from the logos in `art/distros`; the Kali one
was added for this port).

The distribution logos are trademarks of their owners (Canonical, the Arch Linux project, Fedora Project / Red Hat,
OffSec) and are shown only to identify the launcher of that distribution; their presence in this repository is not
an endorsement by, or affiliation with, those owners, and the repository's licence (AGPL-3.0-or-later) covers
Spaces' own work, not those marks. The repository itself carries no separate disclaimer, upstream's `readme.md`
has none, so this paragraph is the one.

## Security model: what replaces SELinux

Upstream relies on `systemd-nspawn` (namespaces, capability bounding set, seccomp filter, device cgroup)
plus an SELinux policy. This port keeps the same layers except SELinux:

| Layer | Void port |
|---|---|
| Namespaces | LXC: mount, pid, uts, ipc and cgroup namespaces; **the network namespace is shared with the host** (as upstream's default); no user namespace by default (root in a space is host root, as upstream), **an opt-in user namespace** where guest root is an unprivileged kuid, see "User namespace" below. The LXC *monitor* is not in the guest's network namespace, see below |
| Capabilities | `lxc.cap.keep` = nspawn's default set, widened or narrowed per permission level like upstream |
| Syscalls | seccomp: LXC's `common.seccomp` base plus the per-permission adjustments |
| Devices | cgroup2 device controller (eBPF), levels `disabled`, `basic`, `admin`, `full` from the permission settings; at `full` watchdogs and VT/console devices stay denied; `/dev/uinput` and `/dev/uhid` (input injection into the host) are only given at `full`, whatever udev tags them; hot-plug and NVIDIA nodes follow the level |
| Mandatory access control | AppArmor profile `lxc-spaces-container` (derived from LXC's `lxc-container-default-cgns`: it additionally allows the mounts systemd uses to sandbox units, but no fresh `proc`, `sysfs` or cgroup v1 mount; the rest is denied as in LXC) **instead of** the SELinux policy `spaces-selinux`. The name starts with `lxc-` because Void loads `/etc/apparmor.d` at every boot (`/etc/runit/core-services/09-apparmor.sh`), which confines `/usr/bin/lxc-start` with the distribution's `usr.bin.lxc-start`; that profile only allows `change_profile -> lxc-*` and has no local include, so a differently named profile makes every launch fail after a reboot with `Failed to write AppArmor profile`. `m9_check.py` simulates that boot. Reviewed in `void/docs/apparmor-review.md`, including what it cannot stop |
| Authorisation | polkit (`org.anatase.spaces.policy` bound to `/usr/bin/spaces.priv`) and host PAM for guest `sudo`, as upstream |
| cgroups | each space under `/sys/fs/cgroup/spaces/NAME`, `cgroup.subtree_control` of the root stays empty (elogind); a private mount namespace hides elogind's v1 hierarchy from LXC |

What is lost or different compared with upstream on Fedora/Anatase:

* No SELinux: no relabelling of `/var/lib/spaces` or `~/.ssh/config`, no type enforcement between
  host and guest processes. AppArmor confines the container processes by path; the upstream rule
  "it is not possible to mount SSH or GPG directories" is enforced by Spaces' own mount policy only
  (`launch._prepare_mounts` refuses hidden directories, `core.validate_home_name` allows only `.ssh/config`),
  not by a second MAC layer: the host makes those binds, so no container profile could see them.
* AppArmor does not mediate the new mount API (`fsopen`, `open_tree`, `mount_setattr`), so root in a space
  can mount a fresh `proc` and write host sysctls such as `core_pattern` and `sysrq-trigger`. Seccomp cannot
  close it: systemd 259 needs `fsopen`/`fsmount` for its unit credentials and fails (journald, tmpfiles) without them,
  and `mount_setattr` cannot be taken away either. By default this is an accepted risk with upstream parity
  (`systemd-nspawn` without SELinux has it too). The fix is the opt-in **user namespace** (next section): with
  it guest root is not host root and the kernel refuses those writes. Without it do not run untrusted code as root
  in a space (`void/docs/apparmor-review.md`, finding 1).
* **The LXC monitor's command socket** is an abstract socket, and abstract sockets belong to a network
  namespace, which the guest shares with the host. `/usr/lib/spaces/spaces-lxc` therefore starts `lxc-start`
  in a new network namespace that it pins at `/run/spaces/lxc/NAME/netns` (a bind mount of the namespace
  file, removed when the launcher exits), the container config makes the guest join the host's namespace again
  (`lxc.namespace.share.net = /proc/1/ns/net`), and every other `lxc-*` call through the wrapper first enters the
  pinned namespace with `nsenter`. Root in the guest, and anything else outside that namespace, gets
  `ECONNREFUSED` on `@/run/spaces/lxc/NAME/command`. Consequences: a plain `lxc-ls` or `lxc-info` from a
  shell sees nothing, use the wrapper (`sudo /usr/lib/spaces/spaces-lxc lxc-info -P /run/spaces/lxc -n NAME`);
  a container that was started before the upgrade keeps its monitor in the host's namespace (the wrapper
  falls back to it) until the space restarts. An AppArmor rule could not do this job, see
  `void/docs/apparmor-review.md`, finding 2.
* A compromised guest root is host root with a seccomp filter, a capability set, an AppArmor profile and
  a device cgroup between it and the machine; a kernel bug or a mount mistake escapes (as upstream warns).
  Do not give a space the `develop` preset or the `full` device level unless you trust it.
* The portal proxy filter lets the guest call a number of host portals without a Spaces prompt (screen
  saver lock/inhibit, notifications, clipboard, USB, screencast, ...). Review: `void/docs/portal-filter-review.md`.
* Processes started by `spaces enter` run in a transient service of the guest's own systemd (inside the
  container), not in a host scope.

## User namespace (opt-in)

`void/docs/apparmor-review.md` (finding 1) explains why the default design cannot stop guest root from reaching
the host's `/proc/sys` through the new mount API. A space can instead run in a user namespace whose id map shifts
every id except the ones that must mean the same on both sides, so guest root is the unprivileged host uid
1000000 and the kernel's permission checks (sysctl and `/proc/sysrq-trigger` compare with the global root)
refuse the writes. It is **off by default**.

```
spaces-void userns status                     # which spaces use one, and whether /etc/subuid and /etc/subgid cover the map
sudo spaces-void userns enable ubuntu         # add the missing subuid/subgid lines, switch the space to a user namespace
sudo spaces-void userns enable --all
sudo spaces-void userns disable ubuntu
sudo sv down /var/service/spaces-ubuntu       # it takes effect at the next start
```

The choice is the file `/var/lib/spaces/NAME/userns` (`on` or `off`); a space without one follows `"userns": true`
in `/etc/spaces/void.json` (default `false`). `spaces-void doctor` fails when an enabled space lacks subuid/subgid ranges.

The map (`lxc.idmap`) is `0..65535` shifted by 1000000 (root's own range in `/etc/subuid`, not the user's
`100000`, which rootless podman uses), except for the ids of the users of the space (so the home directory, the
`XDG_RUNTIME_DIR` sockets, PipeWire and the D-Bus proxy need no idmapped binds for them) and the host groups that
own device nodes (audio, video, input, render, kvm, plugdev) and the nodes the space is handed. Those ids are listed for root in `/etc/subuid` and `/etc/subgid`
(LXC 6.0 run as root still goes through `newuidmap`, which refuses ids that are not listed); `userns enable` and
`userns setup` add them as `root:ID:COUNT` lines, nothing else edits the files.

What changes, and why:

* The root file system and the host-root-owned Spaces directories that are bound into the guest (`home/root`, the
  package cache, `/run/spaces/NAME/system-bus`, `auth.sock`, `resolv.conf`) are **idmapped mounts**
  (`idmap=container`): nothing is chowned, and guest root sees them as root-owned. The launcher gives the 0700 runtime
  directories above the sources `o+x` (search, not listing) because the binds are set up by an unprivileged helper.
* `/sys` cannot be mounted fresh (sysfs belongs to the shared network namespace), so it is a read-only recursive
  bind of the host's, repeating the host's locked mount flags, and every host mount below it (securityfs, efivarfs,
  the host's cgroup2 root) is hidden under an empty read-only tmpfs. The guest's udev stays off (as under nspawn);
  hot-plug is the host's job.
* The host side of the system-bus relay accepts the mapped root as a peer (`SPACES_GUEST_ROOT_UID`, set by the
  launcher); the host-PAM socket checks the process's cgroup, not its uid, and is unchanged.
* The AppArmor profile has one more remount rule (`nosymfollow`, which systemd adds in a user namespace).
* Behaviour changes inside the space: guest root has no capabilities over the host's network namespace (no
  privileged ports, no raw sockets, no interface configuration, `systemd-resolved` logs that it has no stub
  listener) and cannot write the network sysctls it could before; there is no `sysfs` or `proc` mount of its own.

What it does not do: guest root keeps host uid 1000 and the device groups' ids (it can `setuid(1000)` to the host
user inside the guest), it still shares the host's network namespace and abstract sockets, and kernel bugs still
escape. `void/spike/m9_check.py --userns [--regress]` runs the whole check set with it on; the results per distro are
in `void/spike/RESULTS.md` ("M10 and release readiness").

## Troubleshooting

```
sudo spaces-void doctor        # PASS/WARN/FAIL: lxc, apparmor and the profile, cgroup layout, elogind, polkit,
                               # runit service sanity per space, autostart link, config.json, nvidia sync
spaces-void gc -n              # orphaned services (a space whose directory was removed by hand); drop -n to remove them
sudo tail -f /var/log/spaces/NAME/current   # launcher and guest start log of one space (svlogd)
sudo sv status /var/service/spaces-NAME
```

* Space will not start: `sudo sv once /var/service/spaces-NAME` and read `/var/log/spaces/NAME/current`;
  `lxc-info -P /run/spaces/lxc -n NAME` (through `/usr/lib/spaces/spaces-lxc`) shows LXC's view.
* AppArmor denials: `dmesg | grep DENIED`; `sudo apparmor_parser -r /etc/apparmor.d/lxc-spaces-container`
  reloads the profile (the launcher also loads it on demand).
* Guest sees no GPU after an NVIDIA update: `sudo spaces-void sync-config`, restart the space. After a driver
  update the kernel module and the userspace differ until reboot (doctor warns).
* **kill -9 of the launcher / hard crash**: the container may keep running orphaned (the host system-bus broker
  dies with its launcher; the next start also reaps a stale container, session broker and broker of an older
  launcher). `sudo sv down /var/service/spaces-NAME`; if it refuses: `sudo /usr/lib/spaces/spaces-lxc lxc-stop -P /run/spaces/lxc -n NAME -k`
  then remove `/sys/fs/cgroup/spaces/NAME` (`rmdir` from the leaves) and unpin the monitor's namespace
  (`sudo umount /run/spaces/lxc/NAME/netns`). The next `sv once` cleans the rest, including a stale pin.
* **cgroup base**: LXC needs `cgroup.subtree_control` of `/sys/fs/cgroup` empty and cgroup2 mounted there;
  elogind's v1 `name=elogind` mount is tolerated (hidden by the wrapper). If `lxc-start` fails with
  `Failed to set "devices.deny"`, something ran `lxc-*` without `/usr/lib/spaces/spaces-lxc`.
* Fedora bootstrap offline or slow: the `dnf5` shim retries (5), times out (30 s), downloads 8 packages in
  parallel and tries the fastest mirror; options you pass with `--setopt` win.

## Limits and known gaps

* The packages are built locally; `void/tools/release.sh repo` can sign a repository with a key of yours, but none is
  published, and a `v0.0.1` tag is prepared by `void/tools/release.sh` but pushed by hand. CI
  (`.github/workflows/void.yaml`) runs the unit tests and builds the packages for x86_64 and, cross-built only,
  aarch64 (`void/docs/release.md`). x86_64 glibc is the supported platform; musl is intentionally unsupported (the
  helpers that run in the guests are pinned to glibc 2.17).
* Root in a space is host root through the new mount API (`void/docs/apparmor-review.md`, finding 1) unless the
  space uses the opt-in user namespace ("User namespace" above). Seccomp cannot take the API away from systemd.
* The per-space runit services have no `check` or `finish` script, and the tests that need a terminal
  cannot run in the build chroot (the Ctrl-C test is deselected).
* Python upgrades: the package pins `python3>=3.14<3.15`; a Python bump needs a new revision.
* A space that was running when the package is upgraded keeps the old AppArmor profile and old Python code until
  it is restarted.
* Autostart's reaction to a new login is unit-tested with a fake elogind; the real test covers a service
  start while a session is active (no second login is possible from a single session).
* The Arch `/usr/lib32` NVIDIA overlay is only created when the guest's `pacman.conf` enables `[multilib]`.
* `desktop_flavor` changes only what is installed at creation (or by `install-flavor`); it does not remove
  anything. Installing `xdg-desktop-portal-gtk` next to the KDE backend leaves backend choice to
  xdg-desktop-portal (`Spaces:niri` is not a known desktop, so both are candidates).
* Idle shutdown, per-user lingering mounts and the TUI (textual) first-run flow are as upstream or untested.
* Mirrors: Ubuntu keyring, the Arch keyring and the Fedora image are verified by pinned checksums and
  signatures (see `void/spike/RESULTS.md`, M6).

## Maintaining the port

* `void/tools/xbps-build.sh` builds the packages, `void/tools/release.sh` checks, tags and pins checksums for a
  release (`void/docs/release.md`), `void/tools/rebase-check.sh` shows what upstream changed and whether
  `void` still merges or rebases cleanly with passing tests.
* `python3 -m pytest` tests the checkout's sources, not the installed package (`conftest.py` puts `src` first).
* The checks that need the real machine are `void/spike/m2_check.py` to `m9_check.py`; the results are in
  `void/spike/RESULTS.md`. `m9_check.py --regress` also runs `m5_check.py` and `m8_check.py`.
