#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""S-07e seccomp profile generator (FR-062 AC-3, NFR-SEC-002).

usage: s07e_seccomp.py SRC DST NAME [NAME ...]

Reads the host default seccomp profile SRC (podman's /usr/share/containers/seccomp.json).
Removes each NAME from every rule that names it, dropping a rule left with no names.
Inserts one unconditional SCMP_ACT_ALLOW rule for the NAMEs at the front of the list.
Everything else is copied unchanged. The result is written to DST.

Fails closed: exits 2 without writing DST when SRC cannot be read, or when any NAME
does not appear in SRC. It never falls back to an unconfined profile.
"""
import json
import sys


def main(argv):
    if len(argv) < 4:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src, dst, gate = argv[1], argv[2], argv[3:]
    try:
        with open(src, encoding="utf-8") as fh:
            prof = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {src}: {exc}", file=sys.stderr)
        return 2

    gate_set = set(gate)
    found = set()
    removed = 0
    rules = []
    for rule in prof.get("syscalls", []):
        if "name" in rule:
            print(f"error: unexpected singular 'name' key in {src}", file=sys.stderr)
            return 2
        names = rule.get("names", [])
        touched = [n for n in names if n in gate_set]
        if not touched:
            rules.append(rule)
            continue
        found.update(touched)
        removed += len(touched)
        keep = [n for n in names if n not in gate_set]
        if keep:
            rule = dict(rule)
            rule["names"] = keep
            rules.append(rule)

    missing = sorted(gate_set - found)
    if missing:
        print(f"error: {missing} not found in {src}; nothing written", file=sys.stderr)
        return 2

    rules.insert(0, {
        "names": sorted(gate_set),
        "action": "SCMP_ACT_ALLOW",
        "args": [],
        "includes": {},
        "excludes": {},
    })
    prof["syscalls"] = rules
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(prof, fh, indent=1)
        fh.write("\n")

    print(f"wrote {dst}: removed {removed} name entries from source rules; "
          f"inserted unconditional ALLOW for {sorted(gate_set)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
