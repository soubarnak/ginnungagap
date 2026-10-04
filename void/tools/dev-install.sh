#!/bin/bash
# Development install of Spaces on Void Linux (runit + LXC backend).
#
# Usage: sudo void/tools/dev-install.sh [--skip-keyring] [--skip-packages]
#                                       [--skip-distro-tools]
#
# Idempotent. Every file is COPIED to its system location: nothing installed
# here refers back to this (user-writable) checkout, so root never executes
# code that a normal user can edit. Re-run it after changing the sources.
# Revert with void/tools/dev-uninstall.sh. See void/spike/INSTALLED.md.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "dev-install.sh must run as root (use sudo)" >&2
    exit 1
fi

SKIP_KEYRING=0
SKIP_PACKAGES=0
for argument in "$@"; do
    case "$argument" in
        --skip-keyring) SKIP_KEYRING=1 ;;
        --skip-packages) SKIP_PACKAGES=1 ;;
        --skip-distro-tools) SKIP_DISTRO_TOOLS=1 ;;
        *) echo "unknown option: $argument" >&2; exit 2 ;;
    esac
done

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
STATE_DIR=/etc/spaces
STATE=$STATE_DIR/dev-install.state
PYTHON=/usr/bin/python3
BIN_DIR=/usr/bin
LIBEXEC=/usr/lib/spaces
SHARE=/usr/share/spaces
KEYRING=/usr/share/keyrings/ubuntu-archive-keyring.gpg
KEYRING_FPR=F6ECB3762474EDA9D21B7022871920D1991BC93C
KEYRING_POOL=http://archive.ubuntu.com/ubuntu/pool/main/u/ubuntu-keyring

WORK=$(mktemp -d /tmp/spaces-dev-install.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
chmod 700 "$WORK"

log() { printf '==> %s\n' "$*"; }

# The state file records what this script added to the machine, so that the
# uninstaller only removes things that were not there before.
declare -A STATE_KV
if [ -f "$STATE" ]; then
    while IFS='=' read -r key value; do
        [ -n "$key" ] && STATE_KV[$key]=$value
    done <"$STATE"
fi
save_state() {
    install -d -m 0755 "$STATE_DIR"
    : >"$STATE.new"
    for key in "${!STATE_KV[@]}"; do
        printf '%s=%s\n' "$key" "${STATE_KV[$key]}" >>"$STATE.new"
    done
    chmod 0644 "$STATE.new"
    mv -f "$STATE.new" "$STATE"
}

# ---------------------------------------------------------------- packages
build_pkgs=(gcc make pkg-config glib-devel pam-devel)
runtime_pkgs=(python3 python3-Pillow python3-rich python3-textual polkit lxc
    debootstrap apparmor gnupg curl runit elogind xdg-dbus-proxy
    librsvg-utils dconf)
if [ "$SKIP_PACKAGES" -eq 0 ]; then
    missing=()
    for package in "${build_pkgs[@]}" "${runtime_pkgs[@]}"; do
        xbps-query "$package" >/dev/null 2>&1 || missing+=("$package")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        log "installing packages: ${missing[*]}"
        xbps-install -y "${missing[@]}"
        STATE_KV[packages]="${STATE_KV[packages]:-} ${missing[*]}"
        STATE_KV[packages]=$(echo "${STATE_KV[packages]}" | xargs -n1 | sort -u | xargs)
    fi
fi

# ------------------------------------------------------------ native build
# Build in a root-owned scratch copy with the Makefile's own flags. A CFLAGS
# in the environment would replace -fPIC and the other defaults, so drop them.
log "building native binaries"
cp -r "$ROOT/native" "$WORK/native"
rm -rf "$WORK/native/__pycache__"
env -u CFLAGS -u CPPFLAGS -u LDFLAGS -u CC -u PREFIX \
    make -C "$WORK/native" clean >/dev/null
env -u CFLAGS -u CPPFLAGS -u LDFLAGS -u CC -u PREFIX \
    make -C "$WORK/native" DESTDIR="$WORK/stage" LIBEXECDIR="$LIBEXEC" install \
    >"$WORK/make.log" 2>&1 || { cat "$WORK/make.log" >&2; exit 1; }
install -d -m 0755 "$LIBEXEC" "$LIBEXEC/guest" "$LIBEXEC/void/bin"
for file in spaces-pam spaces-broker spaces-system-broker; do
    install -m 0755 -o root -g root "$WORK/stage$LIBEXEC/$file" "$LIBEXEC/$file"
done
for file in pam_spaces.so spaces spaces-portal spaces-secret-helper \
    spaces-open spaces-system-broker; do
    install -m 0755 -o root -g root "$WORK/stage$LIBEXEC/guest/$file" \
        "$LIBEXEC/guest/$file"
done

# ----------------------------------------------------------- python package
SITE=$("$PYTHON" -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
log "installing python package to $SITE/spaces"
rm -rf "$SITE/spaces"
cp -r "$ROOT/src/spaces" "$SITE/spaces"
find "$SITE/spaces" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$SITE/spaces" -type d -exec chmod 0755 {} +
find "$SITE/spaces" -type f -exec chmod 0644 {} +
chown -R root:root "$SITE/spaces"
"$PYTHON" -I -m compileall -q "$SITE/spaces"

cat >"$BIN_DIR/spaces" <<'PYEOF'
#!/usr/bin/python3
import sys

from spaces.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
PYEOF
chmod 0755 "$BIN_DIR/spaces"

# spaces.priv runs as root through pkexec, sudo and runit. Isolated mode keeps
# PYTHON* variables, the working directory and user site-packages out of
# sys.path. The shim directory comes first on PATH so that later milestones can
# provide pacstrap/dnf5 wrappers.
cat >"$BIN_DIR/spaces.priv" <<'SHEOF'
#!/bin/sh
PATH=/usr/lib/spaces/void/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
export PATH
exec /usr/bin/python3 -I -c 'import sys; from spaces.priv import main; sys.exit(main())' "$@"
SHEOF
chown root:root "$BIN_DIR/spaces" "$BIN_DIR/spaces.priv"
chmod 0755 "$BIN_DIR/spaces.priv"

# Run by the user from the graphical session (niri spawn-at-startup) to publish
# the session environment for the root-side launcher; see spaces.host.session_env.
cat >"$BIN_DIR/spaces-session-env" <<'SHEOF'
#!/bin/sh
exec /usr/bin/python3 -I -m spaces.host.session_env "$@"
SHEOF
chown root:root "$BIN_DIR/spaces-session-env"
chmod 0755 "$BIN_DIR/spaces-session-env"

# -------------------------------------------------------------- data files
log "installing data files"
install -d -m 0755 "$SHARE"
for directory in pam keys repos systemd portal system-bridge; do
    rm -rf "${SHARE:?}/$directory"
done
install -d -m 0755 "$SHARE/pam" "$SHARE/keys" "$SHARE/repos" "$SHARE/systemd"
install -m 0644 "$ROOT"/data/pam/* "$SHARE/pam/"
install -m 0644 "$ROOT"/data/keys/* "$SHARE/keys/"
install -m 0644 "$ROOT"/data/repos/* "$SHARE/repos/"
cp -r "$ROOT/data/portal" "$SHARE/portal"
cp -r "$ROOT/data/system-bridge" "$SHARE/system-bridge"
find "$SHARE/portal" "$SHARE/system-bridge" -type d -exec chmod 0755 {} +
find "$SHARE/portal" "$SHARE/system-bridge" -type f -exec chmod 0644 {} +
chown -R root:root "$SHARE"
# These two units are bind-mounted into the guest. The upstream mount unit
# is conditional on systemd-nspawn; an LXC guest reports "lxc", which the
# generic "container" condition matches.
sed 's/^ConditionVirtualization=.*/ConditionVirtualization=container/' \
    "$ROOT/data/run-spaces-proc.mount" >"$SHARE/systemd/run-spaces-proc.mount"
install -m 0644 "$ROOT/data/local-fs-spaces-proc.conf" \
    "$SHARE/systemd/local-fs-spaces-proc.conf"
chmod 0644 "$SHARE/systemd/run-spaces-proc.mount"
grep -q '^ConditionVirtualization=container$' "$SHARE/systemd/run-spaces-proc.mount"

install -Dm644 "$ROOT/data/org.anatase.spaces.policy" \
    /usr/share/polkit-1/actions/org.anatase.spaces.policy
# The policy binds to this exact path; it must be the root-owned wrapper.
grep -q '"org.freedesktop.policykit.exec.path">/usr/bin/spaces.priv<' \
    /usr/share/polkit-1/actions/org.anatase.spaces.policy
install -Dm644 "$ROOT/data/pam/spaces.system-auth" /etc/pam.d/spaces

# ------------------------------------------------------------ void specifics
install -Dm755 "$ROOT/void/bin/spaces-lxc" "$LIBEXEC/spaces-lxc"
install -Dm644 "$ROOT/void/apparmor/spaces-container" \
    /etc/apparmor.d/spaces-container
if command -v apparmor_parser >/dev/null && [ -d /sys/kernel/security/apparmor ]; then
    apparmor_parser -r /etc/apparmor.d/spaces-container 2>/dev/null ||
        echo "warning: apparmor_parser could not load spaces-container" >&2
fi

install -d -m 0755 "$STATE_DIR" /var/lib/spaces /var/cache/spaces /var/log/spaces
chown root:root /var/lib/spaces /var/cache/spaces /var/log/spaces
# /etc/spaces/config.json is generated by spaces-nvidia-sync from the shipped
# base configuration and the optional /etc/spaces/void.json. A config.json
# that was edited by hand is never replaced (config.json.new is written).
install -Dm644 "$ROOT/void/data/config.base.json" "$SHARE/config.base.json"
cat >"$LIBEXEC/spaces-nvidia-sync" <<'SHEOF'
#!/bin/sh
exec /usr/bin/python3 -I -m spaces.host.nvidia "$@"
SHEOF
chown root:root "$LIBEXEC/spaces-nvidia-sync"
chmod 0755 "$LIBEXEC/spaces-nvidia-sync"
config_existed=0
[ -e /etc/spaces/config.json ] && config_existed=1
log "generating /etc/spaces/config.json and the NVIDIA userspace farm"
"$LIBEXEC/spaces-nvidia-sync" || echo "warning: spaces-nvidia-sync failed" >&2
if [ "$config_existed" -eq 0 ] && [ -e /etc/spaces/config.json ]; then
    STATE_KV[config]=created
fi
"$PYTHON" -I -c '
import logging
from spaces import host_config

records = []
class Collect(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())
logging.getLogger().addHandler(Collect())
config = host_config.load()
assert config.version == 1 and not records, ("config.json rejected", records)
print("config ok:", {k: v.packages for k, v in config.distros.items()})
'

# ------------------------------------------------------------ ubuntu keyring
fingerprints() { GNUPGHOME=$WORK/gnupg gpg --batch --show-keys --with-colons "$1" 2>/dev/null; }
has_fingerprint() { fingerprints "$1" | grep -q "^fpr:::::::::${KEYRING_FPR}:"; }
show_fingerprints() {
    fingerprints "$1" | awk -F: '$1=="fpr"{print "    fpr " $10} $1=="uid"{print "    uid " $10}'
}
if [ "$SKIP_KEYRING" -eq 0 ]; then
    mkdir -p "$WORK/gnupg" && chmod 700 "$WORK/gnupg"
    if [ -f "$KEYRING" ] && has_fingerprint "$KEYRING"; then
        log "ubuntu archive keyring already installed and verified"
        show_fingerprints "$KEYRING"
    else
        log "fetching ubuntu-keyring source from $KEYRING_POOL"
        listing=$(curl -fsS --retry 3 "$KEYRING_POOL/")
        if [ -n "${UBUNTU_KEYRING_VERSION:-}" ]; then
            tarball="ubuntu-keyring_${UBUNTU_KEYRING_VERSION}.tar.xz"
        else
            # ubuntu-keyring is a native package: there is no *.orig.tar.gz.
            tarball=$(printf '%s' "$listing" |
                grep -o 'ubuntu-keyring_[0-9][0-9.]*\(build[0-9]*\)\?\.tar\.\(xz\|gz\)' |
                sort -uV | tail -n 1)
        fi
        [ -n "$tarball" ] || { echo "no ubuntu-keyring tarball found" >&2; exit 1; }
        dsc="${tarball%.tar.*}.dsc"
        curl -fsS --retry 3 -o "$WORK/$tarball" "$KEYRING_POOL/$tarball"
        curl -fsS --retry 3 -o "$WORK/$dsc" "$KEYRING_POOL/$dsc"
        expected=$(awk -v f="$tarball" '/^Checksums-Sha256:/{s=1;next} /^[A-Za-z-]+:/{s=0} s&&$3==f{print $1}' "$WORK/$dsc")
        actual=$(sha256sum "$WORK/$tarball" | cut -d' ' -f1)
        if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
            echo "checksum mismatch for $tarball (dsc: $expected, got: $actual)" >&2
            exit 1
        fi
        mkdir "$WORK/keyring-src"
        tar -xf "$WORK/$tarball" -C "$WORK/keyring-src"
        candidate=$(find "$WORK/keyring-src" -name ubuntu-archive-keyring.gpg -path '*/keyring*' | head -n 1)
        [ -n "$candidate" ] || { echo "ubuntu-archive-keyring.gpg not in $tarball" >&2; exit 1; }
        log "fingerprints in $tarball:"
        show_fingerprints "$candidate"
        if ! has_fingerprint "$candidate"; then
            echo "REFUSING to install: signing key $KEYRING_FPR is absent" >&2
            exit 1
        fi
        install -d -m 0755 /usr/share/keyrings
        if [ -e "$KEYRING" ]; then
            cp -a "$KEYRING" "$KEYRING.dev-install-backup"
            STATE_KV[keyring]=replaced
        else
            STATE_KV[keyring]=installed
        fi
        install -m 0644 -o root -g root "$candidate" "$KEYRING"
        log "installed $KEYRING (from $tarball, sha256 $actual)"
    fi
fi

# ------------------------------------------------------ arch/fedora host tools
SKIP_DISTRO_TOOLS=${SKIP_DISTRO_TOOLS:-0}
if [ "$SKIP_DISTRO_TOOLS" -eq 0 ]; then
    # shellcheck source=dev-install-arch.sh
    . "$ROOT/void/tools/dev-install-arch.sh"
    install_arch_host
    # shellcheck source=dev-install-fedora.sh
    . "$ROOT/void/tools/dev-install-fedora.sh"
    install_fedora_host
fi

save_state
if [ -d /home ]; then
    log "done. Try: sudo spaces create ubuntu --preset basic ; spaces enter ubuntu"
fi
