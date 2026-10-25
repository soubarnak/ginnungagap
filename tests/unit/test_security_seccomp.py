# SPDX-License-Identifier: Apache-2.0
"""The nested-container seccomp profile and the inner podman config (FR-062 AC-3, ADR-006).

Covers security/seccomp.py and security/nested.py. The host's default profile is replaced by a
fixture with the same shape, so the test never reads `/usr/share/containers`. Nothing here runs
podman, and every write goes under tmp_path.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ginn.errors import GinnError
from ginn.security import nested, seccomp

SOURCE: dict[str, Any] = {
    "defaultAction": "SCMP_ACT_ERRNO",
    "architectures": ["SCMP_ARCH_X86_64"],
    "syscalls": [
        {
            "names": ["sethostname", "setdomainname"],
            "action": "SCMP_ACT_ALLOW",
            "args": [],
            "includes": {"caps": ["CAP_SYS_ADMIN"]},
        },
        {"names": ["sethostname"], "action": "SCMP_ACT_ALLOW", "args": []},
        {"names": ["read", "write"], "action": "SCMP_ACT_ALLOW", "args": []},
    ],
}


def _source_file(tmp_path: Path, document: object = SOURCE) -> Path:
    source = tmp_path / "seccomp.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    return source


def _profile(data: object = SOURCE) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(seccomp.build_profile(json.dumps(data).encode("utf-8")))
    return parsed


@pytest.mark.fr("FR-062")
def test_the_profile_allows_sethostname_first_with_no_conditions() -> None:
    rules = _profile()["syscalls"]
    assert rules[0] == {
        "names": ["sethostname"],
        "action": "SCMP_ACT_ALLOW",
        "args": [],
        "comment": seccomp.ALLOW_COMMENT,
        "includes": {},
        "excludes": {},
    }


@pytest.mark.fr("FR-062")
def test_sethostname_is_removed_from_every_other_rule_and_setdomainname_is_kept() -> None:
    rules = _profile()["syscalls"]
    rest = rules[1:]
    # The rule with setdomainname keeps its conditions; only the name goes.
    assert rest[0] == {
        "names": ["setdomainname"],
        "action": "SCMP_ACT_ALLOW",
        "args": [],
        "includes": {"caps": ["CAP_SYS_ADMIN"]},
    }
    # The rule that named only sethostname is dropped, and the others are untouched.
    assert rest[1] == {"names": ["read", "write"], "action": "SCMP_ACT_ALLOW", "args": []}
    assert len(rest) == 2
    assert all("sethostname" not in rule.get("names", []) for rule in rest)


@pytest.mark.fr("FR-062")
def test_nothing_else_in_the_profile_changes() -> None:
    profile = _profile()
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["architectures"] == ["SCMP_ARCH_X86_64"]
    assert set(profile) == set(SOURCE)


@pytest.mark.fr("FR-062")
def test_a_source_that_names_no_sethostname_is_refused_and_never_unconfined(tmp_path: Path) -> None:
    document = {
        "defaultAction": "SCMP_ACT_ERRNO",
        "syscalls": [{"names": ["read"], "action": "SCMP_ACT_ALLOW"}],
    }
    with pytest.raises(GinnError) as caught:
        seccomp.plan(tmp_path / "seccomp", _source_file(tmp_path, document))
    assert caught.value.code == "GINN-E-SECCOMP-SOURCE"
    assert "sethostname" in caught.value.message
    assert not (tmp_path / "seccomp").exists()


@pytest.mark.fr("FR-062")
@pytest.mark.parametrize(
    "document",
    [["not", "an", "object"], {"syscalls": "no list"}, {"no": "rules"}],
    ids=["list", "rules-not-a-list", "no-rules"],
)
def test_a_source_that_is_not_a_profile_is_refused(tmp_path: Path, document: object) -> None:
    with pytest.raises(GinnError) as caught:
        seccomp.plan(tmp_path / "seccomp", _source_file(tmp_path, document))
    assert caught.value.code == "GINN-E-SECCOMP-SOURCE"


@pytest.mark.fr("FR-062")
def test_a_source_that_is_not_json_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "seccomp.json"
    source.write_text("{not json", encoding="utf-8")
    with pytest.raises(GinnError) as caught:
        seccomp.plan(tmp_path / "seccomp", source)
    assert caught.value.code == "GINN-E-SECCOMP-SOURCE"
    assert "not valid JSON" in caught.value.message


@pytest.mark.fr("FR-062")
def test_a_missing_source_is_refused_before_any_write(tmp_path: Path) -> None:
    with pytest.raises(GinnError) as caught:
        seccomp.plan(tmp_path / "seccomp", tmp_path / "absent.json")
    assert caught.value.code == "GINN-E-SECCOMP-SOURCE"
    assert not (tmp_path / "seccomp").exists()


@pytest.mark.fr("DR-010")
def test_the_profile_is_named_by_the_hash_of_its_content_and_is_written_once(
    tmp_path: Path,
) -> None:
    source = _source_file(tmp_path)
    directory = tmp_path / "data" / "seccomp"
    planned = seccomp.plan(directory, source)
    digest = hashlib.sha256(planned.content).hexdigest()
    assert planned.path == directory / f"{digest}.json"
    assert not planned.path.exists()

    written = seccomp.write(planned, directory)
    assert written == planned.path
    assert planned.path.read_bytes() == planned.content
    assert (directory.stat().st_mode & 0o777) == 0o700

    # An existing file with the planned bytes is kept, so the second run changes nothing.
    stamp = planned.path.stat().st_mtime_ns
    seccomp.write(planned, directory)
    assert planned.path.stat().st_mtime_ns == stamp


@pytest.mark.fr("DR-010")
def test_a_changed_host_profile_gives_a_new_file(tmp_path: Path) -> None:
    source = _source_file(tmp_path)
    first = seccomp.plan(tmp_path / "seccomp", source)
    source.write_text(json.dumps({**SOURCE, "defaultAction": "SCMP_ACT_KILL"}), encoding="utf-8")
    second = seccomp.plan(tmp_path / "seccomp", source)
    assert first.path != second.path


@pytest.mark.fr("FR-062")
def test_the_inner_podman_config_is_written_to_the_realm_home(tmp_path: Path) -> None:
    realm_home = tmp_path / "home"
    written = nested.write_config(realm_home)
    config = realm_home / ".config" / "containers"
    assert written == [config / "storage.conf", config / "containers.conf"]
    storage = (config / "storage.conf").read_text(encoding="utf-8")
    assert 'driver = "overlay"' in storage
    assert 'mount_program = "/usr/bin/fuse-overlayfs"' in storage
    assert 'ignore_chown_errors = "true"' in storage
    containers = (config / "containers.conf").read_text(encoding="utf-8")
    assert 'cgroup_manager = "cgroupfs"' in containers
    assert 'events_logger = "file"' in containers


@pytest.mark.fr("FR-062")
def test_the_inner_podman_config_is_rewritten_in_place(tmp_path: Path) -> None:
    realm_home = tmp_path / "home"
    nested.write_config(realm_home)
    target = realm_home / ".config" / "containers" / "storage.conf"
    target.write_text("stale\n", encoding="utf-8")
    nested.write_config(realm_home)
    assert target.read_text(encoding="utf-8") == nested.STORAGE_CONF


@pytest.mark.fr("FR-062")
@pytest.mark.parametrize("default", ["SCMP_ACT_ALLOW", "SCMP_ACT_LOG", "SCMP_ACT_NOTIFY", None])
def test_a_profile_that_does_not_default_to_deny_is_refused(
    tmp_path: Path, default: str | None
) -> None:
    document = {**SOURCE, "defaultAction": default}
    with pytest.raises(GinnError) as caught:
        seccomp.plan(tmp_path / "seccomp", _source_file(tmp_path, document))
    assert caught.value.code == "GINN-E-SECCOMP-SOURCE"
    assert "deny" in caught.value.message


@pytest.mark.fr("FR-062")
def test_a_rule_with_the_single_name_field_is_refused_because_ginn_cannot_check_it(
    tmp_path: Path,
) -> None:
    document = {**SOURCE, "syscalls": [{"name": "sethostname", "action": "SCMP_ACT_ALLOW"}]}
    with pytest.raises(GinnError) as caught:
        seccomp.plan(tmp_path / "seccomp", _source_file(tmp_path, document))
    assert caught.value.code == "GINN-E-SECCOMP-SOURCE"
    assert "'name' field" in caught.value.message


@pytest.mark.fr("FR-062")
def test_a_rule_with_no_names_list_is_refused(tmp_path: Path) -> None:
    document = {**SOURCE, "syscalls": [{"action": "SCMP_ACT_ALLOW"}]}
    with pytest.raises(GinnError) as caught:
        seccomp.plan(tmp_path / "seccomp", _source_file(tmp_path, document))
    assert caught.value.code == "GINN-E-SECCOMP-SOURCE"


@pytest.mark.fr("DR-010")
def test_a_profile_path_that_is_a_link_is_refused_and_its_target_is_untouched(
    tmp_path: Path,
) -> None:
    planned = seccomp.plan(tmp_path / "seccomp", _source_file(tmp_path))
    target = tmp_path / "elsewhere.json"
    target.write_text("keep me\n", encoding="utf-8")
    planned.path.parent.mkdir(parents=True)
    planned.path.symlink_to(target)
    with pytest.raises(GinnError) as caught:
        seccomp.write(planned, tmp_path / "seccomp")
    assert caught.value.code == "GINN-E-SECCOMP-SOURCE"
    assert target.read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.fr("DR-010")
def test_a_profile_directory_that_is_a_link_is_refused(tmp_path: Path) -> None:
    planned = seccomp.plan(tmp_path / "real", _source_file(tmp_path))
    link = tmp_path / "seccomp"
    link.symlink_to(tmp_path / "real")
    with pytest.raises(GinnError) as caught:
        seccomp.write(planned, link)
    assert caught.value.code == "GINN-E-SECCOMP-SOURCE"


@pytest.mark.fr("DR-010")
def test_a_profile_file_that_differs_from_the_plan_is_replaced_atomically(tmp_path: Path) -> None:
    planned = seccomp.plan(tmp_path / "seccomp", _source_file(tmp_path))
    planned.path.parent.mkdir(parents=True)
    planned.path.write_text("tampered\n", encoding="utf-8")
    seccomp.write(planned, tmp_path / "seccomp")
    assert planned.path.read_bytes() == planned.content
