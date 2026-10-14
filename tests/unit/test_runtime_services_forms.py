# SPDX-License-Identifier: Apache-2.0
"""The services-on create-time forms: the conmon pidfile and ADR-007 mitigations a, b and d.

The argv is pure, so these cases pin it. Mitigations a and b ship by default since S-05s passed
(ADR-007, CL-23): the create plan sets the flag, and a bare spec's argv carries d only. With the
flag on, the argv carries the ginn-owned journald drop-in and one `/dev/null` bind per masked unit,
all read-only, and nothing under the realm's /etc is written (CR-007). NFR-003 is the memory budget
these forms serve.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from ginn.podman.adapter import CreateSpec
from ginn.runtime.spec import (
    JOURNALD_DROPIN_TARGET,
    SERVICES_MASKED_UNITS,
    SERVICES_MITIGATIONS,
    UNIT_DIR,
    create_argv,
    create_spec,
    mitigation_mounts,
    new_machine_id,
)
from tests.unit.test_runtime_spec import make_spec

DROPIN_SOURCE = "/home/alice/.local/share/ginn/services/journald-ginn.conf"
PIDFILE = "/run/ginn/1000/services/arch.conmon.pid"

pytestmark = [pytest.mark.fr("NFR-003"), pytest.mark.fr("FR-080")]


def _mount_values(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, word in enumerate(argv) if word == "--mount"]


def test_a_bare_services_on_spec_carries_mitigation_d_and_no_bind_forms() -> None:
    argv = create_argv(make_spec(services=True))
    assert not any("journald" in word for word in argv)
    assert not any("source=/dev/null" in word for word in argv)
    assert "SYSTEMD_LOG_LEVEL=warning" in argv, "mitigation d is on by default"


def test_the_create_plan_ships_mitigations_a_and_b_by_default() -> None:
    assert SERVICES_MITIGATIONS is True, "a and b ship since S-05s passed (CL-23, ADR-007)"


def test_the_services_on_argv_names_the_conmon_pidfile_after_the_cgroup_parent() -> None:
    argv = create_argv(make_spec(services=True))
    at = argv.index("--conmon-pidfile")
    assert argv[at + 1] == PIDFILE
    assert argv[at - 2 : at] == ["--cgroup-parent", "/ginn/1000/arch"]


def test_the_services_off_argv_has_no_conmon_pidfile() -> None:
    assert "--conmon-pidfile" not in create_argv(make_spec())


def test_with_the_mitigations_on_the_argv_binds_the_drop_in_read_only() -> None:
    spec = replace(
        make_spec(services=True),
        services_mitigations=True,
        journald_dropin=DROPIN_SOURCE,
    )
    values = _mount_values(create_argv(spec))
    expected = f"type=bind,source={DROPIN_SOURCE},target={JOURNALD_DROPIN_TARGET},ro"
    assert expected in values
    assert JOURNALD_DROPIN_TARGET == "/etc/systemd/journald.conf.d/ginn.conf"


def test_with_the_mitigations_on_each_masked_unit_gets_a_read_only_dev_null_bind() -> None:
    spec = replace(
        make_spec(services=True),
        services_mitigations=True,
        journald_dropin=DROPIN_SOURCE,
    )
    values = _mount_values(create_argv(spec))
    for unit in SERVICES_MASKED_UNITS:
        assert f"type=bind,source=/dev/null,target={UNIT_DIR}/{unit},ro" in values
    assert len([v for v in values if "source=/dev/null" in v]) == 11
    assert UNIT_DIR == "/etc/systemd/system"


def test_the_mitigation_mounts_follow_the_realms_own_mounts() -> None:
    spec = replace(
        make_spec(services=True),
        services_mitigations=True,
        journald_dropin=DROPIN_SOURCE,
    )
    mounts = create_spec(spec).mounts
    assert mounts[: len(spec.binds.mounts)] == list(spec.binds.mounts)
    assert mounts[len(spec.binds.mounts) :] == mitigation_mounts(spec)


def test_mitigation_mounts_are_empty_unless_the_spec_sets_them() -> None:
    assert mitigation_mounts(make_spec(services=True)) == []


def test_the_mitigations_need_the_journald_drop_in_path() -> None:
    with pytest.raises(ValueError, match="journald drop-in"):
        replace(make_spec(services=True), services_mitigations=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("conmon_pidfile", PIDFILE),
        ("services_mitigations", True),
        ("journald_dropin", DROPIN_SOURCE),
    ],
)
def test_a_services_off_realm_takes_no_services_field(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="services-on"):
        replace(make_spec(), **{field: value})


def test_the_conmon_pidfile_must_be_a_plain_absolute_path() -> None:
    base = create_spec(make_spec(services=True))
    for bad in ("relative/arch.pid", "/run/x,y.pid", "/run/x\n.pid"):
        with pytest.raises(ValueError, match="conmon pidfile"):
            CreateSpec(**{**base.__dict__, "conmon_pidfile": bad}).to_argv()


def test_the_pidfile_is_optional_on_the_adapter() -> None:
    base = create_spec(make_spec(services=True))
    argv = CreateSpec(**{**base.__dict__, "conmon_pidfile": None}).to_argv()
    assert "--conmon-pidfile" not in argv


def test_the_masks_in_the_argv_match_the_list_the_s05_spike_used() -> None:
    """The argv masks the S-05 base ten, then logind from the S-05 logind file (CL-28)."""
    spikes = Path(__file__).resolve().parents[2] / "spikes/s05"

    def listed(name: str) -> list[str]:
        lines = (spikes / name).read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.startswith("#")]

    assert list(SERVICES_MASKED_UNITS) == [*listed("masks-base.txt"), *listed("masks-logind.txt")]


@pytest.mark.fr("FR-080")
def test_a_new_machine_id_is_32_lowercase_hex_digits_and_differs_per_call() -> None:
    first, second = new_machine_id(), new_machine_id()
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert first != second
