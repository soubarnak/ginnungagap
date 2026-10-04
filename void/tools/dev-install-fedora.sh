#!/bin/bash
# Fedora host tooling for the Void dev install. Sourced by dev-install.sh.
#
# Installs /usr/lib/spaces/void/bin/dnf5, a shim that runs the real dnf5 inside
# a bootstrap Fedora root (void/bin/dnf5 documents the design). Nothing is
# downloaded here: on first use the shim fetches the Fedora 44 Container Base
# image into /var/lib/spaces/.host/fedora/44/ and verifies it against the
# signed CHECKSUM file (key: /usr/share/spaces/keys/RPM-GPG-KEY-fedora-44-primary).
# `sudo /usr/lib/spaces/void/bin/dnf5 --version` warms that cache.

install_fedora_host() {
    install -d -m 0755 "$LIBEXEC/void/bin"
    install -m 0755 -o root -g root "$ROOT/void/bin/dnf5" "$LIBEXEC/void/bin/dnf5"
    for tool in gpg gpgv tar unshare chroot; do
        command -v "$tool" >/dev/null || echo "warning: $tool is missing (needed by the dnf5 shim)" >&2
    done
}
