#!/bin/bash
# Arch host tooling for the Void dev install. Sourced by dev-install.sh (uses
# its log, ROOT, WORK, LIBEXEC, SHARE and STATE_KV); not run on its own.
#
# Installs, all below paths that no Void package owns:
#   /usr/lib/spaces/void/arch-install-scripts/bin/{pacstrap,arch-chroot,genfstab}
#   /usr/lib/spaces/void/bin/{pacstrap,arch-chroot}    shims (void/bin/*)
#   /usr/lib/spaces/rankmirrors                        (arch.py's RANKMIRRORS)
#   /usr/share/spaces/void/arch-pacman.conf            core + extra, own GPGDir
#   /usr/share/spaces/void/archlinux-keyring/          archlinux.gpg, -trusted, -revoked
#   /var/lib/spaces/.host/arch/gnupg                   pacman keyring (root, 0700)
#   /etc/pacman.d/mirrorlist                           only when absent
# plus the xbps packages `pacman` (pacman, pacman-key) and `m4` (build).
# Pinned sources; the sha256 values were computed from downloads that were
# verified by signature (see void/spike/RESULTS.md, M6).

AIS_VERSION=31
AIS_URL=https://gitlab.archlinux.org/archlinux/arch-install-scripts/-/archive/v31/arch-install-scripts-v31.tar.gz
# Identical (diff -r) to `git archive v31`; tag v31 is signed by
# C100346676634E80C940FB9E9C02FF419FECBE16 (Morten Linderud).
AIS_SHA256=ef22eae93b5cc78c7e7982acc160428cced9f96cb95090aeb77d55bc844a988e

KEYRING_VERSION=20260909
KEYRING_URL=https://gitlab.archlinux.org/archlinux/archlinux-keyring/-/releases/20260909/downloads/archlinux-keyring-20260909.tar.gz
# The release's inline signature (.tar.gz.sig) verifies with the key of
# Christian Hesse 02FD1C7A934E614545849F19A6234074498E9CEE.
KEYRING_SHA256=935ad345a7700358367ca9a2f70220f30869492c31b24e9e4e4e89f05bf5528a

RANKMIRRORS_COMMIT=75d4a70517c649155959d28260198630fcb8fe51
RANKMIRRORS_VERSION=1.13.1
RANKMIRRORS_URL=https://gitlab.archlinux.org/pacman/pacman-contrib/-/raw/$RANKMIRRORS_COMMIT/src/rankmirrors.sh.in
RANKMIRRORS_SHA256=b67902d26a8b193cc096421c8d780f795a253d0742cd31d592254059af15cc27

VOID_LIB=$LIBEXEC/void
ARCH_SHARE=$SHARE/void
ARCH_GPG=/var/lib/spaces/.host/arch/gnupg
ARCH_MARK=$VOID_LIB/arch-install-scripts/VERSION

fetch_pinned() { # url sha256 output
    curl -fsSL --retry 3 -o "$3" "$1"
    local actual
    actual=$(sha256sum "$3" | cut -d' ' -f1)
    if [ "$actual" != "$2" ]; then
        echo "checksum mismatch for $1 (expected $2, got $actual)" >&2
        exit 1
    fi
}

ensure_xbps() {
    local missing=() package
    for package in "$@"; do
        xbps-query "$package" >/dev/null 2>&1 || missing+=("$package")
    done
    [ "${#missing[@]}" -eq 0 ] && return 0
    log "installing packages: ${missing[*]}"
    xbps-install -y "${missing[@]}"
    STATE_KV[packages]="${STATE_KV[packages]:-} ${missing[*]}"
    STATE_KV[packages]=$(echo "${STATE_KV[packages]}" | xargs -n1 | sort -u | xargs)
}

install_arch_host() {
    ensure_xbps pacman m4 util-linux gawk
    install -d -m 0755 "$VOID_LIB/bin" "$ARCH_SHARE"

    # arch-install-scripts
    if [ "$(cat "$ARCH_MARK" 2>/dev/null)" != "$AIS_VERSION" ]; then
        log "fetching arch-install-scripts $AIS_VERSION"
        fetch_pinned "$AIS_URL" "$AIS_SHA256" "$WORK/ais.tar.gz"
        mkdir "$WORK/ais"
        tar -xzf "$WORK/ais.tar.gz" -C "$WORK/ais" --strip-components=1
        make -C "$WORK/ais" arch-chroot genfstab pacstrap >"$WORK/ais.log" 2>&1 ||
            { cat "$WORK/ais.log" >&2; exit 1; }
        for script in arch-chroot genfstab pacstrap; do
            bash -O extglob -n "$WORK/ais/$script"
        done
        rm -rf "$VOID_LIB/arch-install-scripts"
        install -d -m 0755 "$VOID_LIB/arch-install-scripts/bin"
        install -m 0755 "$WORK/ais/arch-chroot" "$WORK/ais/genfstab" \
            "$WORK/ais/pacstrap" "$VOID_LIB/arch-install-scripts/bin/"
        install -m 0644 "$WORK/ais/COPYING" "$VOID_LIB/arch-install-scripts/COPYING"
        echo "$AIS_VERSION" >"$ARCH_MARK"
    fi
    install -m 0755 "$ROOT/void/bin/pacstrap" "$ROOT/void/bin/arch-chroot" "$VOID_LIB/bin/"
    install -m 0644 "$ROOT/void/data/arch-pacman.conf" "$ARCH_SHARE/arch-pacman.conf"

    # rankmirrors (pacman-contrib), as in spaces.spec
    if [ ! -x "$LIBEXEC/rankmirrors" ]; then
        log "fetching rankmirrors $RANKMIRRORS_VERSION"
        fetch_pinned "$RANKMIRRORS_URL" "$RANKMIRRORS_SHA256" "$WORK/rankmirrors.in"
        sed -e "s/@PACKAGE_VERSION@/$RANKMIRRORS_VERSION/g" \
            -e 's|@sysconfdir@|/etc|g' "$WORK/rankmirrors.in" >"$WORK/rankmirrors"
        bash -n "$WORK/rankmirrors"
        install -m 0755 "$WORK/rankmirrors" "$LIBEXEC/rankmirrors"
    fi

    # Arch keyring (files only; the guest installs the real package itself)
    local keyring_dir=$ARCH_SHARE/archlinux-keyring
    if [ "$(cat "$keyring_dir/VERSION" 2>/dev/null)" != "$KEYRING_VERSION" ]; then
        log "fetching archlinux-keyring $KEYRING_VERSION"
        fetch_pinned "$KEYRING_URL" "$KEYRING_SHA256" "$WORK/archlinux-keyring.tar.gz"
        mkdir "$WORK/archkr"
        tar -xzf "$WORK/archlinux-keyring.tar.gz" -C "$WORK/archkr" --strip-components=1
        # The trust anchors are the master keys in archlinux-trusted. Compare
        # them with the list archlinux.org publishes (an independent host).
        local page fingerprint cross=ok
        if page=$(curl -fsS --retry 2 https://archlinux.org/master-keys/ 2>/dev/null); then
            for fingerprint in $(cut -d: -f1 "$WORK/archkr/archlinux-trusted"); do
                printf '%s' "$page" | grep -q "$fingerprint" || cross=mismatch
            done
        else
            cross=unreachable
        fi
        [ "$cross" = mismatch ] &&
            { echo "master keys in archlinux-trusted differ from archlinux.org/master-keys" >&2; exit 1; }
        log "archlinux-trusted master keys vs archlinux.org/master-keys: $cross"
        rm -rf "$keyring_dir"
        install -d -m 0755 "$keyring_dir"
        install -m 0644 "$WORK/archkr/archlinux.gpg" "$WORK/archkr/archlinux-trusted" \
            "$WORK/archkr/archlinux-revoked" "$keyring_dir/"
        echo "$KEYRING_VERSION" >"$keyring_dir/VERSION"
        rm -rf "$ARCH_GPG"
    fi

    # Host pacman keyring: master keys signed locally, owner root, 0700.
    if [ ! -f "$ARCH_GPG/trustdb.gpg" ]; then
        log "initialising the pacman keyring in $ARCH_GPG"
        install -d -m 0755 /var/lib/spaces/.host /var/lib/spaces/.host/arch
        install -d -m 0700 -o root -g root "$ARCH_GPG"
        pacman-key --gpgdir "$ARCH_GPG" --init >"$WORK/pk.log" 2>&1 ||
            { cat "$WORK/pk.log" >&2; exit 1; }
        pacman-key --gpgdir "$ARCH_GPG" --populate-from "$keyring_dir" --populate archlinux \
            >>"$WORK/pk.log" 2>&1 || { cat "$WORK/pk.log" >&2; exit 1; }
        gpgconf --homedir "$ARCH_GPG" --kill all 2>/dev/null || true
        chmod 0700 "$ARCH_GPG"
    fi

    if [ ! -e /etc/pacman.d/mirrorlist ]; then
        install -Dm644 "$ROOT/void/data/arch-mirrorlist" /etc/pacman.d/mirrorlist
        STATE_KV[arch_mirrorlist]=installed
    fi
}
