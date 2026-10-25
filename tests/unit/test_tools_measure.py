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
