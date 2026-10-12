# SPDX-License-Identifier: Apache-2.0
"""The `ginn doctor` report: text lines, the schema 1 JSON object, and the failure count.

The JSON shape is documented in the docstring of ginn.doctor.report, and these tests pin it. A
change to a key must come with a schema bump.
"""

from __future__ import annotations

import json

import pytest

from ginn.doctor import report
from ginn.doctor.checks import CheckResult, Identity, Status

IDENTITY = Identity(user="alice", uid=1000, gid=1000, home="/home/alice")
RESULTS = (
    CheckResult("podman", Status.PASS, "podman 5.8.8 runs rootless"),
    CheckResult("disk", Status.WARN, "5.0 GiB free under /home/alice/.local/share/ginn"),
    CheckResult(
        "subids",
        Status.FAIL,
        "/etc/subuid has no single line",
        ("sudo ginn host-setup --user alice", "sudo ginn host-setup --user alice --root-dir /x"),
    ),
    CheckResult("nvidia", Status.INFO, "no NVIDIA driver is loaded"),
)


@pytest.mark.fr("FR-007")
def test_text_report_prints_each_check_and_its_fixes() -> None:
    text = report.build(IDENTITY, RESULTS).text()
    assert text.splitlines() == [
        "ginn doctor for alice (uid 1000)",
        "PASS  podman: podman 5.8.8 runs rootless",
        "WARN  disk: 5.0 GiB free under /home/alice/.local/share/ginn",
        "FAIL  subids: /etc/subuid has no single line",
        "  fix: sudo ginn host-setup --user alice",
        "  fix: sudo ginn host-setup --user alice --root-dir /x",
        "INFO  nvidia: no NVIDIA driver is loaded",
        "summary: 1 PASS, 1 WARN, 1 FAIL, 1 INFO",
    ]


@pytest.mark.fr("FR-007")
def test_json_object_has_the_schema_one_keys() -> None:
    obj = report.build(IDENTITY, RESULTS).as_dict()
    assert set(obj) == {"schema", "user", "uid", "ok", "counts", "checks"}
    assert obj["schema"] == 1
    assert obj["user"] == "alice"
    assert obj["uid"] == 1000
    assert obj["ok"] is False
    assert obj["counts"] == {"PASS": 1, "WARN": 1, "FAIL": 1, "INFO": 1}
    check = obj["checks"][2]
    assert set(check) == {"name", "status", "message", "fixes"}
    assert check["status"] == "FAIL"
    assert check["fixes"] == [
        "sudo ginn host-setup --user alice",
        "sudo ginn host-setup --user alice --root-dir /x",
    ]


@pytest.mark.fr("FR-007")
def test_json_checks_keep_report_order_and_empty_fixes_for_non_failures() -> None:
    obj = report.build(IDENTITY, RESULTS).as_dict()
    assert [check["name"] for check in obj["checks"]] == ["podman", "disk", "subids", "nvidia"]
    assert obj["checks"][0]["fixes"] == []
    assert obj["checks"][3]["fixes"] == []


@pytest.mark.fr("FR-007")
def test_json_round_trips_without_loss() -> None:
    obj = report.build(IDENTITY, RESULTS).as_dict()
    assert json.loads(json.dumps(obj, sort_keys=True)) == obj


@pytest.mark.fr("FR-007")
def test_ok_and_failure_count_ignore_warn_and_info() -> None:
    no_fail = report.build(IDENTITY, RESULTS[:2] + RESULTS[3:])
    assert no_fail.failed == 0
    assert no_fail.as_dict()["ok"] is True
    full = report.build(IDENTITY, RESULTS)
    assert full.failed == 1
    assert full.total == len(RESULTS)
