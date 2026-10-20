# SPDX-License-Identifier: Apache-2.0
"""tools/measure (T10.4): the JSON format, the verdicts and the arguments of the NFR scripts.

No test starts a realm or runs podman. The host-gate wrappers run a stub in place of pytest, the
sweep wrappers read a fixture runs.tsv, NFR-001 reads a fake proc tree, and NFR-011 runs on a
small synthetic tree. The numbers here are fixtures, not measurements.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
MEASURE = REPO / "tools" / "measure"
EMIT = MEASURE / "emit.py"
NFR_001 = MEASURE / "nfr-001-idle-processes.py"
NFR_006 = MEASURE / "nfr-006-compute-gpu.py"
NFR_008 = MEASURE / "nfr-008-disk-accounting.py"
NFR_009 = MEASURE / "nfr-009-export-latency.py"
NFR_010 = MEASURE / "nfr-010-gui-staleness.py"
NFR_011 = MEASURE / "nfr-011-integrity-scan.py"
WRAPPERS = {
    "NFR-002": MEASURE / "nfr-002-idle-rss-services-off.sh",
    "NFR-003": MEASURE / "nfr-003-idle-rss-services-on.sh",
    "NFR-004": MEASURE / "nfr-004-exec-latency.sh",
    "NFR-005": MEASURE / "nfr-005-start-time.sh",
    "NFR-007": MEASURE / "nfr-007-create-time.sh",
}


def _load_emit() -> ModuleType:
    """Load tools/measure/emit.py by path, so that no test changes sys.path."""
    spec = importlib.util.spec_from_file_location("emit", EMIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


emit = _load_emit()

SWEEP_HEADER = (
    "sweep_id\tcfg\trun\tstatus\timage\tsettle\tboot_s\tstate\tfailed_units\t"
    "conmon_rss_kib\trealm_rss_kib\tpause_rss_kib\ttotal_rss_kib\trealm_pss_kib\t"
    "pause_pss_kib\ttotal_pss_kib\tnproc_container\tswap_kib\tpss_missing\tpause_pid"
)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """A minimal environment: the interpreter's directory first, so `python3` is found."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    path = os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"])
    env = {"PATH": path, "HOME": str(tmp_path / "home"), "TMPDIR": str(work), "LC_ALL": "C"}
    env.update(extra)
    return env


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def _sweep_tsv(path: Path, rows: list[tuple[str, str, str, int]]) -> Path:
    """Write a runs.tsv with the spike's header. Each row is (sweep, cfg, status, total KiB)."""
    lines = [SWEEP_HEADER]
    for index, (sweep, cfg, status, total) in enumerate(rows):
        fields = [sweep, cfg, str(index), status, "img", "na", "-", "-", "-"]
        fields += ["1", "1", "1", str(total), "1", "1", "1", "1", "0", "0", "1"]
        lines.append("\t".join(fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _spike_out(tmp_path: Path, rows: list[tuple[str, str, str, int]], sweep: str) -> Path:
    out = tmp_path / "spike"
    out.mkdir(exist_ok=True)
    _sweep_tsv(out / "runs.tsv", rows)
    (out / "sweep.id").write_text(sweep + "\n", encoding="utf-8")
    return out


def _gate_results(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


# --- emit.py: the JSON line, the verdicts and the percentile -----------------------------------


@pytest.mark.fr("NFR-004")
def test_verdict_compares_with_the_limit_and_is_not_run_without_a_value() -> None:
    assert emit.verdict_at_most(1.0, 2.0) == emit.PASS
    assert emit.verdict_at_most(2.0, 2.0) == emit.PASS
    assert emit.verdict_at_most(2.5, 2.0) == emit.FAIL
    assert emit.verdict_at_most(None, 2.0) == emit.NOT_RUN


@pytest.mark.fr("NFR-004")
def test_a_result_line_has_the_required_keys_in_order(capsys: pytest.CaptureFixture[str]) -> None:
    entry = emit.line("NFR-004", "launch latency", 129.1, "ms", "p95 <= 300 ms", emit.PASS)
    code = emit.emit(entry)
    printed = json.loads(capsys.readouterr().out)
    assert list(printed) == ["nfr", "metric", "value", "unit", "target", "verdict"]
    assert printed["value"] == 129.1
    assert code == 0


@pytest.mark.fr("NFR-004")
@pytest.mark.parametrize(("verdict", "code"), [(emit.PASS, 0), (emit.FAIL, 1), (emit.NOT_RUN, 2)])
def test_the_exit_status_follows_the_verdict(
    verdict: str, code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    value = None if verdict == emit.NOT_RUN else 1.0
    assert emit.emit(emit.line("NFR-004", "m", value, "ms", "t", verdict)) == code
    capsys.readouterr()


@pytest.mark.fr("NFR-004")
def test_values_are_rounded_and_a_missing_value_is_null() -> None:
    assert emit.line("NFR-004", "m", 1.23456, "s", "t", emit.PASS)["value"] == 1.235
    assert emit.line("NFR-004", "m", None, "s", "t", emit.NOT_RUN)["value"] is None


@pytest.mark.fr("NFR-004")
def test_a_note_is_added_only_when_given() -> None:
    assert "note" not in emit.line("NFR-004", "m", 1.0, "s", "t", emit.PASS)
    noted = emit.line("NFR-004", "m", 1.0, "s", "t", emit.PASS, note="partial")
    assert noted["note"] == "partial"


@pytest.mark.fr("NFR-004")
def test_a_script_with_several_lines_exits_with_its_worst_status() -> None:
    assert emit.worst([0, 2, 1]) == 1
    assert emit.worst([0, 2]) == 2
    assert emit.worst([0, 0]) == 0


@pytest.mark.fr("NFR-004")
def test_the_percentile_is_the_nearest_rank_the_host_gate_uses() -> None:
    assert emit.nearest_rank(list(range(1, 21)), 0.95) == 19
    assert emit.nearest_rank(list(range(1, 101)), 0.95) == 95
    assert emit.nearest_rank([0.3], 0.95) == 0.3


# --- gate results: the T4.9 records of tests/integration/test_lifecycle_real.py ---------------


@pytest.mark.fr("NFR-004")
def test_the_last_record_of_an_item_is_read_and_bad_lines_are_skipped(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text(
        '{"item": "e-exec-p95", "p95_ms": 400.0}\n'
        "not json\n"
        '{"item": "other", "p95_ms": 1.0}\n'
        '{"item": "e-exec-p95", "p95_ms": 129.1}\n',
        encoding="utf-8",
    )
    record = emit.last_record(results, "e-exec-p95")
    assert record is not None and record["p95_ms"] == 129.1
    assert emit.last_record(results, "absent") is None
    assert emit.last_record(tmp_path / "missing.jsonl", "e-exec-p95") is None


@pytest.mark.fr("NFR-004")
@pytest.mark.parametrize(
    ("p95_ms", "verdict", "code"),
    [(129.1, "PASS", 0), (350.0, "FAIL", 1)],
)
def test_the_gate_command_prints_the_verdict_against_the_limit(
    tmp_path: Path, p95_ms: float, verdict: str, code: int
) -> None:
    results = _gate_results(
        tmp_path / "r.jsonl", [{"item": "e-exec-p95", "verdict": "MEASURED", "p95_ms": p95_ms}]
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-I",
            str(EMIT),
            "gate",
            "--results",
            str(results),
            "--item",
            "e-exec-p95",
            "--field",
            "p95_ms",
            "--nfr",
            "NFR-004",
            "--metric",
            "m",
            "--unit",
            "ms",
            "--target",
            "p95 <= 300 ms",
            "--max",
            "300",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path),
    )
    assert done.returncode == code, done.stdout + done.stderr
    assert _json_lines(done.stdout)[0]["verdict"] == verdict


@pytest.mark.fr("NFR-004")
def test_a_gate_item_that_is_absent_is_not_run(tmp_path: Path) -> None:
    results = _gate_results(tmp_path / "r.jsonl", [{"item": "something-else"}])
    entry = emit.gate_line(
        results,
        item="e-exec-p95",
        field="p95_ms",
        count_field=None,
        nfr="NFR-004",
        metric="m",
        unit="ms",
        target="t",
        limit=300,
    )
    assert entry["verdict"] == emit.NOT_RUN and entry["value"] is None


@pytest.mark.fr("NFR-005")
def test_the_cycle_count_is_added_to_the_metric(tmp_path: Path) -> None:
    results = _gate_results(
        tmp_path / "r.jsonl", [{"item": "e-start-cycles", "n": 20, "p95_s": 0.107}]
    )
    entry = emit.gate_line(
        results,
        item="e-start-cycles",
        field="p95_s",
        count_field="n",
        nfr="NFR-005",
        metric="start to running",
        unit="s",
        target="t",
        limit=2,
    )
    assert entry["metric"] == "start to running, n=20"
    assert entry["verdict"] == emit.PASS


# --- sweep results: the runs.tsv of spikes/s05_idle_rss.sh ------------------------------------


@pytest.mark.fr("NFR-003")
def test_the_sweep_median_uses_only_counted_runs_of_one_configuration(tmp_path: Path) -> None:
    rows = [("s1", "on-abd", "ok", 38000 + index * 100) for index in range(10)]
    rows += [("s1", "on-abd", "excluded-pressure", 1)]
    rows += [("s1", "on-base", "ok", 90000)]
    rows += [("s0", "on-abd", "ok", 90000)]
    runs = _sweep_tsv(tmp_path / "runs.tsv", rows)
    entry = emit.sweep_line(
        runs, sweep_id="s1", cfg="on-abd", nfr="NFR-003", metric="m", target="t", limit_mib=40
    )
    assert entry["verdict"] == emit.PASS
    # Ten counted values 38000..38900 KiB: the median is (38400 + 38500) / 2.
    assert entry["value"] == round(38450 / 1024, 3)
    assert "median of 10 runs" in entry["metric"]


@pytest.mark.fr("NFR-003")
def test_the_sweep_median_above_the_limit_fails(tmp_path: Path) -> None:
    runs = _sweep_tsv(tmp_path / "runs.tsv", [("s1", "on-abd", "ok", 50 * 1024) for _ in range(10)])
    entry = emit.sweep_line(
        runs, sweep_id="s1", cfg="on-abd", nfr="NFR-003", metric="m", target="t", limit_mib=40
    )
    assert entry["verdict"] == emit.FAIL and entry["value"] == 50.0


@pytest.mark.fr("NFR-003")
def test_fewer_than_ten_counted_runs_is_not_run(tmp_path: Path) -> None:
    runs = _sweep_tsv(tmp_path / "runs.tsv", [("s1", "on-abd", "ok", 1024) for _ in range(9)])
    entry = emit.sweep_line(
        runs, sweep_id="s1", cfg="on-abd", nfr="NFR-003", metric="m", target="t", limit_mib=40
    )
    assert entry["verdict"] == emit.NOT_RUN and entry["value"] is None
    assert "9 counted run(s) of on-abd; 10 required" in entry["note"]


@pytest.mark.fr("NFR-003")
def test_a_sweep_without_an_id_or_a_runs_file_is_not_run(tmp_path: Path) -> None:
    no_id = emit.sweep_line(
        tmp_path / "none.tsv",
        sweep_id="",
        cfg="off",
        nfr="NFR-002",
        metric="m",
        target="t",
        limit_mib=8,
    )
    no_file = emit.sweep_line(
        tmp_path / "none.tsv",
        sweep_id="s1",
        cfg="off",
        nfr="NFR-002",
        metric="m",
        target="t",
        limit_mib=8,
    )
    assert no_id["verdict"] == emit.NOT_RUN
    assert no_file["verdict"] == emit.NOT_RUN


# --- the wrappers ------------------------------------------------------------------------------


@pytest.mark.fr("NFR-002")
def test_the_services_off_wrapper_reads_the_sweep_named_in_sweep_id(tmp_path: Path) -> None:
    rows = [("s9", "off", "ok", 4500) for _ in range(10)]
    out = _spike_out(tmp_path, rows, "s9")
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(WRAPPERS["NFR-002"])],
        env=_env(tmp_path, GINN_SPIKE_OUT=str(out)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    entry = _json_lines(done.stdout)[0]
    assert entry["nfr"] == "NFR-002" and entry["verdict"] == "PASS"
    assert "S-05r sweep s9" in entry["metric"]


@pytest.mark.fr("NFR-002")
def test_the_services_off_wrapper_with_another_sweep_id_is_not_run(tmp_path: Path) -> None:
    out = _spike_out(tmp_path, [("s9", "off", "ok", 4500) for _ in range(10)], "s9")
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(WRAPPERS["NFR-002"])],
        env=_env(tmp_path, GINN_SPIKE_OUT=str(out), GINN_MEASURE_SWEEP="other"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2
    assert _json_lines(done.stdout)[0]["verdict"] == "NOT RUN"


@pytest.mark.fr("NFR-003")
def test_the_services_on_wrapper_judges_the_abd_configuration_only(tmp_path: Path) -> None:
    rows = [("s9", "on-abd", "ok", 37 * 1024) for _ in range(10)]
    rows += [("s9", "on-base", "ok", 45 * 1024) for _ in range(10)]
    out = _spike_out(tmp_path, rows, "s9")
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(WRAPPERS["NFR-003"])],
        env=_env(tmp_path, GINN_SPIKE_OUT=str(out)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    entry = _json_lines(done.stdout)[0]
    assert entry["value"] == 37.0 and entry["verdict"] == "PASS"


@pytest.mark.fr("NFR-004")
@pytest.mark.parametrize(
    ("p95_ms", "code"),
    [(129.1, 0), (350.0, 1)],
)
def test_the_exec_wrapper_reads_a_results_file(tmp_path: Path, p95_ms: float, code: int) -> None:
    results = _gate_results(
        tmp_path / "r.jsonl", [{"item": "e-exec-p95", "verdict": "MEASURED", "p95_ms": p95_ms}]
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(WRAPPERS["NFR-004"]), "--from-results", str(results)],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == code, done.stdout + done.stderr
    assert _json_lines(done.stdout)[0]["nfr"] == "NFR-004"


@pytest.mark.fr("NFR-005")
def test_the_start_wrapper_reports_the_cycle_count_and_the_partial_note(tmp_path: Path) -> None:
    results = _gate_results(
        tmp_path / "r.jsonl", [{"item": "e-start-cycles", "n": 20, "p95_s": 0.107}]
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(WRAPPERS["NFR-005"]), "--from-results", str(results)],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    entry = _json_lines(done.stdout)[0]
    assert entry["metric"].endswith("n=20") and "50 cycles" in entry["note"]


@pytest.mark.fr("NFR-007")
def test_the_create_wrapper_reads_a_results_file(tmp_path: Path) -> None:
    results = _gate_results(tmp_path / "r.jsonl", [{"item": "c-create-timing", "elapsed_s": 4.758}])
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(WRAPPERS["NFR-007"]), "--from-results", str(results)],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert _json_lines(done.stdout)[0]["value"] == 4.758


STUB = """#!/bin/sh
# Stands in for the pytest run of a wrapper. It logs its argv and environment, and it writes the
# record that the wrapper then reads.
printf '%s\\n' "$@" > "$STUB_LOG"
printf 'GINN_INTEGRATION=%s XDG_RUNTIME_DIR=%s\\n' \\
    "${GINN_INTEGRATION-unset}" "${XDG_RUNTIME_DIR-unset}" >> "$STUB_LOG"
printf '%s\\n' "$STUB_RECORD" >> "$GINN_T49_RESULTS"
"""


@pytest.mark.parametrize(
    ("nfr", "node", "record"),
    [
        pytest.param(
            "NFR-004",
            "test_e_exec_p95_over_100_launches",
            '{"item": "e-exec-p95", "p95_ms": 129.1}',
            marks=pytest.mark.fr("NFR-004"),
        ),
        pytest.param(
            "NFR-005",
            "test_e_start_stop_cycles",
            '{"item": "e-start-cycles", "n": 20, "p95_s": 0.107}',
            marks=pytest.mark.fr("NFR-005"),
        ),
        pytest.param(
            "NFR-007",
            "test_c_create_timing",
            '{"item": "c-create-timing", "elapsed_s": 4.758}',
            marks=pytest.mark.fr("NFR-007"),
        ),
    ],
)
def test_the_default_run_uses_the_host_gate_method(
    tmp_path: Path, nfr: str, node: str, record: str
) -> None:
    stub = tmp_path / "stub-python"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "stub.log"
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(WRAPPERS[nfr])],
        env=_env(
            tmp_path,
            GINN_MEASURE_PYTHON=str(stub),
            STUB_LOG=str(log),
            STUB_RECORD=record,
            XDG_RUNTIME_DIR="/should/not/reach/the/gate",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    argv_and_env = log.read_text(encoding="utf-8").splitlines()
    assert argv_and_env[:3] == ["-m", "pytest", "-q"]
    assert any(node in line and "test_lifecycle_real.py::" in line for line in argv_and_env)
    assert argv_and_env[-1] == "GINN_INTEGRATION=1 XDG_RUNTIME_DIR=unset"
    assert _json_lines(done.stdout)[0]["nfr"] == nfr


@pytest.mark.parametrize(
    "nfr",
    [
        pytest.param("NFR-002", marks=pytest.mark.fr("NFR-002")),
        pytest.param("NFR-003", marks=pytest.mark.fr("NFR-003")),
        pytest.param("NFR-004", marks=pytest.mark.fr("NFR-004")),
        pytest.param("NFR-005", marks=pytest.mark.fr("NFR-005")),
        pytest.param("NFR-007", marks=pytest.mark.fr("NFR-007")),
    ],
)
def test_a_wrapper_refuses_an_unknown_argument(tmp_path: Path, nfr: str) -> None:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(WRAPPERS[nfr]), "--bogus"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2
    assert "usage:" in done.stderr


# --- NFR-001 and NFR-011 -----------------------------------------------------------------------


def _fake_process(proc: Path, pid: int, comm: str, uid: int, rss_kib: int, cmd: str) -> None:
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    (directory / "comm").write_text(comm + "\n", encoding="utf-8")
    (directory / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
    (directory / "status").write_text(
        f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nVmRSS:\t{rss_kib} kB\n",
        encoding="utf-8",
    )


@pytest.mark.fr("NFR-001")
def test_ginn_processes_fail_the_idle_check_and_the_supervisor_is_bounded(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    proc = tmp_path / "proc"
    _fake_process(proc, 101, "ginn-wait", uid, 1000, "ginn-wait 1 2")
    _fake_process(proc, 102, "runsvdir", uid, 900, "runsvdir /home/u/.local/state/ginn/sv")
    _fake_process(proc, 103, "catatonit", uid, 700, "catatonit -P")
    _fake_process(proc, 104, "ginn", uid + 1, 500, "ginn status")
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-I", str(NFR_001), "--proc", str(proc)],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    lines = _json_lines(done.stdout)
    assert lines[0]["value"] == 1.0 and lines[0]["verdict"] == "FAIL"
    assert lines[1]["verdict"] == "PASS" and lines[1]["value"] < 1.0
    assert "ginn process 101: ginn-wait" in done.stderr
    assert "1 catatonit (podman pause) process(es), not counted" in done.stderr


@pytest.mark.fr("NFR-001")
def test_an_idle_host_with_no_supervisor_is_not_run_for_the_supervisor(tmp_path: Path) -> None:
    uid = os.getuid()
    proc = tmp_path / "proc"
    _fake_process(proc, 201, "bash", uid, 3000, "bash")
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-I", str(NFR_001), "--proc", str(proc)],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2, done.stdout + done.stderr
    lines = _json_lines(done.stdout)
    assert lines[0]["verdict"] == "PASS" and lines[0]["value"] == 0.0
    assert lines[1]["verdict"] == "NOT RUN"


@pytest.mark.fr("NFR-011")
def test_the_integrity_script_times_a_small_synthetic_scope(tmp_path: Path) -> None:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-I", str(NFR_011), "--files", "30", "--runs", "2"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    entry = _json_lines(done.stdout)[0]
    assert entry["nfr"] == "NFR-011" and entry["unit"] == "s"
    assert entry["value"] is not None and entry["value"] > 0
    assert "30 files in scope" in entry["metric"]


@pytest.mark.fr("NFR-011")
def test_the_integrity_script_refuses_an_empty_scope(tmp_path: Path) -> None:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-I", str(NFR_011), "--files", "0"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2


# --- NFR-006, NFR-008, NFR-009 and NFR-010: scripts that run ginn --------------------------------
#
# Each test puts a fake `ginn` (and, for NFR-006, fake benchmarks) on a private PATH. The fakes
# log their argv and print fixture numbers. No test runs podman, a real benchmark or a real realm.


def _fake(path: Path, body: str) -> Path:
    """Write an executable Python script that runs under the interpreter of the tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _script(script: Path, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-I", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _load_script(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look the module up by name
    sys.path.insert(0, str(MEASURE))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(MEASURE))
    return module


FAKE_EXEC_GINN = """
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:1] == ["exec"]:
    os.environ["FAKE_SIDE"] = "realm"
    rest = args[args.index("--") + 1:]
    os.execvp(rest[0], rest)
sys.exit(3)
"""

FAKE_7Z = """
import os
side = os.environ.get("FAKE_SIDE", "host")
if not os.environ.get("FAKE_NO_BANNER_" + side):
    print("7-Zip (z) " + os.environ.get("FAKE_7Z_VERSION_" + side, "23.01") + " (x64)")
print("Tot:           100   %s  %s" % (1, os.environ["FAKE_7Z_" + side]))
"""

FAKE_GLMARK2 = """
import os, sys
side = os.environ.get("FAKE_SIDE", "host")
if os.environ.get("FAKE_GL_FAIL"):
    sys.stderr.write("Error: cannot open display\\n")
    sys.exit(1)
print("    glmark2 2023.01")
print("                                  glmark2 Score: " + os.environ["FAKE_GL_" + side])
"""


def _bench_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    _fake(bin_dir / "ginn", FAKE_EXEC_GINN)
    _fake(bin_dir / "7z", FAKE_7Z)
    _fake(bin_dir / "glmark2", FAKE_GLMARK2)
    env = _env(tmp_path, FAKE_LOG=str(tmp_path / "ginn.log"), **extra)
    env["PATH"] = os.pathsep.join([str(bin_dir), env["PATH"]])
    return env


@pytest.mark.fr("NFR-006")
def test_the_benchmark_scores_are_read_from_the_tool_output() -> None:
    nfr6 = _load_script(NFR_006, "nfr006")
    seven = "Avr:  5 6 7\nTot:         100   3883  120948\n"
    assert nfr6.score_sevenzip(seven) == 120948.0
    assert nfr6.score_sevenzip("nothing") is None
    assert nfr6.score_glmark2("   glmark2 Score: 4821 \n") == 4821.0
    assert nfr6.score_glmark2("") is None


@pytest.mark.fr("NFR-006")
@pytest.mark.parametrize(
    ("realm_cpu", "realm_gpu", "cpu", "gpu", "code"),
    [
        ("98000", "980", "PASS", "PASS", 0),  # 2% short on both
        ("120000", "1100", "PASS", "PASS", 0),  # faster than the host: negative shortfall
        ("90000", "980", "FAIL", "PASS", 1),  # 10% short on the CPU
        ("100000", "960", "PASS", "FAIL", 1),  # 4% short on the GPU
    ],
)
def test_the_benchmark_shortfall_is_judged_against_three_percent(
    tmp_path: Path, realm_cpu: str, realm_gpu: str, cpu: str, gpu: str, code: int
) -> None:
    env = _bench_env(
        tmp_path,
        FAKE_7Z_host="100000",
        FAKE_7Z_realm=realm_cpu,
        FAKE_GL_host="1000",
        FAKE_GL_realm=realm_gpu,
    )
    done = _script(NFR_006, ["--realm", "arch", "--runs", "5"], env)
    lines = _json_lines(done.stdout)
    assert [entry["verdict"] for entry in lines] == [cpu, gpu], done.stdout + done.stderr
    assert done.returncode == code
    assert lines[0]["nfr"] == "NFR-006"
    assert lines[0]["unit"] == "%"
    assert "median of 5 runs" in lines[0]["metric"]
    assert "note" not in lines[0] or "SRS asks for" not in lines[0]["note"]


@pytest.mark.fr("NFR-006")
def test_the_realm_runs_go_through_ginn_exec_with_an_argv_list(tmp_path: Path) -> None:
    env = _bench_env(
        tmp_path, FAKE_7Z_host="1", FAKE_7Z_realm="1", FAKE_GL_host="1", FAKE_GL_realm="1"
    )
    done = _script(NFR_006, ["--realm", "arch", "--runs", "2"], env)
    assert done.returncode == 2, done.stdout + done.stderr  # 2 runs of 5: NOT RUN
    calls = [json.loads(row) for row in (tmp_path / "ginn.log").read_text().splitlines()]
    assert calls[:2] == [["exec", "arch", "--", "7z", "b"]] * 2
    assert calls[2:] == [["exec", "arch", "--", "glmark2"]] * 2
    assert "2 run(s) of the 5" in _json_lines(done.stdout)[0]["note"]


@pytest.mark.fr("NFR-006")
def test_a_failing_gpu_leg_and_a_build_mismatch_are_not_run(tmp_path: Path) -> None:
    env = _bench_env(
        tmp_path,
        FAKE_7Z_host="1",
        FAKE_7Z_realm="1",
        FAKE_7Z_VERSION_realm="22.00",
        FAKE_GL_FAIL="1",
    )
    done = _script(NFR_006, ["--realm", "arch", "--runs", "1"], env)
    cpu, gpu = _json_lines(done.stdout)
    assert (cpu["verdict"], gpu["verdict"]) == (emit.NOT_RUN, emit.NOT_RUN)
    assert cpu["value"] is None and "not the same build" in cpu["note"]
    assert "display" in gpu["note"]
    assert done.returncode == 2


@pytest.mark.fr("NFR-006")
@pytest.mark.parametrize(
    ("extra", "runs", "reason"),
    [
        ({"FAKE_NO_BANNER_realm": "1"}, "1", "no version banner"),
        ({"FAKE_NO_BANNER_host": "1"}, "1", "no version banner"),
        ({"FAKE_7Z_host": "0", "FAKE_7Z_realm": "0"}, "5", "not positive"),
        ({}, "4", "4 run(s) of the 5"),
    ],
)
def test_an_unjudgeable_cpu_leg_is_not_run(
    tmp_path: Path, extra: dict[str, str], runs: str, reason: str
) -> None:
    base = {
        "FAKE_7Z_host": "100",
        "FAKE_7Z_realm": "100",
        "FAKE_GL_host": "1",
        "FAKE_GL_realm": "1",
    }
    env = _bench_env(tmp_path, **{**base, **extra})
    done = _script(NFR_006, ["--realm", "arch", "--runs", runs, "--skip-gpu"], env)
    cpu = _json_lines(done.stdout)[0]
    assert cpu["verdict"] == emit.NOT_RUN and cpu["value"] is None, done.stdout
    assert reason in cpu["note"]


@pytest.mark.fr("NFR-006")
def test_skipping_the_gpu_leg_still_prints_a_not_run_line(tmp_path: Path) -> None:
    env = _bench_env(tmp_path, FAKE_7Z_host="5", FAKE_7Z_realm="5")
    done = _script(NFR_006, ["--realm", "arch", "--runs", "5", "--skip-gpu"], env)
    cpu, gpu = _json_lines(done.stdout)
    assert (cpu["verdict"], gpu["verdict"]) == (emit.PASS, emit.NOT_RUN)
    assert done.returncode == 2


FAKE_STATUS_GINN = """
import json, os, sys
if sys.argv[1:] != ["--json", "status", "arch", "--sizes"]:
    sys.exit(3)
if os.environ.get("FAKE_FAIL"):
    sys.exit(1)
sys.stdout.write(os.environ["FAKE_STATUS"] + "\\n")
"""


def _status_env(tmp_path: Path, payload: dict[str, Any], **extra: str) -> dict[str, str]:
    _fake(tmp_path / "bin" / "ginn", FAKE_STATUS_GINN)
    env = _env(tmp_path, FAKE_STATUS=json.dumps(payload), **extra)
    env["PATH"] = os.pathsep.join([str(tmp_path / "bin"), env["PATH"]])
    return env


def _disk(
    home_mib: float,
    container_mib: float | None,
    partial: bool = False,
    image_mib: float | None = 100,
    base_mib: float | None = 100,
) -> dict[str, Any]:
    mib = 1024 * 1024

    def to_bytes(value: float | None) -> int | None:
        return None if value is None else int(value * mib)

    return {
        "name": "arch",
        "disk": {
            "realm_home_bytes": to_bytes(home_mib),
            "realm_home_partial": partial,
            "container_bytes": to_bytes(container_mib),
            "image_bytes": to_bytes(image_mib),
            "base_image_bytes": to_bytes(base_mib),
        },
    }


@pytest.mark.fr("NFR-008")
@pytest.mark.parametrize(
    ("payload", "verdict", "value", "code"),
    [
        (_disk(5, 10, image_mib=100, base_mib=100), emit.PASS, 15.0, 0),
        (_disk(100, 100, image_mib=100, base_mib=100), emit.PASS, 200.0, 0),
        (_disk(101, 100, image_mib=100, base_mib=100), emit.FAIL, 201.0, 1),
        (_disk(0, 0, image_mib=80, base_mib=100), emit.PASS, -20.0, 0),
    ],
)
def test_the_total_is_judged_against_the_base_image_plus_200_mib(
    tmp_path: Path, payload: dict[str, Any], verdict: str, value: float, code: int
) -> None:
    # total = derived image + container layer + Realm Home; target = pinned base + 200 MiB.
    done = _script(NFR_008, ["--realm", "arch"], _status_env(tmp_path, payload))
    (entry,) = _json_lines(done.stdout)
    assert entry["verdict"] == verdict and entry["value"] == value
    assert "official image" in entry["note"]
    assert done.returncode == code


@pytest.mark.fr("NFR-008")
@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        (_disk(5, None), "container_bytes"),
        (_disk(5, 1, image_mib=None), "image_bytes"),
        (_disk(5, 1, base_mib=None), "base_image_bytes"),
    ],
)
def test_a_null_figure_is_not_run_and_names_it(
    tmp_path: Path, payload: dict[str, Any], missing: str
) -> None:
    done = _script(NFR_008, ["--realm", "arch"], _status_env(tmp_path, payload))
    (entry,) = _json_lines(done.stdout)
    assert entry["verdict"] == emit.NOT_RUN and entry["value"] is None
    assert missing in entry["note"]
    assert done.returncode == 2


@pytest.mark.fr("NFR-008")
@pytest.mark.parametrize(
    ("payload", "fail", "reason"),
    [
        (_disk(5, 1, partial=True), "", "partial"),
        ({"name": "arch", "disk": None}, "", "no disk object"),
        (_disk(5, 1), "1", "exited 1"),
    ],
)
def test_an_unreadable_status_is_still_not_run_with_its_reason(
    tmp_path: Path, payload: dict[str, Any], fail: str, reason: str
) -> None:
    env = _status_env(tmp_path, payload, **({"FAKE_FAIL": fail} if fail else {}))
    done = _script(NFR_008, ["--realm", "arch"], env)
    (entry,) = _json_lines(done.stdout)
    assert entry["verdict"] == emit.NOT_RUN and reason in entry["note"]
    assert done.returncode == 2


@pytest.mark.fr("NFR-009")
def test_the_p95_line_keeps_the_verdict_and_notes_short_series_and_timeouts() -> None:
    line = emit.p95_line(
        "NFR-009", "m", [0.1, 0.2, 0.3], unit="s", target="t", limit=5, runs_required=20
    )
    assert line["verdict"] == emit.PASS and line["value"] == 0.3
    assert "3 run(s) of the 20" in line["note"]
    late = emit.p95_line(
        "NFR-009", "m", [0.1, 6.0], unit="s", target="t", limit=5, runs_required=2, timeouts=1
    )
    assert late["verdict"] == emit.FAIL and "1 run(s) timed out" in late["note"]
    none = emit.p95_line("NFR-009", "m", [], unit="s", target="t", limit=5, runs_required=2)
    assert none["verdict"] == emit.NOT_RUN and none["value"] is None


FAKE_EXPORT_GINN = """
import json, os, subprocess, sys
from pathlib import Path
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:1] == ["exec"]:
    args = [args[args.index("--") + 3], *args[args.index("--") + 4:]]  # drop PM and its flags
    args = ["install" if "-S" in sys.argv else "remove", "arch", *args]
verb = args[0]
entry = Path(os.environ["FAKE_APPS"]) / "ginn-arch-htop.desktop"
delay = os.environ.get("FAKE_DELAY", "0")
action = "entry.touch()" if verb == "install" else "entry.unlink(missing_ok=True)"
code = "import time; from pathlib import Path; time.sleep(%s); entry = Path(%r); %s" % (
    delay, str(entry), action)
subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
"""


def _export_env(tmp_path: Path, **extra: str) -> tuple[dict[str, str], Path]:
    apps = tmp_path / "apps"
    apps.mkdir(exist_ok=True)
    _fake(tmp_path / "bin" / "ginn", FAKE_EXPORT_GINN)
    env = _env(tmp_path, FAKE_LOG=str(tmp_path / "ginn.log"), FAKE_APPS=str(apps), **extra)
    env["PATH"] = os.pathsep.join([str(tmp_path / "bin"), env["PATH"]])
    return env, apps


@pytest.mark.fr("NFR-009")
def test_export_and_unexport_latency_follow_the_entry_file(tmp_path: Path) -> None:
    env, apps = _export_env(tmp_path, FAKE_DELAY="0.3")
    args = ["--realm", "arch", "--package", "htop", "--app", "htop", "--runs", "2"]
    done = _script(NFR_009, [*args, "--applications", str(apps)], env)
    export, unexport = _json_lines(done.stdout)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "export latency of ginn-arch-htop.desktop" in export["metric"]
    assert unexport["metric"].startswith("unexport latency")
    for entry in (export, unexport):
        assert 0.05 < entry["value"] < 2.0, entry
        assert "2 run(s) of the 20" in entry["note"]
    calls = [json.loads(row) for row in (tmp_path / "ginn.log").read_text().splitlines()]
    assert calls == [["install", "arch", "htop"], ["remove", "arch", "htop"]] * 2
    assert not list(apps.iterdir())


@pytest.mark.fr("NFR-009")
def test_the_native_path_runs_the_package_manager_through_ginn_exec(tmp_path: Path) -> None:
    env, apps = _export_env(tmp_path)
    args = ["--realm", "arch", "--package", "htop", "--app", "htop", "--runs", "1"]
    args += ["--via", "native", "--pm-install", "pacman -S --noconfirm"]
    args += ["--pm-remove", "pacman -R --noconfirm", "--applications", str(apps)]
    done = _script(NFR_009, args, env)
    assert done.returncode == 0, done.stdout + done.stderr
    calls = [json.loads(row) for row in (tmp_path / "ginn.log").read_text().splitlines()]
    assert calls[0] == ["exec", "--root", "arch", "--", "pacman", "-S", "--noconfirm", "htop"]
    assert calls[1] == ["exec", "--root", "arch", "--", "pacman", "-R", "--noconfirm", "htop"]
    assert "via native" in _json_lines(done.stdout)[0]["note"]


@pytest.mark.fr("NFR-009")
def test_an_entry_that_never_appears_counts_at_the_timeout(tmp_path: Path) -> None:
    nfr9 = _load_script(NFR_009, "nfr009")
    entry = tmp_path / "ginn-arch-htop.desktop"
    quiet = [sys.executable, "-c", "pass"]
    series = nfr9.measure(quiet, quiet, entry, 2, 0.1)
    assert series.exported == [0.1, 0.1] and series.error is None
    assert (series.export_timeouts, series.unexport_timeouts) == (2, 0)
    assert series.unexported[0] < 0.1  # the entry was never there, so it is gone at once


@pytest.mark.fr("NFR-009")
def test_a_failed_remove_keeps_the_data_and_tries_one_cleanup_remove(tmp_path: Path) -> None:
    nfr9 = _load_script(NFR_009, "nfr009b")
    entry = tmp_path / "ginn-arch-htop.desktop"
    log = tmp_path / "remove.log"
    touch = [sys.executable, "-c", f"open({str(entry)!r}, 'w').close()"]
    bad_remove = [
        sys.executable,
        "-c",
        f"open({str(log)!r}, 'a').write('x'); raise SystemExit(1)",
    ]
    series = nfr9.measure(touch, bad_remove, entry, 3, 0.2)
    assert series.exported == [pytest.approx(0, abs=0.1)] and series.unexported == []
    assert "exited 1" in series.error and "may still be installed" in series.error
    assert log.read_text() == "xx"  # the failed remove, then the one cleanup attempt


@pytest.mark.fr("NFR-009")
def test_an_entry_that_stays_between_runs_stops_the_series_before_the_next_install(
    tmp_path: Path,
) -> None:
    nfr9 = _load_script(NFR_009, "nfr009c")
    entry = tmp_path / "ginn-arch-htop.desktop"
    installs = tmp_path / "installs.log"
    touch = [
        sys.executable,
        "-c",
        f"open({str(entry)!r}, 'w').close(); open({str(installs)!r}, 'a').write('i')",
    ]
    keeps = [sys.executable, "-c", "pass"]  # a remove that leaves the entry behind
    series = nfr9.measure(touch, keeps, entry, 3, 0.2)
    assert installs.read_text() == "i"  # no second install on top of a leftover entry
    assert "still exists before an install" in series.error
    assert len(series.exported) == 1 and series.unexport_timeouts == 1


@pytest.mark.fr("NFR-009")
def test_a_series_that_stopped_early_cannot_pass(tmp_path: Path) -> None:
    env, apps = _export_env(tmp_path)
    (apps / "leftover").touch()
    fake = tmp_path / "bin" / "ginn"
    fake.write_text(
        fake.read_text().replace(
            "verb = args[0]", "verb = args[0]\nif verb == 'remove': sys.exit(1)"
        )
    )
    args = ["--realm", "arch", "--package", "htop", "--app", "htop", "--runs", "20"]
    done = _script(NFR_009, [*args, "--applications", str(apps)], env)
    export, unexport = _json_lines(done.stdout)
    assert export["verdict"] == emit.NOT_RUN and export["value"] is None
    assert "stopped early" in export["note"] and "not judged" in export["note"]
    assert unexport["verdict"] == emit.NOT_RUN
    assert done.returncode == 2


@pytest.mark.fr("NFR-009")
def test_an_entry_that_exists_before_the_first_run_is_not_run(tmp_path: Path) -> None:
    env, apps = _export_env(tmp_path)
    (apps / "ginn-arch-htop.desktop").touch()
    args = ["--realm", "arch", "--package", "htop", "--app", "htop", "--runs", "1"]
    done = _script(NFR_009, [*args, "--applications", str(apps)], env)
    lines = _json_lines(done.stdout)
    assert [entry["verdict"] for entry in lines] == [emit.NOT_RUN, emit.NOT_RUN]
    assert "exists before the first run" in lines[0]["note"]
    assert not (tmp_path / "ginn.log").exists()  # nothing was installed
    assert done.returncode == 2


@pytest.mark.fr("NFR-009")
def test_the_native_path_needs_both_package_manager_commands(tmp_path: Path) -> None:
    env, _ = _export_env(tmp_path)
    args = ["--realm", "arch", "--package", "htop", "--app", "htop", "--via", "native"]
    done = _script(NFR_009, args, env)
    assert done.returncode == 2 and "--pm-install" in done.stderr


FAKE_GUI_GINN = """
import json, os, sys
from pathlib import Path
state = Path(os.environ["XDG_STATE_HOME"]) / "ginn" / "realms"
record = state / "arch.json"
args = sys.argv[1:]
if args == ["--json", "list"]:
    now = json.loads(record.read_text())["state"] if record.exists() else None
    realms = [] if now is None else [{"name": "arch", "state": now}]
    sys.stdout.write(json.dumps({"realms": realms}) + "\\n")
elif args[:1] == ["--json"] and args[1] in ("start", "stop"):
    import time
    time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
    counter = Path(os.environ["XDG_STATE_HOME"]) / "count"
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    if str(count) == os.environ.get("FAKE_FAIL_ON_RUN"):
        sys.exit(1)
    state.mkdir(parents=True, exist_ok=True)
    tmp = state / "arch.tmp"
    new = "running" if args[1] == "start" else "stopped"
    tmp.write_text(json.dumps({"state": new}))
    os.replace(tmp, record)
    sys.stdout.write(json.dumps({"name": "arch", "state": new}) + "\\n")
else:
    sys.exit(3)
"""


def _gui_env(tmp_path: Path, initial: str | None) -> dict[str, str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    state = tmp_path / "state"
    realms = state / "ginn" / "realms"
    realms.mkdir(parents=True, exist_ok=True)
    if initial is not None:
        (realms / "arch.json").write_text(json.dumps({"state": initial}))
    fake = _fake(tmp_path / "fake-ginn", FAKE_GUI_GINN)
    return _env(
        tmp_path,
        XDG_STATE_HOME=str(state),
        GINN_RUNTIME_DIR=str(runtime),
        PYTHONPATH=str(REPO / "src"),
        FAKE=str(fake),
    )


@pytest.mark.fr("NFR-010")
@pytest.mark.parametrize("initial", ["running", "stopped"])
def test_the_gui_probe_sees_each_cli_change_through_the_file_monitor(
    tmp_path: Path, initial: str
) -> None:
    pytest.importorskip("gi")
    env = _gui_env(tmp_path, initial)
    args = ["--realm", "arch", "--runs", "4", "--ginn", f"{sys.executable} {env['FAKE']}"]
    done = _script(NFR_010, args, env)
    (entry,) = _json_lines(done.stdout)
    assert entry["nfr"] == "NFR-010" and entry["unit"] == "s", done.stdout + done.stderr
    assert entry["verdict"] == emit.PASS and done.returncode == 0
    assert 0.0 <= entry["value"] < 1.0
    assert "4 runs" in entry["metric"] and "50 the SRS asks for" in entry["note"]
    assert "no GUI probe hook" in entry["note"]
    state = json.loads((tmp_path / "state" / "ginn" / "realms" / "arch.json").read_text())
    assert state["state"] == initial  # an even number of runs leaves the realm where it began


@pytest.mark.fr("NFR-010")
def test_the_gui_probe_is_not_run_for_an_unknown_realm(tmp_path: Path) -> None:
    pytest.importorskip("gi")
    env = _gui_env(tmp_path, None)
    args = ["--realm", "arch", "--runs", "1", "--ginn", f"{sys.executable} {env['FAKE']}"]
    done = _script(NFR_010, args, env)
    (entry,) = _json_lines(done.stdout)
    assert entry["verdict"] == emit.NOT_RUN and entry["value"] is None
    assert done.returncode == 2


@pytest.mark.fr("NFR-010")
def test_the_wait_starts_when_the_command_returns_not_when_it_starts(tmp_path: Path) -> None:
    pytest.importorskip("gi")
    env = _gui_env(tmp_path, "running")
    env["FAKE_DELAY"] = "1.6"  # longer than --timeout, shorter than the command cap
    args = ["--realm", "arch", "--runs", "2", "--timeout", "1.2"]
    args += ["--ginn", f"{sys.executable} {env['FAKE']}"]
    done = _script(NFR_010, args, env)
    (entry,) = _json_lines(done.stdout)
    assert "timed out" not in entry.get("note", ""), done.stdout + done.stderr
    assert entry["value"] is not None and entry["value"] < 1.0


@pytest.mark.fr("NFR-010")
def test_a_series_that_stopped_early_is_not_run(tmp_path: Path) -> None:
    pytest.importorskip("gi")
    env = _gui_env(tmp_path, "running")
    env["FAKE_FAIL_ON_RUN"] = "2"
    args = ["--realm", "arch", "--runs", "4", "--ginn", f"{sys.executable} {env['FAKE']}"]
    done = _script(NFR_010, args, env)
    (entry,) = _json_lines(done.stdout)
    assert entry["verdict"] == emit.NOT_RUN and entry["value"] is None, done.stdout
    assert "stopped early" in entry["note"] and "not judged" in entry["note"]
    assert done.returncode == 2


@pytest.mark.fr("NFR-010")
def test_a_fail_stays_a_fail_when_the_series_stopped_early() -> None:
    failing = emit.line("NFR-010", "m", 3.0, "s", "t", emit.FAIL, "n")
    assert emit.cap_at_not_run(failing, "stopped early: x") is failing
    passing = emit.line("NFR-010", "m", 0.2, "s", "t", emit.PASS)
    capped = emit.cap_at_not_run(passing, "stopped early: x")
    assert capped["verdict"] == emit.NOT_RUN and capped["value"] is None
    assert passing["verdict"] == emit.PASS  # the input is not changed
