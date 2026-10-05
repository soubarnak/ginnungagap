<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="art/ginnungagap-wordmark-ondark.svg">
    <source media="(prefers-color-scheme: light)" srcset="art/ginnungagap-wordmark-onwhite.svg">
    <img alt="ginnungagap: out of the void, worlds." src="art/ginnungagap-wordmark-onwhite.svg" width="750">
  </picture>
</p>

<p align="center">
<strong>Spaces for Void Linux</strong>: whole Ubuntu, Arch, Fedora and Kali systems, born from an empty host.</p>

<p align="center">
  <a href="https://github.com/soubarnak/ginnungagap/tags"><img alt="release" src="https://img.shields.io/github/v/tag/soubarnak/ginnungagap?label=release&color=478061&labelColor=1f2328"></a>
  <a href="LICENSE"><img alt="license AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0--or--later-478061?labelColor=1f2328"></a>
  <img alt="Void Linux" src="https://img.shields.io/badge/host-Void%20Linux-478061?logo=voidlinux&logoColor=white&labelColor=1f2328">
  <img alt="runit and LXC" src="https://img.shields.io/badge/runit-LXC-478061?labelColor=1f2328">
  <img alt="AppArmor" src="https://img.shields.io/badge/AppArmor-enforced-478061?labelColor=1f2328">
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#why-the-name">Why the name</a> ·
  <a href="#why-void-needs-this">Why Void</a> ·
  <a href="#what-changed-from-upstream">What changed</a> ·
  <a href="#security-in-plain-words">Security</a> ·
  <a href="#credits">Credits</a>
</p>

---

## Why the name

In Norse myth, **Ginnungagap** is the yawning void that existed before anything else: the empty gap between the
realm of ice and the realm of fire. Everything that exists, the worlds included, took shape out of that emptiness.

This project is built on the same idea. **Void Linux** is a deliberately small, empty-handed base: runit, a
minimal userland, no more than you ask for. **Spaces** are the worlds you raise inside it: complete Ubuntu, Arch,
Fedora and Kali systems, each with its own packages and its own services, started from nothing but the host's
kernel and a package repository. The emptiness is the point. A host with nothing extra on it can hold any world you
need, and none of those worlds crowds the others or the host. Hence the name: Ginnungagap is the void, and Spaces
come out of it.

## Why Void needs this

Void is lean, fast and rolling, and many people run it because they want control over what is installed. The cost
is that the toolchains developers reach for first, such as `apt` with Ubuntu packages, `pacman` and the AUR, Fedora's
`dnf`, or Kali's security tools, are not in Void's repositories, and wiring them in by hand makes a mess of a
carefully kept system.

Spaces removes that trade-off. You keep Void as a clean base and open a Space whenever a project wants another
distribution:

* develop against the exact Ubuntu or Fedora your servers run, with the real package manager and the real libraries;
* use Arch's rolling toolchains or Kali's tools without installing any of it on the host;
* run Docker or other services in a Space with their own init, while the host stays as small as it was;
* get desktop integration anyway: GUI applications from a Space show up on your taskbar, the clipboard, audio, GPU
  (NVIDIA and AMD, including PRIME offload), portals and theming are forwarded, and `sudo` in a Space uses the host's PAM.

Upstream Spaces is built on `systemd-nspawn`, `machinectl` and SELinux, none of which Void ships. ginnungagap is the
port that makes it work there, so Void can be a development machine without giving up what makes it Void.

## Install

**You do not need to build anything from source.** Every release ships ready-made, signed `.xbps` packages, and xbps
installs them like any other package: add the release as a repository and run `xbps-install`. Building with Void's own
[`xbps-src`](https://github.com/void-linux/void-packages) is the other way, for when you want to build from a branch or
change something. Both need Void Linux on x86_64 (glibc).

### From the signed repository (no build)

Each release carries a signed xbps repository as its assets, and the URL below always points at the newest
release, so `sudo xbps-install -Su` keeps ginnungagap up to date:

```bash
echo "repository=https://github.com/soubarnak/ginnungagap/releases/latest/download" | sudo tee /etc/xbps.d/20-spaces.conf
sudo xbps-install -S spaces
```

To stay on one release, use `https://github.com/soubarnak/ginnungagap/releases/download/v0.0.3` (or another tag) instead.

On the first sync xbps shows the signer and the key fingerprint and asks whether to trust it. Compare it with this one
before you answer `y`:

```
Spaces Void port <soubarnakarmakar@gmail.com>
f4:55:72:f9:ac:23:eb:b3:c3:e3:f8:b3:a9:24:97:39
```

Packages for aarch64 are in the same repository. They are cross-built and have never been run.

### Build it yourself

You build the packages from this repository and install them from the local repository xbps-src produces. It needs no
root until the last step.

**You need** Void Linux on x86_64 (glibc), `git`, `bubblewrap`, about 2 GB of free space, and your user in the
`xbuilder` group (xbps-src builds in an unprivileged user namespace):

```bash
sudo xbps-install -S git bubblewrap
sudo usermod -aG xbuilder "$USER"       # then log out and in again
```

**Build and install:**

```bash
git clone https://github.com/soubarnak/ginnungagap && cd ginnungagap    # the default branch, void, is the port
void/tools/xbps-build.sh                # clones void-packages, bootstraps it, builds the five packages
sudo xbps-install -S -R ~/.local/share/ginnungagap/void-packages/hostdir/binpkgs spaces
```

To check out the released tag instead of the latest `void`, run `git checkout v0.0.3` before the build. Do not add
`--release` to the build: that mode builds the committed template, which names GitHub's tarball and pins its
checksum in a commit made after the tag.

**Keep the repository for upgrades** (a local repository needs no signature):

```bash
echo "repository=$HOME/.local/share/ginnungagap/void-packages/hostdir/binpkgs" | sudo tee /etc/xbps.d/20-spaces-local.conf
```

After a new build, `sudo xbps-install -u spaces` upgrades. Running spaces keep running. `sudo xbps-remove spaces` removes
the program and stops the spaces; their data in `/var/lib/spaces` stays.

### Create and enter a space

With either install:

```bash
sudo spaces create ubuntu --preset basic    # or fedora, arch, kali
ubuntu -- id                                # or: spaces enter ubuntu
spaces-void doctor                          # checks the host if anything looks wrong
```

**Read [void/docs/void.md](void/docs/void.md)** for what gets installed where, entry commands, autostart
(`sudo ln -s /etc/sv/spaces-autostart /var/service/`), the desktop flavour, the security model compared to upstream,
troubleshooting (`spaces-void doctor`) and known gaps. Milestone logs: `void/spike/RESULTS.md` and
`void/spike/INSTALLED.md`. Releasing: [void/docs/release.md](void/docs/release.md).

## What changed from upstream

| | Upstream Spaces | ginnungagap (`void` branch) |
|---|---|---|
| Host init and services | systemd units (`spaces@NAME`) | runit services |
| Logins and sessions | systemd-logind | elogind |
| Container engine | `systemd-nspawn`, `machinectl` | LXC |
| Mandatory access control | SELinux | AppArmor |
| Packaging | RPM and Arch packages | xbps packages built with xbps-src |
| Guests | Arch, Fedora, Kali, Ubuntu | the same four, still booting their own systemd |

Only the host side is replaced. The guests are unchanged. The `master` branch tracks upstream untouched, so upstream
changes stay easy to rebase onto (`void/tools/rebase-check.sh`).

## Security in plain words

Be clear about what a Space is. Root inside a Space that has no user namespace is the host's root, and the new Linux
mount API cannot be mediated by AppArmor, so such a Space is a convenience boundary, not a hardened sandbox against a
hostile root. Every Space you create gets a user namespace by default, so root in it is an unprivileged user on the
host. The cost is NFS in the guest, privileged ports, raw sockets and interface changes. `spaces create --no-userns`
refuses it. A Space made before this default existed keeps host root until you run
`sudo spaces-void userns enable NAME` on it.
The details and the evidence are in [void/docs/apparmor-review.md](void/docs/apparmor-review.md).

## Credits

**The logo.** The mark keeps the composition of the Spaces logo, overlapping rounded squares, and reads it as the myth
the project is named after: Niflheim's ice (upper right, with Isa, the rune of ice) and Muspelheim's fire (lower left,
with Kenaz, the rune of the torch) meet across the gap, and from it comes the B of the Bongbetic
brand, tinted from ice to flame. The B glyph is Bongbetic's; the overlapping-squares idea is from the Spaces logo by the
Anatase project. The mark and wordmark are in `art/`.

ginnungagap would not exist without the people who made Spaces.

* **[Antheas Kapenekakis](https://github.com/antheas)**, the original author of
  **[Spaces](https://github.com/anatase-org/spaces)**. The design, the permission model, the desktop integration and
  nearly every line this port stands on are theirs. Copyright (C) 2026 Antheas Kapenekakis, see `COPYRIGHT`.
* **[Anatase Linux](https://anatase.org)** and the [anatase-org](https://github.com/anatase-org) project: "a modern
  immutable distribution for development and play", which publishes Spaces as part of its system and released it
  under the AGPL so that others can build on it.

Both are the reason this exists. If you find ginnungagap useful, star and support the
[upstream project](https://github.com/anatase-org/spaces) too. Security issues in the original code go to the upstream
address in its own notes below; issues specific to the Void port belong in this repository's issue tracker.

> **Fork notice (AGPL-3.0 section 5a).** ginnungagap is a modified version of
> [anatase-org/spaces](https://github.com/anatase-org/spaces), changed on 2026-10-04 and later, to run on Void Linux
> (runit, elogind, LXC, AppArmor) instead of systemd and SELinux. It is not affiliated with or endorsed by the upstream
> authors. Upstream copyright: Copyright (C) 2026 Antheas Kapenekakis, see `COPYRIGHT` and `LICENSE`.
>
> **Status: v0.0.3.** The Void port works end to end and is tested on four guests; it is an early release. Signed
> packages are on the GitHub release (see Install), or build them from this checkout with `void/tools/xbps-build.sh`.
> x86_64 glibc is the supported platform.

---

The text below is the original upstream README. It describes the systemd-based original: where it mentions
`systemctl`, `spaces@NAME` units, SELinux or `machinectl`, read the Void equivalents in
[void/docs/void.md](void/docs/void.md).

## Spaces, the original
Spaces provide a chroot-like sandboxing environment for you to access your favorite distributions: Arch, Fedora, Kali, and Ubuntu. A simple permission system ensures your local files and credentials remain secure, even if your space is compromised. Spaces are constructed directly using packages from your chosen distribution repositories with signature enforcement. No container middleman or surprises.

### About

Spaces is a "simple" wrapper around `systemd-nspawn` that makes it easier to use and provides host integration with a couple of intuitive permissions, dbus/theming integration, and PAM.

To use, type in your terminal:

```bash
spaces enter ubuntu # or fedora, arch, or kali
```

And follow the graphical prompts. After the space initializes, you will face a familiar terminal. Except, this time, it is a real Ubuntu system you can do anything you want in. Install packages, add custom services, use docker, install vs code, your dev toolchains, browsers, etc.

Everything works as it would on a normal system. If you install desktop packages, they appear on your taskbar. If you share your screen using Chrome, it works. The only overhead is 75mb of memory and 6 seconds of booting.

By default, after creating a space or configuring it for your user, the `spaces@<your-space>` user service is enabled, ensuring that the space is started when you log in over ssh or in a graphical session. This is because the 6 seconds of startup would lead to an unacceptable user experience for novice users.

You may disable this service with:
```bash
systemctl --user disable --now spaces@<your-space>
```

This service essentially starts the system `spaces@<your-space>` service. If you want your space to run on boot, run:
```bash
sudo systemctl enable --now spaces@<your-space>
```

And it will start on boot. You may stop that service to poweroff your space. Once started, such as by launching applications, the service or entering a space, the space does not power down until you shutdown your computer/server. Idle detection may be added in the future.

However, if your space relies on system services accessing your user data, this will not work, as your user and its files will not be mounted on boot.  This is done partly for security reasons, and partly to avoid edge cases with solutions such as systemd-homed.


Instead, you can enable lingering for your user, which starts the user manager at
boot without requiring an interactive login. This way, the service starts with important paths already mounted.
```bash
loginctl enable-linger
```

### Security
For security issues, email: security@anatase.org

Spaces is a small daemon that escalates using polkits. The root inside spaces is powerful depending on granted permissions, and requires the same excalation pattern as the host. This means: to get root in a space, the same authentication that would be performed on the host is required and unpriviledged user processes need a normal bypass. One action is allowed without authentication: a configured user chooses to enter a space as themselves. During this escalation, the service `spaces@<space>` is also started if needed. This is ok, because the permissions and users that have been granted to a space previously used authentication, and all rootful applications added to that space also used PAM authentication for e.g., sudo. Therefore, just turning on a space does not give unpriviledged code an escalation path, regardless of whether that code is inside or outside the space.

Spaces only mounts users that have executed `spaces configure --user <space>` or were the ones to create the space and only after they log in. The root inside spaces is the same root as the host. This was chosen because user namespaces cannot do certain actions such as modify the host network (would need a bridge and would not be able to open ports) or change kernel settings. Even though the default priviledge level of spaces disallows these actions, a key requirement in design was making spaces able to change permissions without recreating them. However, this also means that spaces can be victim to a class of security issues such as improper mounts, kernel bugs, or other sandboxing holes that can cause the root inside the space to escape.

It is not possible to mount SSH or GPG directories from the host into the space, other than `~/.ssh/config` and that is additionally enforced by SELinux.

#### SELinux

The `spaces-selinux` policy package provides the selinux rules separately to enable moving between images with and without Spaces. Spaces relabel `/var/lib/spaces`, `~/.ssh/config` so users moving between those images would have to relabel those files otherwise. It also allows to runtime replace spaces for development without rebuilding SELinux policy and breaking SELinux on updates (see `./sync.sh`).

```bash
sudo restorecon -RF /var/lib/spaces
restorecon -F ~/.ssh/config
```

### Contributing

Spaces does not currently accept external contributions. You are welcome to post issues in the issue tracker, with suggestions or bug reports.

### License

A copy of Spaces is provided to you under the terms of [GNU Affero General Public License v3.0 or later](LICENSE).

The files under `./art/distros` and `./src/spaces/overlay` are used as identifiers to their respective distributions with no implication of association or affiliation to their respective communities or companies.
