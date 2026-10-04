# Spaces shell integration (bash, zsh, other POSIX shells). OPT-IN.
#
# Not enabled by default. Either source it from your own rc file:
#     [ -r /usr/share/spaces/void/shell/spaces.sh ] && . /usr/share/spaces/void/shell/spaces.sh
# or enable it for every bash user:
#     sudo ln -s /usr/share/spaces/void/shell/spaces.sh /etc/bash/bashrc.d/spaces.sh
#
# It defines:
#   arch, ubuntu, fedora, kali    run a command in that space
#                                 (`arch pacman -Q`, `ubuntu` alone opens a shell).
#                                 /usr/bin/arch belongs to coreutils (prints the
#                                 machine type), so the Arch space is only
#                                 reachable as this function or as `arch-linux`.
#   apt, apt-get, dnf             only when the host has no such command: a hint
#                                 that points at the space. Nothing is overridden
#                                 on the host: pacman and xbps-* are left alone.
# Set SPACES_NO_HINTS=1 to skip the hints.

if command -v spaces >/dev/null 2>&1; then
    arch() { [ "${1:-}" = -- ] && shift; spaces enter arch -- "$@"; }
    ubuntu() { [ "${1:-}" = -- ] && shift; spaces enter ubuntu -- "$@"; }
    fedora() { [ "${1:-}" = -- ] && shift; spaces enter fedora -- "$@"; }
    kali() { [ "${1:-}" = -- ] && shift; spaces enter kali -- "$@"; }

    if [ -z "${SPACES_NO_HINTS:-}" ]; then
        _spaces_hint() {
            printf '%s: not installed on this Void host. Run it in the %s space instead: `%s %s ...`\n' \
                "$1" "$2" "$2" "$1" >&2
            return 127
        }
        command -v apt >/dev/null 2>&1 || apt() { _spaces_hint apt ubuntu; }
        command -v apt-get >/dev/null 2>&1 || apt-get() { _spaces_hint apt-get ubuntu; }
        command -v dnf >/dev/null 2>&1 || dnf() { _spaces_hint dnf fedora; }
    fi
fi
