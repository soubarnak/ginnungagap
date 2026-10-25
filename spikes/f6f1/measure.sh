#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# F6 Part A memory runs (spikes/results/F6-F1-probe.md): N=5 idle runs of each hand variant, then
# N=5 of the product realm f6-arch as the control. Run from anywhere; it changes into the repo root.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
for v in v0 v1cp v3cp v4cp; do
  echo "=== variant $v"
  "$PY" spikes/f6f1/f6f1.py variant "$v" 5
done
echo "=== product f6-arch"
"$PY" spikes/f6f1/f6f1.py product-measure 5
echo ALL-DONE
