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
#   PROVENANCE_SEEDS            optional, default 0,1,2,3. Comma-separated PYTHONHASHSEED values,
#                               each 0 to 4294967295. The detection runs once per seed; the
#                               table keeps the maximum similarity and token count per pair.
#   PROVENANCE_REPORT_DIR       optional. Receives table.txt only (numbers and paths). CI sets it.
#   PROVENANCE_HTML_DIR         optional. Receives report-seed-N.html, one per seed, each showing
#                               the matches of that seed. The HTML shows reference code: keep it
#                               outside the checkout, never set it in CI, never upload it.
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
SEEDS_TEXT=${PROVENANCE_SEEDS:-0,1,2,3}
ALLOWLIST=docs/provenance-allowlist.toml

url_pattern='^https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+$'
ref_pattern='^[A-Za-z0-9._/-]+$'
shell_shebang='^#!(/bin/sh|/bin/bash|/usr/bin/env (sh|bash))([[:space:]]|$)'
seed_pattern='^(0|[1-9][0-9]{0,9})(,(0|[1-9][0-9]{0,9}))*$'

die() {
    echo "provenance-scan: $*" >&2
    exit 2
}

repo_root=$(cd "$(dirname "$0")/.." && pwd -P)
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
export GIT_TERMINAL_PROMPT=0

# Seeds: PYTHONHASHSEED accepts 0 to 4294967295. Leading zeros are refused so that each seed has
# one spelling, and the seed set in the table header stays exact.
[[ "$SEEDS_TEXT" =~ $seed_pattern ]] \
    || die "PROVENANCE_SEEDS must be a comma-separated list of integers, for example 0,1,2,3"
IFS=, read -r -a seeds <<<"$SEEDS_TEXT"
for seed in "${seeds[@]}"; do
    (( seed <= 4294967295 )) || die "PROVENANCE_SEEDS value out of range (0 to 4294967295): $seed"
done
duplicate_seeds=$(printf '%s\n' "${seeds[@]}" | sort | uniq -d)
[ -z "$duplicate_seeds" ] || die "PROVENANCE_SEEDS repeats a seed: $duplicate_seeds"

# The HTML report goes outside the checkout, so that it can never be committed by accident.
html_dir=""
if [ -n "${PROVENANCE_HTML_DIR:-}" ]; then
    html_dir=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$PROVENANCE_HTML_DIR")
    case "$html_dir/" in
        "$repo_root/"*) die "PROVENANCE_HTML_DIR must lie outside the repository: $html_dir" ;;
    esac
fi

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

# The Python code lives in its own directory, apart from the clones and the venv.
code_dir="$scratch/code"
pairs_dir="$scratch/pairs"
html_scratch="$scratch/html"
report_dir="$scratch/report"
mkdir -p "$code_dir" "$pairs_dir" "$html_scratch" "$report_dir"

# One detection run per seed. The seed reaches copydetect only through PYTHONHASHSEED, which
# Python reads at interpreter start, so each seed needs its own process. The Python API returns
# the matched pairs directly. The highlighted code in each result row is never read or printed.
cat >"$code_dir/detect.py" <<'PY'
import glob
import json
import os
import sys

from copydetect import CopyDetector

(scratch, noise, guarantee, display, extensions, html_path, out_path, *test_dirs) = sys.argv[1:]

ref_dirs = sorted(
    (os.path.realpath(path) for path in glob.glob(os.path.join(scratch, "ref-*"))),
    key=lambda path: int(os.path.basename(path).split("-", 1)[1]),
)


def reference_label(path):
    real = os.path.realpath(path)
    for index, ref_root in enumerate(ref_dirs, start=1):
        if real.startswith(ref_root + os.sep):
            return f"ref-{index}:" + os.path.relpath(real, ref_root)
    return None


detector = CopyDetector(
    test_dirs=list(test_dirs),
    ref_dirs=ref_dirs,
    extensions=extensions.split(","),
    noise_t=int(noise),
    guarantee_t=int(guarantee),
    display_t=float(display),
    autoopen=False,
    silent=True,
    out_file=html_path or os.path.join(scratch, "unused.html"),
)
detector.run()
if html_path:
    detector.generate_html_report()

# Only pairs whose reference side lies under a ref-N directory count. Test-versus-test pairs
# inside this repository are not provenance. Row layout: [test sim, ref sim, test path,
# ref path, highlighted test code, highlighted ref code, overlapping tokens].
rows = []
for row in detector.get_copied_code_list():
    label = reference_label(row[3])
    if label is None:
        continue
    similarity = max(float(row[0]), float(row[1]))
    rows.append([os.path.realpath(row[2]), label, similarity, int(row[6])])
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(rows, handle)
PY

for seed in "${seeds[@]}"; do
    html_out=""
    if [ -n "$html_dir" ]; then
        html_out="$html_scratch/report-seed-$seed.html"
    fi
    PYTHONHASHSEED="$seed" "$scratch/venv/bin/python" "$code_dir/detect.py" "$scratch" \
        "$NOISE_THRESHOLD" "$GUARANTEE_THRESHOLD" "$DISPLAY_THRESHOLD" "$SCAN_EXTENSIONS" \
        "$html_out" "$pairs_dir/seed-$seed.json" "${test_dirs[@]}" \
        || die "detection failed for PYTHONHASHSEED=$seed"
done

# Merge the seeds and apply the gate. Each pair takes its maximum similarity and its maximum
# token count over the seeds, independently; the gate then runs on the merged pairs.
cat >"$code_dir/gate.py" <<'PY'
import glob
import json
import os
import sys
import tomllib

REQUIRED_KEYS = ("path", "reason", "reviewed_by", "date")

(scratch, out_path, root, fail_similarity_text, min_tokens_text, allowlist_path,
 extensions, seeds_text, *test_dirs) = sys.argv[1:]
seeds = seeds_text.split(",")


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


def render(rows):
    file_width = max([len("FILE")] + [len(r["file"]) for r in rows])
    ref_width = max([len("REFERENCE")] + [len(r["ref"]) for r in rows])
    lines = [
        "seeds: PYTHONHASHSEED=" + ",".join(seeds)
        + " (maximum over the seeds, per file and reference pair)",
        f"{'FILE':<{file_width}}  {'SIMILARITY':>10}  {'REFERENCE':<{ref_width}}  "
        f"{'TOKENS':>6}  GATE",
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
ref_roots = sorted(
    glob.glob(os.path.join(scratch, "ref-*")),
    key=lambda path: int(os.path.basename(path).split("-", 1)[1]),
)

merged = {}
for seed in seeds:
    with open(os.path.join(scratch, "pairs", f"seed-{seed}.json"), encoding="utf-8") as handle:
        for path, label, similarity, tokens in json.load(handle):
            current = merged.get((path, label))
            if current is None:
                merged[(path, label)] = [similarity, tokens]
            else:
                current[0] = max(current[0], similarity)
                current[1] = max(current[1], tokens)

pairs = {}
for (path, label), (similarity, tokens) in merged.items():
    pairs.setdefault(path, []).append((similarity, tokens, label))

rows = []
for path in scanned_files(test_dirs, suffixes):
    rel = os.path.relpath(path, root)
    # Sorted by label, so that ties on similarity and tokens resolve the same way on every run.
    matches = sorted(pairs.get(path, []), key=lambda m: m[2])
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
with open(out_path, "w", encoding="utf-8") as handle:
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

status=0
"$scratch/venv/bin/python" "$code_dir/gate.py" "$scratch" "$report_dir/table.txt" "$repo_root" \
    "$FAIL_SIMILARITY" "$MIN_TOKENS" "$repo_root/$ALLOWLIST" "$SCAN_EXTENSIONS" "$SEEDS_TEXT" \
    "${test_dirs[@]}" || status=$?

# The report directory receives table.txt and nothing else. The HTML files are never copied there.
if [ -n "${PROVENANCE_REPORT_DIR:-}" ] && [ -f "$report_dir/table.txt" ]; then
    mkdir -p -- "$PROVENANCE_REPORT_DIR"
    cp -- "$report_dir/table.txt" "$PROVENANCE_REPORT_DIR/table.txt"
    echo "report: $PROVENANCE_REPORT_DIR/table.txt (numbers and paths)"
fi
if [ -n "$html_dir" ]; then
    mkdir -p -- "$html_dir"
    cp -- "$html_scratch"/report-seed-*.html "$html_dir/"
    echo "html: $html_dir (shows reference code; local human triage only, never upload)"
fi
if [ "$status" -ne 0 ]; then
    echo "provenance scan FAILED (status $status)" >&2
    exit "$status"
fi
echo "provenance scan passed"
