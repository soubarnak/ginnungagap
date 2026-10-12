# SPDX-License-Identifier: Apache-2.0
"""Error catalogue checks against GINN-SRS-001 and the generated docs/errors.md (NFR-040)."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from ginn.errors import (
    CATALOGUE,
    EXEC_GINN_FAILURE,
    GinnError,
    Warning,
    make,
    placeholders,
    warn,
)

REPO = Path(__file__).resolve().parents[2]
SRS = REPO / "GINN-SRS-001-v1.0.md"
GENERATOR = REPO / "tools" / "gen_errors_doc.py"
CODE_PATTERN = re.compile(r"GINN-[EW]-[A-Z0-9-]+")

# The exit-code scheme. Changing a status is a deliberate edit here and in errors.py.
EXPECTED_EXIT: dict[str, int] = {
    "GINN-E-USAGE": 2,
    "GINN-E-PKG-NAME-INVALID": 2,
    "GINN-E-OPTION-INVALID": 2,
    "GINN-E-PATH-FORBIDDEN": 2,
    "GINN-E-NAME-INVALID": 2,
    "GINN-E-NO-SUCH-REALM": 3,
    "GINN-E-NAME-TAKEN": 4,
    "GINN-E-UNDO-CGROUP-BUSY": 75,
    "GINN-E-UNDO-REALMS-EXIST": 4,
    "GINN-E-UNDO-SERVICE-RUNNING": 75,
    "GINN-E-HOST-UNACKED": 4,
    "GINN-E-PKG-MANAGER": 1,
    "GINN-E-DOCTOR-FAILED": 1,
    "GINN-E-IMAGE-UNVERIFIED": 65,
    "GINN-E-PKG-SIGNATURE": 65,
    "GINN-E-NETWORK": 69,
    "GINN-E-DISK-SPACE": 73,
    "GINN-E-REALM-BUSY": 75,
    "GINN-E-SETUP-ROOT": 77,
    "GINN-E-HOST-NOT-SET-UP": 77,
    "GINN-E-RUNTIME-DIR-UNSAFE": 77,
    "GINN-E-SUBID-LOCK-UNSAFE": 77,
    "GINN-E-SUBID-LOCKED": 75,
    "GINN-E-NO-SUCH-USER": 2,
    "GINN-E-SUBID-CONFLICT": 4,
    "GINN-E-SETUP-PATH-UNSAFE": 77,
    "GINN-E-SETUP-BUSY": 75,
    "GINN-E-UNDO-SUPERVISE-STATE": 77,
    "GINN-E-INTERRUPTED": 130,
    "GINN-E-INTERNAL": 70,
    "GINN-E-DISTRO-DATA": 70,
    "GINN-E-TRUST-DATA": 70,
    "GINN-E-ATTACH-FAILED": 1,
    "GINN-E-RECOVERY-PENDING": 4,
    "GINN-W-HOST-CHANGED": 0,
    "GINN-W-REPLAY-MISSING": 0,
}


@pytest.mark.fr("NFR-040")
def test_every_srs_code_is_in_catalogue() -> None:
    codes = set(CODE_PATTERN.findall(SRS.read_text(encoding="utf-8")))
    assert codes, "no GINN-E-* or GINN-W-* codes found in the SRS"
    missing = sorted(codes - set(CATALOGUE))
    assert not missing, f"SRS codes missing from the catalogue: {missing}"


@pytest.mark.fr("NFR-040")
def test_every_entry_has_summary_remedy_and_source() -> None:
    for spec in CATALOGUE.values():
        assert spec.summary.strip(), spec.code
        assert spec.remedy.strip(), spec.code
        assert spec.source.strip(), spec.code
        assert "\n" not in spec.remedy, f"{spec.code}: remedy must be one line"


@pytest.mark.fr("NFR-040")
def test_warnings_exit_zero_and_errors_exit_non_zero() -> None:
    for spec in CATALOGUE.values():
        if spec.code.startswith("GINN-W-"):
            assert spec.exit_code == 0, spec.code
        else:
            assert spec.exit_code != 0, spec.code


@pytest.mark.fr("NFR-040")
def test_exit_codes_follow_the_scheme() -> None:
    assert {code: spec.exit_code for code, spec in CATALOGUE.items()} == EXPECTED_EXIT


@pytest.mark.fr("IR-011")
def test_exec_ginn_failure_is_reserved_and_unused() -> None:
    assert EXEC_GINN_FAILURE == 125
    assert EXEC_GINN_FAILURE not in {spec.exit_code for spec in CATALOGUE.values()}


@pytest.mark.fr("NFR-040")
@pytest.mark.parametrize("code", sorted(CATALOGUE))
def test_every_entry_formats_with_its_own_fields(code: str) -> None:
    """Catches placeholder typos: each entry is filled with dummy values for exactly its fields."""
    spec = CATALOGUE[code]
    names = placeholders(spec.summary) | placeholders(spec.remedy)
    for name in names:
        assert name.isidentifier(), f"{code}: placeholder {name!r} is not an identifier"
    values = {name: f"VALUE-{name}" for name in names}
    if spec.exit_code == 0:
        notice = warn(code, **values)
        assert isinstance(notice, Warning)
        text = notice.message + " " + notice.remedy
    else:
        err = make(code, **values)
        assert err.details == values
        text = err.message + " " + err.remedy
    for value in values.values():
        assert value in text, f"{code}: {value} missing from the formatted text"


@pytest.mark.fr("NFR-040")
def test_make_renders_message_remedy_and_details() -> None:
    err = make("GINN-E-NAME-TAKEN", name="arch")
    assert err.code == "GINN-E-NAME-TAKEN"
    assert err.message == "realm name 'arch' is already taken"
    assert err.exit_code == 4
    assert err.details == {"name": "arch"}


@pytest.mark.fr("NFR-040")
def test_make_rejects_missing_fields() -> None:
    with pytest.raises(ValueError, match=r"GINN-E-NAME-TAKEN.*missing fields \['name'\]"):
        make("GINN-E-NAME-TAKEN")


@pytest.mark.fr("NFR-040")
def test_make_rejects_unexpected_fields() -> None:
    with pytest.raises(ValueError, match=r"GINN-E-NAME-TAKEN.*unexpected fields \['extra'\]"):
        make("GINN-E-NAME-TAKEN", name="arch", extra="x")


@pytest.mark.fr("NFR-040")
def test_make_refuses_warning_codes() -> None:
    with pytest.raises(ValueError, match="GINN-W-REPLAY-MISSING"):
        make("GINN-W-REPLAY-MISSING", package="htop")


@pytest.mark.fr("NFR-040")
def test_warn_refuses_error_codes() -> None:
    with pytest.raises(ValueError, match="GINN-E-NAME-TAKEN"):
        warn("GINN-E-NAME-TAKEN", name="arch")


@pytest.mark.fr("NFR-040")
def test_warn_renders_message_and_remedy() -> None:
    notice = warn("GINN-W-HOST-CHANGED", paths="/etc/hosts")
    assert notice == Warning(
        "GINN-W-HOST-CHANGED",
        "host files changed since baseline: /etc/hosts",
        "review the listed paths; the likely cause is xbps or a system file edit made while ginn "
        "was running (R-004); then run 'ginn integrity --ack'",
    )


@pytest.mark.fr("NFR-040")
def test_ginn_error_refuses_exit_zero() -> None:
    with pytest.raises(ValueError, match="warning"):
        GinnError("GINN-E-TEST-ZERO", "message", "remedy", exit_code=0)


@pytest.mark.fr("NFR-040")
def test_ginn_error_refuses_a_catalogued_warning_code() -> None:
    with pytest.raises(ValueError, match="GINN-W-REPLAY-MISSING"):
        GinnError("GINN-W-REPLAY-MISSING", "message", "remedy", exit_code=1)


@pytest.mark.fr("DR-002")
def test_name_invalid_keeps_the_realm_name_pattern_literal() -> None:
    err = make("GINN-E-NAME-INVALID", name="Arch!")
    assert "^[a-z][a-z0-9-]{0,31}$" in err.message
    assert err.exit_code == 2


@pytest.mark.fr("NFR-040")
def test_placeholders_skip_escaped_braces() -> None:
    assert placeholders("pattern {{0,31}} and {name}") == frozenset({"name"})


@pytest.mark.fr("FR-122")
def test_recovery_pending_offers_resume_only_for_a_create() -> None:
    create = make("GINN-E-RECOVERY-PENDING", realm="arch", op="create")
    assert "run 'ginn create --resume arch' or 'ginn delete arch'" in create.remedy
    update = make("GINN-E-RECOVERY-PENDING", realm="arch", op="update")
    assert "otherwise run 'ginn doctor' and retry the update" in update.remedy
    assert "do not delete the realm" in update.remedy
    assert update.exit_code == 4


@pytest.mark.fr("DR-010")
def test_host_not_set_up_names_doctor_then_host_setup_then_the_supervisor() -> None:
    err = make("GINN-E-HOST-NOT-SET-UP", path="/run/ginn/1000")
    assert err.message == "runtime directory /run/ginn/1000 does not exist"
    assert err.remedy.startswith("run 'ginn doctor'; if Host Setup has not run")
    assert "sudo ginn host-setup --user <name>" in err.remedy
    assert "sudo sv status ginn-<user>" in err.remedy
    assert "\n" not in err.remedy


@pytest.mark.fr("DR-010")
def test_runtime_dir_unsafe_names_the_path_owner_and_mode() -> None:
    err = make("GINN-E-RUNTIME-DIR-UNSAFE", path="/run/ginn/1000", uid="1000", found="mode 755")
    assert err.code == "GINN-E-RUNTIME-DIR-UNSAFE"
    assert err.exit_code == 77
    assert err.message == (
        "runtime directory /run/ginn/1000 must be a real directory owned by uid 1000 with mode "
        "0700; found mode 755"
    )


@pytest.mark.fr("FR-001")
def test_subid_lock_unsafe_names_the_lock_gate_and_the_root_fix() -> None:
    lock = "/run/lock/ginn-subuid.lock"
    gate = "/run/lock/ginn-subuid.lock.reclaim"
    err = make("GINN-E-SUBID-LOCK-UNSAFE", lock=lock, path=gate)
    assert err.code == "GINN-E-SUBID-LOCK-UNSAFE"
    assert err.exit_code == 77
    assert err.message == (
        f"stale subordinate ID lock {lock} cannot be reclaimed: "
        f"its reclaim gate {gate} is a symbolic link"
    )
    assert f"as root, run 'ls -l {gate}'" in err.remedy
    assert f"'rm {gate}' to remove the link itself" in err.remedy
    assert "\n" not in err.remedy


@pytest.mark.fr("FR-001")
def test_subid_locked_names_the_holder_and_the_manual_lock_removal() -> None:
    lock = "/run/lock/ginn-subuid.lock"
    held = make("GINN-E-SUBID-LOCKED", lock=lock, holder="pid 4242")
    assert held.code == "GINN-E-SUBID-LOCKED"
    assert held.exit_code == 75
    assert held.message == f"subordinate ID lock {lock} is held by another process (pid 4242)"
    assert "check for a running 'ginn host-setup'" in held.remedy
    assert f"if none is running, remove {lock} by hand" in held.remedy
    assert "then run 'sudo ginn host-setup --user <name>' again" in held.remedy
    assert "\n" not in held.remedy
    unknown = make("GINN-E-SUBID-LOCKED", lock=lock, holder="no readable pid")
    assert "(no readable pid)" in unknown.message


@pytest.mark.fr("DR-040")
def test_trust_data_names_the_source_and_the_problem() -> None:
    err = make("GINN-E-TRUST-DATA", source="trust.toml", problem="schema 2 is not supported")
    assert err.code == "GINN-E-TRUST-DATA"
    assert err.exit_code == 70
    assert err.message == "shipped trust data trust.toml is invalid: schema 2 is not supported"
    assert err.details == {"source": "trust.toml", "problem": "schema 2 is not supported"}


@pytest.mark.fr("NFR-040")
def test_generated_errors_doc_is_current() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
