#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Provenance scan (NFR-063): flags ginn source that shares code with a reference source.
# The reference repositories are not named in this file; CI passes them in through
# PROVENANCE_REPOS. docs/provenance.md describes the gate, the allowlist and the triage steps.
#
# Usage: PROVENANCE_REPOS="URL[@REF] ..." tools/provenance-scan.sh
#   PROVENANCE_REPOS            required. Space-separated https clone URLs without credentials.
#                               Append @REF (branch, tag or commit SHA) to pin a reference;
#                               without it the remote HEAD is scanned. Each resolved commit is
#                               printed.
#   PROVENANCE_REPORT_DIR       optional. Copy table.txt and report.html into this directory.
#                               report.html shows reference code: never commit it or publish it.
#   PROVENANCE_FAIL_SIMILARITY  gate similarity, default 0.30
#   PROVENANCE_MIN_TOKENS       gate matched tokens, default 60
#
# Exit status: 0 pass, 1 a file is over the gate, 2 configuration or setup error.
set -euo pipefail

COPYDETECT_VERSION=0.5.0
NOISE_THRESHOLD=25
GUARANTEE_THRESHOLD=30
DISPLAY_THRESHOLD=0.0
SCAN_EXTENSIONS=py,sh,c,h
SCAN_DIRS=(src tools tests packaging native)
FAIL_SIMILARITY=${PROVENANCE_FAIL_SIMILARITY:-0.30}
MIN_TOKENS=${PROVENANCE_MIN_TOKENS:-60}
ALLOWLIST=docs/provenance-allowlist.toml

url_pattern='^https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+$'
ref_pattern='^[A-Za-z0-9._/-]+$'
shell_shebang='^#!(/bin/sh|/bin/bash|/usr/bin/env (sh|bash))([[:space:]]|$)'

die() {
    echo "provenance-scan: $*" >&2
    exit 2
}

repo_root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
export GIT_TERMINAL_PROMPT=0

# Copy each extensionless shell script to NAME.sh beside it, so that the scanner sees it.
# Only the first line is read, to recognise the interpreter.
add_shell_copies() {
    local dir=$1 file base first copies=0
    while IFS= read -r -d '' file; do
        base=${file##*/}
        case "$base" in
            *.*) continue ;;
        esac
        IFS= read -r first <"$file" || first=""
        if [[ "$first" =~ $shell_shebang ]] && [ ! -e "$file.sh" ]; then
            cp -- "$file" "$file.sh"
            copies=$((copies + 1))
        fi
    done < <(find "$dir" -path "$dir/.git" -prune -o -type f -print0)
    echo "  extensionless shell scripts copied to .sh: $copies"
}

# Clone one reference entry ("URL" or "URL@REF") at depth 1 into the directory $2.
fetch_reference() {
    local entry=$1 dest=$2 url ref
    if [[ "$entry" == *@* ]]; then
        url=${entry%@*}
        ref=${entry##*@}
    else
        url=$entry
        ref=HEAD
    fi
    [[ "$url" =~ $url_pattern ]] || die "PROVENANCE_REPOS entry is not an https URL: $entry"
    [[ "$ref" =~ $ref_pattern ]] || die "PROVENANCE_REPOS entry has a bad ref: $entry"
    mkdir -p "$dest"
    git -C "$dest" init --quiet
    git -C "$dest" fetch --quiet --depth 1 "$url" "$ref"
    git -C "$dest" checkout --quiet FETCH_HEAD
    echo "reference: $url@$ref at $(git -C "$dest" rev-parse HEAD)"
    add_shell_copies "$dest"
}

[ -n "${PROVENANCE_REPOS:-}" ] || die "set PROVENANCE_REPOS to the reference URLs (docs/provenance.md)"
read -r -a repos <<<"$PROVENANCE_REPOS"
[ "${#repos[@]}" -gt 0 ] || die "PROVENANCE_REPOS names no reference source"

index=0
for entry in "${repos[@]}"; do
    index=$((index + 1))
    fetch_reference "$entry" "$scratch/ref-$index"
done

test_dirs=()
for dir in "${SCAN_DIRS[@]}"; do
    # Skip directories that do not exist yet, for example native/ before M5.
    if [ -d "$repo_root/$dir" ]; then
        test_dirs+=("$repo_root/$dir")
    fi
done
[ "${#test_dirs[@]}" -gt 0 ] || die "no source directories to scan under $repo_root"

python3 -m venv "$scratch/venv"
"$scratch/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    "copydetect==$COPYDETECT_VERSION"

report_dir="$scratch/report"
mkdir -p "$report_dir"
status=0
# The Python API returns the matched pairs directly. Only pairs whose reference side lies
# under $scratch/ref-* count; test-versus-test pairs inside this repository are not provenance.
# The highlighted code in each result row is never read or printed.
"$scratch/venv/bin/python" - "$scratch" "$report_dir" "$repo_root" "$NOISE_THRESHOLD" \
    "$GUARANTEE_THRESHOLD" "$DISPLAY_THRESHOLD" "$SCAN_EXTENSIONS" "$FAIL_SIMILARITY" \
    "$MIN_TOKENS" "$repo_root/$ALLOWLIST" "${test_dirs[@]}" <<'PY' || status=$?
import glob
import os
import sys
import tomllib

from copydetect import CopyDetector

REQUIRED_KEYS = ("path", "reason", "reviewed_by", "date")

(scratch, report_dir, root, noise, guarantee, display, extensions,
 fail_similarity_text, min_tokens_text, allowlist_path, *test_dirs) = sys.argv[1:]


def fail(message):
    print(f"provenance-scan: {message}", file=sys.stderr)
    sys.exit(2)


def load_allowlist(path):
    if not os.path.isfile(path):
        fail(f"allowlist not found: {path}")
    try:
        with open(path, "rb") as handle:
            entries = tomllib.load(handle).get("allow", [])
    except tomllib.TOMLDecodeError as exc:
        fail(f"allowlist is not valid TOML: {exc}")
    allowed = {}
    for number, entry in enumerate(entries, start=1):
        for key in REQUIRED_KEYS:
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                fail(f"allowlist entry {number} needs a non-empty '{key}'")
        rel = entry["path"]
        if os.path.isabs(rel) or os.path.normpath(rel) != rel:
            fail(f"allowlist entry {number}: path must be relative and normalised: {rel}")
        allowed[rel] = entry
    return allowed


def scanned_files(dirs, suffixes):
    found = set()
    for top in dirs:
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            for name in filenames:
                if os.path.splitext(name)[1] in suffixes:
                    found.add(os.path.realpath(os.path.join(dirpath, name)))
    return sorted(found)


def reference_label(path):
    real = os.path.realpath(path)
    for index, ref_root in enumerate(ref_roots, start=1):
        if real.startswith(ref_root + os.sep):
            return f"ref-{index}:" + os.path.relpath(real, ref_root)
    return None


def render(rows):
    file_width = max([len("FILE")] + [len(r["file"]) for r in rows])
    ref_width = max([len("REFERENCE")] + [len(r["ref"]) for r in rows])
    lines = [
        f"{'FILE':<{file_width}}  {'SIMILARITY':>10}  {'REFERENCE':<{ref_width}}  "
        f"{'TOKENS':>6}  GATE"
    ]
    for r in rows:
        lines.append(
            f"{r['file']:<{file_width}}  {r['sim']:>10.3f}  {r['ref']:<{ref_width}}  "
            f"{r['tokens']:>6}  {r['gate']}"
        )
    return "\n".join(lines) + "\n"


fail_similarity = float(fail_similarity_text)
min_tokens = int(min_tokens_text)
suffixes = {"." + ext for ext in extensions.split(",")}
root = os.path.realpath(root)
allowed = load_allowlist(allowlist_path)
ref_dirs = [os.path.realpath(path) for path in glob.glob(os.path.join(scratch, "ref-*"))]
ref_dirs.sort(key=lambda path: int(os.path.basename(path).split("-", 1)[1]))
ref_roots = ref_dirs

detector = CopyDetector(
    test_dirs=list(test_dirs),
    ref_dirs=ref_dirs,
    extensions=extensions.split(","),
    noise_t=int(noise),
    guarantee_t=int(guarantee),
    display_t=float(display),
    autoopen=False,
    silent=True,
    out_file=os.path.join(report_dir, "report.html"),
)
detector.run()
detector.generate_html_report()

# Per test file: every reference pair as (similarity, matched tokens, reference label).
# Similarity is the larger of the two copied fractions of the pair. Matched tokens are the
# test file's tokens covered by copied fragments of that pair (copydetect's token count).
pairs = {}
for row in detector.get_copied_code_list():
    label = reference_label(row[3])
    if label is None:
        continue
    similarity = max(float(row[0]), float(row[1]))
    pairs.setdefault(os.path.realpath(row[2]), []).append((similarity, int(row[6]), label))

rows = []
for path in scanned_files(test_dirs, suffixes):
    rel = os.path.relpath(path, root)
    matches = pairs.get(path, [])
    best = max(matches, key=lambda m: (m[0], m[1]), default=(0.0, 0, "-"))
    over = [m for m in matches if m[0] >= fail_similarity and m[1] >= min_tokens]
    if over and rel in allowed:
        gate = "ALLOWED"
    elif over:
        gate = "FAIL"
    else:
        gate = "ok"
    trigger = max(over, key=lambda m: m[0]) if over else None
    rows.append({
        "file": rel,
        "sim": best[0],
        "ref": best[2],
        "tokens": best[1],
        "gate": gate,
        "trigger": trigger,
    })
rows.sort(key=lambda r: (-r["sim"], r["file"]))

table = render(rows)
with open(os.path.join(report_dir, "table.txt"), "w", encoding="utf-8") as handle:
    handle.write(table)
sys.stdout.write(table)
print(
    f"gate: similarity >= {fail_similarity} and matched tokens >= {min_tokens}, "
    "checked per reference pair"
)
failed = [r for r in rows if r["gate"] == "FAIL"]
allowed_hits = [r for r in rows if r["gate"] == "ALLOWED"]
print(
    f"provenance: {len(ref_roots)} reference source(s), {len(rows)} file(s) scanned, "
    f"{len(allowed_hits)} allowlisted hit(s), {len(failed)} over the gate"
)
scanned = {r["file"] for r in rows}
for stale in sorted(set(allowed) - scanned):
    print(f"warning: allowlist entry matches no scanned file: {stale}", file=sys.stderr)
for r in failed:
    trig_sim, trig_tokens, trig_ref = r["trigger"]
    print(
        f"FAIL: {r['file']}: similarity {trig_sim:.3f}, {trig_tokens} matched tokens, "
        f"reference {trig_ref}",
        file=sys.stderr,
    )
sys.exit(1 if failed else 0)
PY

if [ -n "${PROVENANCE_REPORT_DIR:-}" ]; then
    mkdir -p -- "$PROVENANCE_REPORT_DIR"
    for name in table.txt report.html; do
        if [ -f "$report_dir/$name" ]; then
            cp -- "$report_dir/$name" "$PROVENANCE_REPORT_DIR/"
        fi
    done
    echo "report: $PROVENANCE_REPORT_DIR/table.txt (numbers and paths)"
    echo "report: $PROVENANCE_REPORT_DIR/report.html (shows reference code; human triage only)"
fi
if [ "$status" -ne 0 ]; then
    echo "provenance scan FAILED (status $status)" >&2
    exit "$status"
fi
echo "provenance scan passed"
