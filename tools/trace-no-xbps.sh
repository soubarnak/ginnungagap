#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Process trace for FR-004 (T10.3): ginn never launches an xbps-* executable.
#
#   tools/trace-no-xbps.sh -- COMMAND [ARG...]
#
# Runs COMMAND with its arguments as an argv (no shell) under
#   strace -f -s 4096 -e trace=execve,execveat
# Every exec attempt whose path has a basename that starts with `xbps-` is printed, and the check
# fails. An attempt counts even when the kernel refused it (for example `= -1 EACCES`). Two cases
# make no execve call, so they are not attempts in this trace, and nothing is launched in either:
# a path that does not exist (strace stats COMMAND before it runs), and a PATH search that finds
# no file (dash checks with stat first). The path is read in full, because strace cuts long
# strings at 32 characters unless -s is raised.
#
# Environment:
#   STRACE   the strace binary (default: strace on PATH, then /usr/sbin/strace)
#
# Before tracing, the tool runs `strace -f -e trace=execve -- true` and checks that the exec was
# logged. A host where ptrace is blocked fails here with exit 2, so a blocked tracer is never
# read as a clean run.
#
# Limits: strace follows the processes that COMMAND starts (-f). A process started outside that
# tree is not seen. The trace is only as complete as the exec calls strace is asked to log.
#
# Exit status:
#   0  no xbps-* exec attempt, and COMMAND exited 0
#   1  an xbps-* exec attempt was traced (wins over COMMAND's own status)
#   2  usage error, or strace is missing or cannot trace here
#   3  no xbps-* exec attempt, but COMMAND exited non-zero (its status is printed)
#
# The trace file is kept, and its path is printed, when the check fails (exit 1).
set -eu

usage() {
    printf 'usage: %s -- COMMAND [ARG...]\n' "$0" >&2
    exit 2
}

die() {
    printf 'trace-no-xbps: %s\n' "$*" >&2
    exit 2
}

[ "${1:-}" = "--" ] || usage
shift
[ $# -ge 1 ] || usage

strace_bin=${STRACE:-}
if [ -z "$strace_bin" ]; then
    strace_bin=$(command -v strace 2>/dev/null || true)
fi
if [ -z "$strace_bin" ] && [ -x /usr/sbin/strace ]; then
    strace_bin=/usr/sbin/strace
fi
if [ -z "$strace_bin" ] || [ ! -x "$strace_bin" ]; then
    die "strace is not available (install it, or set STRACE)"
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/ginn-trace-no-xbps.XXXXXX") \
    || die "cannot create a work directory under ${TMPDIR:-/tmp}"
keep=0
cleanup() {
    if [ "$keep" -eq 0 ]; then
        rm -rf -- "$work"
    fi
}
trap cleanup EXIT

if ! "$strace_bin" -f -e trace=execve -o "$work/probe.log" -- true > /dev/null 2>&1 \
    || ! grep -q 'execve(' "$work/probe.log"; then
    die "strace cannot trace a process here (ptrace blocked?); no trace was taken"
fi

log="$work/trace.log"
status=0
"$strace_bin" -f -s 4096 -e trace=execve,execveat -o "$log" -- "$@" || status=$?

# The lines that start an exec call. The pid prefix is optional, as is the dirfd of execveat.
grep -E '^([0-9]+ +|\[pid +[0-9]+\] +)?execve(at)?\(' "$log" > "$work/hits" || true
attempts=$(wc -l < "$work/hits")

xbps=0
while IFS= read -r line; do
    path=$(printf '%s\n' "$line" | sed -E -n \
        's/^([0-9]+ +|\[pid +[0-9]+\] +)?execve(at)?\((AT_FDCWD, *|-?[0-9]+, *)?"(([^"\\]|\\.)*)".*$/\4/p')
    base=${path##*/}
    case $base in
        xbps-*)
            xbps=$((xbps + 1))
            printf 'trace-no-xbps: xbps-* exec attempt: %s\n' "$line" >&2
            ;;
    esac
done < "$work/hits"

printf 'trace-no-xbps: %s exec attempt(s) traced, %s of them xbps-*\n' "$attempts" "$xbps"

if [ "$xbps" -gt 0 ]; then
    printf 'trace-no-xbps: FR-004 violated; the trace is kept in %s\n' "$work" >&2
    keep=1
    exit 1
fi
if [ "$status" -ne 0 ]; then
    printf 'trace-no-xbps: COMMAND exited %s\n' "$status" >&2
    exit 3
fi
exit 0
