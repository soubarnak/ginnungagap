# Provenance scan (NFR-063)

The provenance scan checks that ginn source does not copy code from the reference repositories. CI runs it in the `provenance` job of `.github/workflows/ci.yaml`. You can run it locally with `tools/provenance-scan.sh`. The clean-room rules in `AGENTS.md` still apply: design comes from the SRS and public specifications, and every file is written from scratch.

## What the scan does

1. Clones each entry of `PROVENANCE_REPOS` at depth 1 into a temporary directory, which is removed when the script exits. An entry is `URL` or `URL@REF`. Only `https://` URLs without credentials are accepted. `REF` is a branch, tag or commit SHA. Without `REF` the default branch is scanned. The script prints the resolved commit of each reference.
2. Copies each extensionless file whose first line starts with `#!/bin/sh`, `#!/usr/bin/env sh`, `#!/bin/bash` or `#!/usr/bin/env bash` to `NAME.sh` beside it, so that shell scripts without an extension are scanned too. The original file is kept.
3. Compares every `.py`, `.sh`, `.c` and `.h` file under `src`, `tools`, `tests`, `packaging` and `native` (directories that do not exist are skipped) with the files of every reference. It uses copydetect 0.5.0 with `--noise-threshold 25` and `--guarantee-threshold 30`. Noise threshold: copied runs shorter than 25 tokens are ignored. Guarantee threshold: copied runs of at least 30 tokens are always found.
4. Prints a table with one row per scanned file, and applies the gate. When `PROVENANCE_REPORT_DIR` is set, the table is also written to `table.txt` and the HTML report to `report.html` in that directory. Without it, the report is discarded when the script exits.

## The table

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

Exit status: `0` when no file fails, `1` when a file fails, `2` for a configuration error such as a malformed allowlist or an invalid `PROVENANCE_REPOS` entry.

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
2. A human downloads the `provenance-report` artifact from the CI run, or runs the scan locally with `PROVENANCE_REPORT_DIR` set. The human opens `report.html` outside this repository. The HTML report shows the matched code on both sides. Keep the report out of the repository and do not publish it.
3. The human decides:
   - **Copied code.** Rewrite the code from the SRS and public specifications, then run the scan again. Do not allowlist it.
   - **False positive or accepted idiom.** Add an allowlist entry with the reason, the reviewer's name and the date. Commit it in the same change as the code it covers.
4. Agents never read reference sources or the HTML report. An agent may read `table.txt`, fix code that the table flags, and run the scan. An agent must not add allowlist entries. Only a human reviewer adds them.
5. Delete the local report when triage is finished.

## CI

The `provenance` job:

- runs `tools/provenance-scan.sh` with `PROVENANCE_REPOS` set in the job environment and `PROVENANCE_REPORT_DIR` set to a directory outside the checkout
- uploads that directory as the `provenance-report` artifact, even when the gate fails

The CI default scans both references at their default branch HEAD. A shell-era pin for the second reference is still pending. Its upstream publishes no tags, so a human must choose a commit SHA before it can be added. Until then, the scan covers that reference only at HEAD.

## Run locally

```sh
PROVENANCE_REPOS="<reference-url>[@REF] <reference-url>[@REF]" \
    PROVENANCE_REPORT_DIR="$HOME/provenance-report" tools/provenance-scan.sh
```

Set `TMPDIR` to choose where the clones are written. They are removed when the script exits. A run needs network access to clone the references and to install copydetect into a temporary virtual environment.
