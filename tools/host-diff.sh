#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Host write confinement check (FR-005, T10.2): the host changes only inside the Owned Paths.
#
#   tools/host-diff.sh [--root DIR] [--exclude PATH]... -- COMMAND [ARG...]
#   tools/host-diff.sh --print-exclusions
#
# Takes a snapshot of the host filesystem, runs COMMAND with its arguments as an argv (no shell),
# takes a second snapshot, and prints each entry that differs. An entry records the path, type,
# mode, owner, group, size, mtime and symlink target. A directory records its mode and owner only:
# its size and mtime follow the entries inside it, and each of those is recorded on its own line.
# A regular file under /etc or /usr also gets the sha256 of its content, so a change that keeps
# size and mtime still shows.
#
# Exclusions (host paths; none of them is recorded):
#   /proc /sys /dev /run /tmp /var/tmp /var/cache /var/log  volatile or per-boot system state
#   $HOME                                                    the invoking user's home
#   the Owned Paths of store/paths.py (OwnedPaths.from_env):
#     data    $XDG_DATA_HOME/ginn, default $HOME/.local/share/ginn
#     config  $XDG_CONFIG_HOME/ginn, default $HOME/.config/ginn
#     state   $XDG_STATE_HOME/ginn, default $HOME/.local/state/ginn
#     runtime GINN_RUNTIME_DIR, default /run/ginn/<uid> (already under /run)
#     apps    $XDG_DATA_HOME/applications/ginn-*.desktop, the only owned part of that directory
#   the directory that holds this script's snapshots
#   each --exclude PATH
# An XDG value counts only when it is absolute, as in store/paths.py. A GINN_RUNTIME_DIR that is
# set and relative is a usage error (exit 2). HOME must be absolute.
#
# --root DIR walks DIR instead of /, so tests can run on a temporary tree. Every host path named
# above, and every --exclude PATH, is then taken under DIR. The output names entries by their
# on-disk path, which reads as a host path when DIR is /.
#
# Unreadable entries (a directory without read permission, a file whose content cannot be read)
# are recorded as unreadable, and the check goes on. A path that vanishes during the walk is not
# an error. A volatile file that the host changes on its own, such as /etc/resolv.conf, is a
# false positive unless it is passed with --exclude.
#
# Limits: the hash pass covers /etc and /usr only, for speed. A name with a newline or a tab in
# it may be split across lines of the diff.
#
# Exit status:
#   0  nothing outside the exclusions changed, and COMMAND exited 0
#   1  something changed (wins over the next code; COMMAND's status is printed too)
#   2  usage error, or a snapshot could not be taken
#   3  nothing changed, but COMMAND exited non-zero
#
# A snapshot directory is kept, and its path is printed, when the check finds a change.
set -eu

usage() {
    printf 'usage: %s [--root DIR] [--exclude PATH]... -- COMMAND [ARG...]\n' "$0" >&2
    printf '       %s --print-exclusions\n' "$0" >&2
    exit 2
}

die() {
    printf 'host-diff: %s\n' "$*" >&2
    exit 2
}

# at HOST_PATH: the on-disk path of a host path under --root (the same path when --root is /).
at() {
    printf '%s%s\n' "$base" "$1"
}

# glob_escape TEXT: TEXT with the find -path wildcards escaped, so that it matches only itself.
glob_escape() {
    printf '%s\n' "$1" | sed 's/[][*?\\]/\\&/g'
}

# xdg_base VALUE DEFAULT: VALUE when it is an absolute path, else DEFAULT (store/paths.py).
xdg_base() {
    case ${1:-} in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s\n' "$2" ;;
    esac
}

# host_paths: the fixed exclusions as host paths, one per line. The desktop-entry glob is listed
# by owned_listing, because its wildcard must not be escaped.
host_paths() {
    printf '%s\n' /proc /sys /dev /run /tmp /var/tmp /var/cache /var/log
    printf '%s\n' "$home" "$data" "$config" "$state" "$runtime"
}

# owned_listing: the lines that --print-exclusions shows for the owned desktop entries.
owned_listing() {
    printf '%s/ginn-*.desktop\n' "$apps"
}

# add_prune PATTERN: one find -path pattern, one line of $work/prune.
add_prune() {
    printf '%s\n' "$1" >> "$work/prune"
}

# snapshot NAME: write the sorted snapshot of the walk to $work/NAME.snap.
# A snapshot line is `PATH TAB TYPE TAB MODE ...` for metadata, `PATH TAB sha256 TAB HASH` for
# content, or `! TAB MESSAGE` for an entry that could not be read.
snapshot() {
    name=$1
    meta="$work/$name.meta"
    part="$work/$name.part"
    errs="$work/$name.err"
    hashes="$work/$name.hashes"
    snap="$work/$name.snap"
    : > "$part"
    : > "$errs"
    : > "$hashes"
    # The prune patterns become the positional parameters of this function, one -path each.
    set --
    while IFS= read -r pat; do
        if [ $# -eq 0 ]; then
            set -- -path "$pat"
        else
            set -- "$@" -o -path "$pat"
        fi
    done < "$work/prune"
    find "$start" -mindepth 1 \( "$@" \) -prune -o \
        -printf '%p\t%y\t%m\t%U\t%G\t%s\t%T@\t%l\n' > "$meta" 2>> "$errs" || true
    for scope in etc usr; do
        dir=$(at "/$scope")
        [ -d "$dir" ] || continue
        find "$dir" \( "$@" \) -prune -o -type f -readable -print0 2>> "$errs" \
            | xargs -0 -r sha256sum >> "$hashes" 2>> "$errs" || true
        find "$dir" \( "$@" \) -prune -o -type f ! -readable \
            -printf '%p\tsha256\tunreadable\n' >> "$part" 2>> "$errs" || true
    done
    sed 's/^\([0-9a-f]*\)  \(.*\)$/\2\tsha256\t\1/' "$hashes" >> "$part"
    grep -v 'No such file or directory' "$errs" | LC_ALL=C sort -u | sed 's/^/!\t/' >> "$part" \
        || true
    # A directory line keeps its path, type, mode and owner. Its size and mtime become `-`.
    cat "$meta" "$part" \
        | sed -E "/^[^${tab}]*${tab}d${tab}/ s/^([^${tab}]*${tab}d${tab}[^${tab}]*${tab}[^${tab}]*${tab}[^${tab}]*)${tab}[^${tab}]*${tab}[^${tab}]*${tab}/\\1${tab}-${tab}-${tab}/" \
        | LC_ALL=C sort > "$snap"
}

# unreadable_count FILE: how many snapshot lines record an unreadable entry.
unreadable_count() {
    grep -c -e '^!' -e 'unreadable$' "$1" || true
}

tab=$(printf '\t')
root=/
print_only=0
saw_separator=0
extra_excludes=""

while [ $# -gt 0 ]; do
    case $1 in
        --root)
            [ $# -ge 2 ] && [ -n "$2" ] || usage
            root=$2
            shift 2
            ;;
        --exclude)
            [ $# -ge 2 ] || usage
            extra_excludes="$extra_excludes$2
"
            shift 2
            ;;
        --print-exclusions)
            print_only=1
            shift
            ;;
        --)
            saw_separator=1
            shift
            break
            ;;
        *)
            usage
            ;;
    esac
done

if [ "$print_only" -eq 0 ]; then
    [ "$saw_separator" -eq 1 ] && [ $# -ge 1 ] || usage
fi

case ${HOME:-} in
    /*) home=$HOME ;;
    *) die "HOME must be an absolute path" ;;
esac
uid=$(id -u)
data="$(xdg_base "${XDG_DATA_HOME:-}" "$home/.local/share")/ginn"
apps="$(xdg_base "${XDG_DATA_HOME:-}" "$home/.local/share")/applications"
config="$(xdg_base "${XDG_CONFIG_HOME:-}" "$home/.config")/ginn"
state="$(xdg_base "${XDG_STATE_HOME:-}" "$home/.local/state")/ginn"
case ${GINN_RUNTIME_DIR:-} in
    '') runtime="/run/ginn/$uid" ;;
    /*) runtime=$GINN_RUNTIME_DIR ;;
    *) die "GINN_RUNTIME_DIR must be an absolute path" ;;
esac

if [ "$print_only" -eq 1 ]; then
    host_paths
    owned_listing
    printf '%s' "$extra_excludes" | while IFS= read -r x; do
        if [ -n "$x" ]; then printf '%s\n' "$x"; fi
    done
    exit 0
fi

# Find must be GNU findutils: -printf and -readable are used below.
find /dev/null -maxdepth 0 -printf '' -readable > /dev/null 2>&1 \
    || die "GNU find (findutils) is required on PATH"

base=${root%/}
start=${base:-/}
[ -d "$start" ] || die "--root $root is not a directory"

work=$(mktemp -d "${TMPDIR:-/tmp}/ginn-host-diff.XXXXXX") \
    || die "cannot create a work directory under ${TMPDIR:-/tmp}"
keep=0
cleanup() {
    if [ "$keep" -eq 0 ]; then
        rm -rf -- "$work"
    fi
}
trap cleanup EXIT

: > "$work/prune"
host_paths | while IFS= read -r p; do
    add_prune "$(glob_escape "$(at "$p")")"
done
add_prune "$(glob_escape "$(at "$apps")")/ginn-*.desktop"
add_prune "$(glob_escape "$work")"
printf '%s' "$extra_excludes" | while IFS= read -r x; do
    if [ -n "$x" ]; then add_prune "$(glob_escape "$(at "$x")")"; fi
done

snapshot before
status=0
"$@" || status=$?
snapshot after

LC_ALL=C comm -23 "$work/before.snap" "$work/after.snap" > "$work/gone"
LC_ALL=C comm -13 "$work/before.snap" "$work/after.snap" > "$work/new"
changed=$(( $(wc -l < "$work/gone") + $(wc -l < "$work/new") ))

printf 'host-diff: before %s entries (%s unreadable), after %s entries (%s unreadable)\n' \
    "$(wc -l < "$work/before.snap")" "$(unreadable_count "$work/before.snap")" \
    "$(wc -l < "$work/after.snap")" "$(unreadable_count "$work/after.snap")"
if [ "$status" -ne 0 ]; then
    printf 'host-diff: COMMAND exited %s\n' "$status"
fi
if [ "$changed" -gt 0 ]; then
    printf 'host-diff: %s line(s) changed outside the exclusions (- before, + after)\n' "$changed"
    sed 's/^/- /' "$work/gone"
    sed 's/^/+ /' "$work/new"
    printf 'host-diff: snapshots kept in %s\n' "$work"
    keep=1
    exit 1
fi
if [ "$status" -ne 0 ]; then
    exit 3
fi
printf 'host-diff: no change outside the exclusions\n'
exit 0
