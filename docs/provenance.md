# Provenance scan (NFR-063)

The provenance scan checks that ginn source does not copy code from the reference repositories. CI runs it in the `provenance` job of `.github/workflows/ci.yaml`. You can run it locally with `tools/provenance-scan.sh`. The clean-room rules in `AGENTS.md` still apply: design comes from the SRS and public specifications, and every file is written from scratch.

## What the scan does

1. Clones each entry of `PROVENANCE_REPOS` at depth 1 into a temporary directory, which is removed when the script exits. An entry is `URL` or `URL@REF`. Only `https://` URLs without credentials are accepted. `REF` is a branch, tag or commit SHA. Without `REF` the default branch is scanned. The script prints the resolved commit of each reference.
2. Copies each extensionless file whose first line starts with `#!/bin/sh`, `#!/usr/bin/env sh`, `#!/bin/bash` or `#!/usr/bin/env bash` to `NAME.sh` beside it, so that shell scripts without an extension are scanned too. The original file is kept.
3. Compares every `.py`, `.sh`, `.c` and `.h` file under `src`, `tools`, `tests`, `packaging` and `native` (directories that do not exist are skipped) with the files of every reference. It uses copydetect 0.5.0 with `--noise-threshold 25` and `--guarantee-threshold 30`. Noise threshold: copied runs shorter than 25 tokens are ignored. Guarantee threshold: copied runs of at least 30 tokens are always found.
4. Runs step 3 once for each seed in `PROVENANCE_SEEDS`, default `0,1,2,3`. Each run is a separate Python process with `PYTHONHASHSEED` set to its seed. copydetect fingerprints k-grams with Python's `hash()`, which varies between processes unless the seed is fixed. Each (file, reference) pair takes its maximum similarity and its maximum token count over the seeds. The two maxima are taken separately.
5. Prints a table with one row per scanned file, and applies the gate. When `PROVENANCE_REPORT_DIR` is set, the table is also written to `table.txt` in that directory.

`PROVENANCE_SEEDS` takes comma-separated integers from 0 to 4294967295, with no leading zeros and no repeats. Any other value exits with status 2. A larger seed set can only raise the numbers in the table, never lower them.

The script fails closed on an empty reference. After steps 1 and 2, it counts the files in each reference clone whose names end in `.py`, `.sh`, `.c` or `.h`, with `.git` skipped and the step 2 copies included. A clone with no such files, for example from a wrong `REF` or a changed layout, would give zero matches and pass the gate. In that case the script exits with status 2, naming the reference, before any detector runs. The count of each reference is printed as `reference files to compare`.

## Determinism

With the same seeds, the same reference commits and the same tree, two runs produce the same `table.txt`. The table does not depend on the order in which copydetect returns its pairs: ties on similarity and tokens resolve by reference label. The resolved reference commits are printed on stdout, not written to the table, so that the table diffs only when the detection changes.

## The table

The first line of the table names the seed set, for example `seeds: PYTHONHASHSEED=0,1,2,3 (maximum over the seeds, per file and reference pair)`. The second line is the column header.

| Column | Meaning |
|---|---|
| `FILE` | path of the scanned file, relative to the repository root |
| `SIMILARITY` | the highest similarity of the file to any reference file. For one pair this is the larger of two fractions: the share of the file that is copied and the share of the reference file that is copied. |
| `REFERENCE` | the reference file of that best pair, as `ref-N:path`. `N` is the position of the reference in `PROVENANCE_REPOS`, starting at 1. |
| `TOKENS` | the number of tokens of the file that lie in copied runs against that reference file |
| `GATE` | `ok`, `FAIL` (over the gate and not allowlisted) or `ALLOWED` (over the gate and allowlisted) |

The table shows numbers and paths only. It never shows code. A file with no match has similarity `0.000` and `-` as its reference.

## The gate

A file fails the gate when any one of its reference pairs has similarity at least `PROVENANCE_FAIL_SIMILARITY` (default `0.30`) and at least `PROVENANCE_MIN_TOKENS` tokens (default `60`). Both conditions must hold for the same reference file. The best pair in the table can differ from the pair that fails the gate. The script prints the failing pair.

The gate is calibrated this way because any non-zero similarity is noise. Common idioms, such as an entry point that calls `main()`, match at low similarity over a few tokens. A copied function or module is a long run at high similarity.

The gate runs on the merged maxima over the seeds. The thresholds are not seed-dependent.

Exit status: `0` when no file fails, `1` when a file fails, `2` for a configuration error such as a malformed allowlist, an invalid `PROVENANCE_REPOS` entry, an invalid `PROVENANCE_SEEDS`, or a reference with no files of the scanned extensions.

## The allowlist

`docs/provenance-allowlist.toml` lists files that a human has reviewed and cleared. Each entry needs all four fields:

```toml
[[allow]]
path        = "src/ginn/example.py"   # relative to the repository root
reason      = "why the match is not copied code"
reviewed_by = "name of the human reviewer"
date        = "YYYY-MM-DD"            # date of the review
```

An entry only exempts a file from failing. The file still appears in the table, marked `ALLOWED`. An entry whose path matches no scanned file prints a warning.

## Triage: a human decides

A failing file fails the CI job. Triage works like this:

1. The table row gives the file, the similarity, the matched tokens and the reference path. Those are numbers and paths, so they are safe to read in logs and by agents.
2. A human reads the matched code locally. CI does not provide it. Run the scan with `PROVENANCE_HTML_DIR` set to a directory outside the checkout. The script writes one `report-seed-N.html` per seed. Each file shows the matches of that seed. Every match the gate counts appears in at least one of these files. Open the files outside this repository. They show the matched code on both sides. Do not commit them and do not publish them. Delete them when triage is finished.
3. The human decides:
   - **Copied code.** Rewrite the code from the SRS and public specifications, then run the scan again. Do not allowlist it.
   - **False positive or accepted idiom.** Add an allowlist entry with the reason, the reviewer's name and the date. Commit it in the same change as the code it covers.
4. Agents never read reference sources or the HTML report. An agent may read `table.txt`, fix code that the table flags, and run the scan. An agent must not add allowlist entries. Only a human reviewer adds them.

## CI

The `provenance` job:

- runs `tools/provenance-scan.sh` with `PROVENANCE_REPOS` set in the job environment and `PROVENANCE_REPORT_DIR` set to a directory outside the checkout
- never sets `PROVENANCE_HTML_DIR`, so no HTML report is produced in CI
- uploads only `table.txt` as the `provenance-report` artifact, even when the gate fails

The CI default `PROVENANCE_REPOS` scans three entries:

| Reference | Entry | Scanned at |
|---|---|---|
| `ref-1` | the first reference | default branch HEAD |
| `ref-2` | the second reference | default branch HEAD |
| `ref-3` | the first reference, shell-era tag `1.8.2.5` | commit `40c3cd724faa434aeb0a23e28776665b92de68bd` |

The pin for `ref-3` is the highest `1.8.x` tag the first reference publishes, as listed by `git ls-remote --tags`. The tag is lightweight, so the commit above is the tag target. Check the pin against the `reference: ... at <commit>` line the scan prints. Changing a pin is a reviewed change to `ci.yaml` and to the occurrence count in `tests/unit/test_repo_hygiene.py`.

The second reference publishes no tags, so it is scanned at HEAD. A pin needs a commit SHA chosen by a human first. Until then the scan covers that reference only at HEAD.

## Run locally

```sh
PROVENANCE_REPOS="<reference-url>[@REF] <reference-url>[@REF]" \
    PROVENANCE_REPORT_DIR="$HOME/provenance-table" \
    PROVENANCE_HTML_DIR="$HOME/provenance-html" tools/provenance-scan.sh
```

Set `TMPDIR` to choose where the clones are written. They are removed when the script exits. A run needs network access to clone the references and to install copydetect into a temporary virtual environment. The script needs Python 3.11 or later for `tomllib`. CI uses 3.12.
