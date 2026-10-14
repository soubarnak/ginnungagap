# SPDX-License-Identifier: Apache-2.0
#
# Shared by the tools/measure wrappers (T10.4). This file is sourced, never run. The caller sets
# `here`, the directory of the wrapper, before it sources this file.
#
# PY is the Python for pytest: GINN_MEASURE_PYTHON when set, else the repository venv, else
# python3. The host-gate tests need the venv, because they import ginn.

root=$(cd "$here/../.." && pwd)
PY=${GINN_MEASURE_PYTHON:-}
if [ -z "$PY" ]; then
    if [ -x "$root/.venv/bin/python" ]; then
        PY="$root/.venv/bin/python"
    else
        PY=python3
    fi
fi

# run_gate_test NODE RESULTS: run one host-gate test on this host with the T4.9 method. The test
# writes its records to RESULTS (GINN_T49_RESULTS, tests/integration/conftest.py). The verdict
# comes from those records, not from the pytest status, so a failed test is still read.
# GINN_INTEGRATION=1 switches the gate on. XDG_RUNTIME_DIR is unset for the run, as in T4.9, so
# the test's own session directory is the only runtime dir in use.
run_gate_test() {
    env -u XDG_RUNTIME_DIR GINN_INTEGRATION=1 GINN_T49_RESULTS="$2" \
        "$PY" -m pytest -q -p no:cacheprovider \
        "$root/tests/integration/test_lifecycle_real.py::$1" >&2 || true
}
