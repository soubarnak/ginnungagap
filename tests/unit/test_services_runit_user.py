# SPDX-License-Identifier: Apache-2.0
"""The per-realm runit service directory and its supervision helpers (FR-081, AS-028, NFR-033).

Rendering is pure, so the golden files under tests/fixtures/services/ pin the scripts byte for
byte. The shell tests run the rendered scripts with `sh`, with a stand-in for the ginn command
that records its argv. No runsv, no podman and no real ginn runs here. The FIFO case stands in
for runsv's control pipe, with a reader that the test holds.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from ginn.domain import RealmName
from ginn.runtime.spec import (
    JOURNALD_DROPIN_TEXT,
    SERVICES_BASE_MASKED_UNITS,
    SERVICES_LOGIND_UNIT,
    SERVICES_MASKED_UNITS,
)
from ginn.services import supervise
from ginn.services.runit_user import (
    DOWN,
    FINISH,
    RUN,
    conmon_pidfile,
    crash_log,
    journald_dropin,
    mark_down,
    remove_service,
    render,
    service_dir,
    write_journald_dropin,
    write_service_dir,
)
from ginn.store.paths import OwnedPaths

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "services"
EDGE_REALM = "a-0-"
"""A valid realm name with the edge characters: a digit, a hyphen and a trailing hyphen."""
EDGE_PROGRAM = "/opt/gin's tools/ginn"
"""An absolute ginn path with a space and a single quote, which must stay one shell word."""
MASKS_FILE = REPO / "spikes" / "s05" / "masks-base.txt"
LOGIND_MASKS_FILE = REPO / "spikes" / "s05" / "masks-logind.txt"

pytestmark = [pytest.mark.fr("FR-081"), pytest.mark.fr("AS-028")]


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OwnedPaths:
    """Owned Paths under tmp_path, with the XDG roots and the runtime pinned there."""
    root = tmp_path
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    (root / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(root / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    return resolved


@pytest.mark.fr("NFR-033")
def test_the_rendered_scripts_match_the_golden_files() -> None:
    rendered = render(RealmName(EDGE_REALM), EDGE_PROGRAM)
    assert set(rendered) == {RUN, FINISH}
    for file_name, text in rendered.items():
        expected = (FIXTURES / EDGE_REALM / file_name).read_text(encoding="utf-8")
        assert text == expected, file_name


def test_each_value_is_one_single_quoted_shell_word() -> None:
    run = render(RealmName(EDGE_REALM), EDGE_PROGRAM)[RUN]
    assert "exec '/opt/gin'\\''s tools/ginn' _service-run 'a-0-'\n" in run
    finish = render(RealmName(EDGE_REALM), EDGE_PROGRAM)[FINISH]
    tail = "exec '/opt/gin'\\''s tools/ginn' _service-finish 'a-0-' \"$1\" \"$2\"\n"
    assert finish.endswith(tail)


@pytest.mark.parametrize("program", ["ginn", "", "/opt/ginn\nrm -rf /", "/opt/ginn\0"])
def test_a_ginn_command_that_is_not_an_absolute_plain_path_is_refused(program: str) -> None:
    with pytest.raises(ValueError):
        render(RealmName(EDGE_REALM), program)


@pytest.mark.parametrize("file_name", [RUN, FINISH])
def test_the_rendered_scripts_are_valid_posix_sh(tmp_path: Path, file_name: str) -> None:
    text = render(RealmName(EDGE_REALM), EDGE_PROGRAM)[file_name]
    script = tmp_path / file_name
    script.write_text(text, encoding="utf-8")
    checked = subprocess.run(  # noqa: S603 - fixed argv for sh, no user input
        ["sh", "-n", str(script)],  # noqa: S607 - fixed argv, no user input
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize("file_name", [RUN, FINISH])
def test_the_scripts_hand_the_realm_and_the_arguments_to_ginn_as_separate_words(
    tmp_path: Path, file_name: str
) -> None:
    """A stand-in ginn at a path with a space and a quote records its argv, so quoting is run."""
    tools = tmp_path / "gin's tools"
    tools.mkdir()
    recorder = tools / "ginn"
    recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORD"\n', encoding="utf-8")
    recorder.chmod(0o755)
    record = tmp_path / "argv.txt"
    script = tmp_path / file_name
    script.write_text(render(RealmName(EDGE_REALM), str(recorder))[file_name], encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "RECORD": str(record)}
    args = ["-1", "9"] if file_name == FINISH else []
    subprocess.run(  # noqa: S603 - fixed argv for sh, the script is ours
        ["sh", str(script), *args],  # noqa: S607 - fixed argv, the script is ours
        env=env,
        check=True,
    )
    words = record.read_text(encoding="utf-8").splitlines()
    if file_name == RUN:
        assert words == ["_service-run", EDGE_REALM]
    else:
        assert words == ["_service-finish", EDGE_REALM, "-1", "9"]


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
@pytest.mark.parametrize("file_name", [RUN, FINISH])
def test_the_scripts_pass_shellcheck(tmp_path: Path, file_name: str) -> None:
    script = tmp_path / file_name
    script.write_text(render(RealmName(EDGE_REALM), EDGE_PROGRAM)[file_name], encoding="utf-8")
    checked = subprocess.run(  # noqa: S603 - fixed argv for shellcheck, no user input
        ["shellcheck", "--shell=sh", str(script)],  # noqa: S607 - fixed argv, no user input
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_write_service_dir_installs_run_finish_and_down_at_once(paths: OwnedPaths) -> None:
    name = RealmName(EDGE_REALM)
    target = write_service_dir(paths, name, EDGE_PROGRAM)
    assert target == service_dir(paths, name) == paths.sv / EDGE_REALM
    assert sorted(entry.name for entry in target.iterdir()) == [DOWN, FINISH, RUN]
    for file_name in (RUN, FINISH):
        mode = stat.S_IMODE((target / file_name).stat().st_mode)
        assert mode == 0o755, file_name
        text = (target / file_name).read_text(encoding="utf-8")
        assert text == render(name, EDGE_PROGRAM)[file_name]
    assert (target / DOWN).read_bytes() == b""
    assert not (paths.sv / f".{EDGE_REALM}.tmp").exists()
    assert conmon_pidfile(paths, name).parent.is_dir()
    assert crash_log(paths, name).parent.is_dir()


def test_an_existing_service_directory_is_refused_and_left_alone(paths: OwnedPaths) -> None:
    name = RealmName(EDGE_REALM)
    write_service_dir(paths, name, EDGE_PROGRAM)
    with pytest.raises(FileExistsError):
        write_service_dir(paths, name, "/usr/bin/ginn")
    assert (service_dir(paths, name) / RUN).read_text(encoding="utf-8") == render(
        name, EDGE_PROGRAM
    )[RUN]


def test_a_leftover_temporary_directory_does_not_block_an_install(paths: OwnedPaths) -> None:
    name = RealmName(EDGE_REALM)
    leftover = paths.sv / f".{EDGE_REALM}.tmp"
    leftover.mkdir(parents=True)
    (leftover / "stale").write_text("left by an interrupted install", encoding="utf-8")
    write_service_dir(paths, name, EDGE_PROGRAM)
    assert not leftover.exists()


def test_mark_down_creates_the_down_file_and_is_idempotent(paths: OwnedPaths) -> None:
    name = RealmName(EDGE_REALM)
    write_service_dir(paths, name, EDGE_PROGRAM)
    (service_dir(paths, name) / DOWN).unlink()
    mark_down(paths, name)
    mark_down(paths, name)
    assert (service_dir(paths, name) / DOWN).is_file()


def test_remove_service_removes_the_directory_and_the_per_realm_files(paths: OwnedPaths) -> None:
    name = RealmName(EDGE_REALM)
    write_service_dir(paths, name, EDGE_PROGRAM)
    conmon_pidfile(paths, name).write_text("123\n", encoding="utf-8")
    crash_log(paths, name).write_text("[]", encoding="utf-8")
    remove_service(paths, name)
    assert not service_dir(paths, name).exists()
    assert not conmon_pidfile(paths, name).exists()
    assert not crash_log(paths, name).exists()
    remove_service(paths, name)  # absent is a no-op


def test_remove_service_takes_a_directory_that_runsv_has_made_its_supervise_in(
    paths: OwnedPaths,
) -> None:
    name = RealmName(EDGE_REALM)
    write_service_dir(paths, name, EDGE_PROGRAM)
    supervise_dir = service_dir(paths, name) / "supervise"
    supervise_dir.mkdir()
    os.mkfifo(supervise_dir / "control")
    remove_service(paths, name)
    assert not service_dir(paths, name).exists()


def test_the_journald_drop_in_is_the_adr_007_text_under_the_data_root(paths: OwnedPaths) -> None:
    path = write_journald_dropin(paths)
    assert path == journald_dropin(paths) == paths.data / "services" / "journald-ginn.conf"
    assert path.read_text(encoding="utf-8") == JOURNALD_DROPIN_TEXT
    assert JOURNALD_DROPIN_TEXT == "[Journal]\nStorage=volatile\nRuntimeMaxUse=8M\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_the_masked_units_are_the_ten_s05_base_units_and_logind() -> None:
    """ADR-007 mitigation b: the ten base units of the S-05 file, then logind (CL-28)."""

    def listed(path: Path) -> tuple[str, ...]:
        return tuple(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    assert listed(MASKS_FILE) == SERVICES_BASE_MASKED_UNITS
    assert listed(LOGIND_MASKS_FILE) == (SERVICES_LOGIND_UNIT,)
    assert SERVICES_MASKED_UNITS == (*SERVICES_BASE_MASKED_UNITS, SERVICES_LOGIND_UNIT)
    assert len(SERVICES_MASKED_UNITS) == 11


def test_the_crash_window_gives_up_on_the_fifth_crash_within_sixty_seconds() -> None:
    kept, give_up = supervise.note_crash([0.0, 10.0, 20.0, 30.0], 40.0)
    assert kept == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert give_up is True


def test_five_crashes_spanning_more_than_sixty_seconds_do_not_give_up() -> None:
    stamps: list[float] = []
    for now in (0.0, 15.0, 30.0, 45.0, 61.0):
        stamps, give_up = supervise.note_crash(stamps, now)
    assert give_up is False
    assert stamps == [0.0, 15.0, 30.0, 45.0, 61.0]


def test_only_the_last_five_crash_times_are_kept() -> None:
    stamps: list[float] = []
    for now in (0.0, 100.0, 200.0, 300.0, 400.0, 500.0, 600.0):
        stamps, _ = supervise.note_crash(stamps, now)
    assert stamps == [200.0, 300.0, 400.0, 500.0, 600.0]


def test_a_crash_log_that_cannot_be_read_counts_as_empty(tmp_path: Path) -> None:
    broken = tmp_path / "bad.crashes"
    broken.write_text("not json", encoding="utf-8")
    assert supervise.read_crashes(broken) == []
    assert supervise.read_crashes(tmp_path / "missing.crashes") == []
    broken.write_text('{"not": "a list"}', encoding="utf-8")
    assert supervise.read_crashes(broken) == []


def test_the_crash_log_round_trips_through_an_atomic_write(tmp_path: Path) -> None:
    log = tmp_path / "lab.crashes"
    supervise.write_crashes(log, [1.5, 2.5])
    assert supervise.read_crashes(log) == [1.5, 2.5]
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_signal_runsv_down_writes_the_down_byte_to_a_fifo_with_a_reader(tmp_path: Path) -> None:
    sdir = tmp_path / "lab"
    (sdir / "supervise").mkdir(parents=True)
    fifo = sdir / "supervise" / "control"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert supervise.signal_runsv_down(sdir) is True
        assert os.read(reader, 8) == b"d"
    finally:
        os.close(reader)


def test_signal_runsv_down_with_no_runsv_reading_is_not_an_error(tmp_path: Path) -> None:
    sdir = tmp_path / "lab"
    (sdir / "supervise").mkdir(parents=True)
    os.mkfifo(sdir / "supervise" / "control")  # no reader: the open fails with ENXIO
    assert supervise.signal_runsv_down(sdir) is False
    assert supervise.signal_runsv_down(tmp_path / "absent") is False
