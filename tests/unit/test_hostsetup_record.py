# SPDX-License-Identifier: Apache-2.0
"""The Host Setup Record: schema 2 JSON, pre-images, refusals and the atomic write (DR-009).

Each test works on bytes or on a temporary file. Nothing touches the real /etc.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ginn.errors import GinnError
from ginn.hostsetup import record as rec
from ginn.hostsetup.subids import SubIdEntry

ENTRY_UID = SubIdEntry(name="alice", start=100000, count=524288)
ENTRY_GID = SubIdEntry(name="alice", start=100000, count=524288)
DIGEST = "a" * 64
BINARY = b"\x00\xffline\n\x01"
BOOT = "6f1d2c3b-0000-4000-8000-000000000001"


def _sample() -> rec.HostSetupRecord:
    """A complete schema 2 record with each kind of image, shared and per-user entries."""
    return rec.HostSetupRecord(
        user="alice",
        uid=1000,
        gid=1000,
        timestamp="2027-01-15T08:00:00.000Z",
        status="complete",
        subids=rec.SubIds(uid=ENTRY_UID, gid=ENTRY_GID),
        boot_id=BOOT,
        paths=(
            rec.PathEntry(
                path="etc/subuid",
                kind="append-lines",
                pre=rec.Image("file", mode=0o644, data=BINARY),
                post=rec.Image("file", mode=0o644, sha256=DIGEST),
                shared=True,
            ),
            rec.PathEntry(
                path="etc/ginn",
                kind="dir",
                pre=rec.Image("absent"),
                post=rec.Image("dir", mode=0o755),
                shared=True,
            ),
            rec.PathEntry(
                path="etc/sv/ginn-alice",
                kind="dir",
                pre=rec.Image("absent"),
                post=rec.Image("dir", mode=0o755),
            ),
            rec.PathEntry(
                path="etc/runit/runsvdir/default/ginn-alice",
                kind="symlink",
                pre=rec.Image("absent"),
                post=rec.Image("symlink", target="/etc/sv/ginn-alice"),
            ),
            rec.PathEntry(
                path="etc/ginn/setup-alice.json",
                kind="file",
                pre=rec.Image("absent"),
                post=rec.Image("self"),
            ),
            rec.PathEntry(
                path="etc/sv/ginn-alice/run",
                kind="file",
                pre=rec.Image("absent"),
                post=rec.Image("file", mode=0o755, sha256=DIGEST),
            ),
        ),
        service_paths=(
            rec.PathEntry(
                path="sys/fs/cgroup/cgroup.subtree_control",
                kind="file",
                pre=rec.Image("file", mode=0o644, data=b"cpu io memory\n"),
                shared=True,
            ),
            rec.PathEntry(
                path="sys/fs/cgroup/ginn",
                kind="dir",
                pre=rec.Image("absent"),
                shared=True,
            ),
            rec.PathEntry(path="run/ginn/1000", kind="dir", pre=rec.Image("absent")),
        ),
    )


def _json(record: rec.HostSetupRecord) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(rec.encode(record))
    return loaded


def _refused(body: dict[str, Any], match: str) -> None:
    with pytest.raises(rec.RecordError, match=match):
        rec.decode(json.dumps(body).encode("utf-8"))


@pytest.mark.fr("DR-009")
def test_a_record_survives_an_encode_and_decode_round_trip() -> None:
    record = _sample()
    assert rec.decode(rec.encode(record)) == record


@pytest.mark.fr("DR-009")
def test_the_json_names_the_user_ids_subids_boot_id_status_timestamp_and_schema_2() -> None:
    body = _json(_sample())
    assert body["schema"] == 2
    assert body["user"] == "alice"
    assert (body["uid"], body["gid"]) == (1000, 1000)
    assert body["timestamp"] == "2027-01-15T08:00:00.000Z"
    assert body["status"] == "complete"
    assert body["boot_id"] == BOOT
    assert body["subids"]["uid"] == {"name": "alice", "start": 100000, "count": 524288}
    assert body["subids"]["gid"] == {"name": "alice", "start": 100000, "count": 524288}


@pytest.mark.fr("DR-009")
def test_a_file_pre_image_is_stored_as_base64_bytes_so_undo_can_restore_it_exactly() -> None:
    body = _json(_sample())
    pre = body["paths"][0]["pre"]
    assert pre == {"state": "file", "mode": 0o644, "data": base64.b64encode(BINARY).decode("ascii")}
    assert base64.b64decode(pre["data"]) == BINARY


@pytest.mark.fr("DR-009")
def test_a_file_post_image_has_its_mode_and_a_digest_and_not_the_bytes() -> None:
    body = _json(_sample())
    assert body["paths"][0]["post"] == {"state": "file", "mode": 0o644, "sha256": DIGEST}
    script = next(entry for entry in body["paths"] if entry["path"].endswith("/run"))
    assert script["post"] == {"state": "file", "mode": 0o755, "sha256": DIGEST}


@pytest.mark.fr("DR-009")
def test_a_directory_post_image_carries_its_mode() -> None:
    body = _json(_sample())
    directory = next(entry for entry in body["paths"] if entry["path"] == "etc/ginn")
    assert directory["post"] == {"state": "dir", "mode": 0o755}


@pytest.mark.fr("DR-009")
def test_a_directory_post_image_without_its_mode_is_refused() -> None:
    body = _json(_sample())
    entry = next(item for item in body["paths"] if item["path"] == "etc/ginn")
    entry["post"] = {"state": "dir"}
    _refused(body, "exactly its fields")


@pytest.mark.fr("DR-009")
def test_the_subid_files_are_kind_append_lines_and_shared() -> None:
    body = _json(_sample())
    subuid = body["paths"][0]
    assert subuid["path"] == "etc/subuid"
    assert subuid["kind"] == "append-lines"
    assert subuid["shared"] is True


@pytest.mark.fr("DR-009")
def test_each_entry_says_whether_it_is_shared_and_a_user_entry_is_not() -> None:
    body = _json(_sample())
    shared = {entry["path"]: entry["shared"] for entry in body["paths"] + body["service_paths"]}
    assert shared["etc/ginn"] is True
    assert shared["sys/fs/cgroup/ginn"] is True
    assert shared["sys/fs/cgroup/cgroup.subtree_control"] is True
    assert shared["etc/sv/ginn-alice"] is False
    assert shared["run/ginn/1000"] is False


@pytest.mark.fr("DR-009")
def test_an_entry_without_its_shared_flag_is_refused() -> None:
    body = _json(_sample())
    del body["paths"][1]["shared"]
    _refused(body, "exactly its fields")


@pytest.mark.fr("DR-009")
def test_a_shared_flag_that_is_not_a_boolean_is_refused() -> None:
    body = _json(_sample())
    body["paths"][1]["shared"] = "yes"
    _refused(body, "shared flag")


@pytest.mark.fr("DR-009")
def test_the_record_lists_itself_with_an_absent_pre_image_and_post_image_self() -> None:
    body = _json(_sample())
    own = [entry for entry in body["paths"] if entry["path"] == "etc/ginn/setup-alice.json"]
    assert own == [
        {
            "path": "etc/ginn/setup-alice.json",
            "kind": "file",
            "shared": False,
            "pre": {"state": "absent"},
            "post": {"state": "self"},
        }
    ]


@pytest.mark.fr("DR-009")
def test_service_paths_carry_pre_images_and_no_post_image() -> None:
    body = _json(_sample())
    assert [entry["path"] for entry in body["service_paths"]] == [
        "sys/fs/cgroup/cgroup.subtree_control",
        "sys/fs/cgroup/ginn",
        "run/ginn/1000",
    ]
    assert all("post" not in entry for entry in body["service_paths"])


@pytest.mark.fr("DR-009")
def test_a_null_boot_id_is_allowed_and_a_blank_one_is_refused() -> None:
    no_boot = replace(_sample(), boot_id=None)
    assert rec.decode(rec.encode(no_boot)).boot_id is None
    with pytest.raises(ValueError, match="boot_id"):
        rec.encode(replace(_sample(), boot_id=""))
    body = _json(_sample())
    body["boot_id"] = ""
    _refused(body, "boot_id")


@pytest.mark.fr("DR-009")
def test_a_pending_record_may_hold_no_subids_and_no_post_images() -> None:
    pending = replace(
        _sample(),
        status="pending",
        subids=None,
        paths=tuple(replace(entry, post=None) for entry in _sample().paths),
    )
    assert rec.decode(rec.encode(pending)) == pending


@pytest.mark.fr("DR-009")
def test_a_complete_record_without_subids_is_refused_when_encoded() -> None:
    with pytest.raises(ValueError, match="subordinate ID"):
        rec.encode(replace(_sample(), subids=None))


@pytest.mark.fr("DR-009")
def test_a_complete_record_missing_a_post_image_is_refused_when_encoded() -> None:
    paths = list(_sample().paths)
    paths[0] = replace(paths[0], post=None)
    with pytest.raises(ValueError, match="no post-image"):
        rec.encode(replace(_sample(), paths=tuple(paths)))


@pytest.mark.fr("DR-009")
def test_a_record_that_does_not_list_its_own_file_is_refused() -> None:
    kept = tuple(entry for entry in _sample().paths if entry.path != "etc/ginn/setup-alice.json")
    with pytest.raises(ValueError, match="own file"):
        rec.encode(replace(_sample(), paths=kept))


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("bad", ["../etc/shadow", "/etc/passwd", "etc//x", "etc/./x", "a\nb"])
def test_a_path_that_leaves_the_root_or_is_not_clean_is_refused(bad: str) -> None:
    paths = (rec.PathEntry(path=bad, kind="file", pre=rec.Image("absent")),) + _sample().paths
    with pytest.raises(ValueError):
        rec.encode(replace(_sample(), paths=paths))


@pytest.mark.fr("DR-009")
def test_a_record_of_schema_1_is_refused_on_read() -> None:
    body = _json(_sample())
    body["schema"] = 1
    _refused(body, "schema")


@pytest.mark.fr("DR-009")
def test_an_unknown_schema_version_is_refused_on_read() -> None:
    body = _json(_sample())
    body["schema"] = 3
    _refused(body, "schema")


@pytest.mark.fr("DR-009")
def test_a_record_with_an_extra_top_level_field_is_refused() -> None:
    body = _json(_sample())
    body["extra"] = 1
    with pytest.raises(ValueError, match="top level"):
        rec.decode(json.dumps(body).encode("utf-8"))


@pytest.mark.fr("DR-009")
def test_a_file_pre_image_without_its_data_is_refused() -> None:
    body = _json(_sample())
    del body["paths"][0]["pre"]["data"]
    with pytest.raises(ValueError, match="exactly"):
        rec.decode(json.dumps(body).encode("utf-8"))


@pytest.mark.fr("DR-009")
def test_a_post_image_digest_that_is_not_sha256_hex_is_refused() -> None:
    body = _json(_sample())
    body["paths"][0]["post"]["sha256"] = "not-a-digest"
    with pytest.raises(ValueError, match="digest"):
        rec.decode(json.dumps(body).encode("utf-8"))


@pytest.mark.fr("DR-009")
def test_bytes_that_are_not_json_are_refused_as_a_setup_path_error(tmp_path: Path) -> None:
    path = tmp_path / "setup-alice.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(GinnError) as caught:
        rec.load(path)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"
    assert caught.value.exit_code == 77
    assert str(path) in caught.value.message


@pytest.mark.fr("DR-009")
def test_a_missing_record_loads_as_none_and_is_not_an_error(tmp_path: Path) -> None:
    assert rec.load(tmp_path / "setup-alice.json") is None


@pytest.mark.fr("DR-009")
def test_a_symbolic_link_in_place_of_the_record_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_bytes(rec.encode(_sample()))
    path = tmp_path / "setup-alice.json"
    os.symlink(target, path)
    with pytest.raises(GinnError) as caught:
        rec.load(path)
    assert caught.value.code == "GINN-E-SETUP-PATH-UNSAFE"


@pytest.mark.fr("DR-009")
def test_save_writes_the_record_with_mode_0600_and_load_reads_it_back(tmp_path: Path) -> None:
    path = tmp_path / "setup-alice.json"
    rec.save(path, _sample())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert rec.load(path) == _sample()
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["setup-alice.json"]


@pytest.mark.fr("DR-009")
def test_the_relative_path_of_a_record_sits_under_etc_ginn() -> None:
    assert rec.relpath("alice") == "etc/ginn/setup-alice.json"


@pytest.mark.fr("NFR-040")
def test_a_user_name_that_could_escape_the_record_directory_is_refused() -> None:
    with pytest.raises(ValueError, match="invalid user name"):
        rec.encode(replace(_sample(), user="../alice"))


@pytest.mark.fr("NFR-040")
def test_a_system_path_refusal_asks_for_an_inspection_and_never_for_a_move() -> None:
    for system in (Path("/var/service"), Path("/etc/sv"), Path("/etc/ginn")):
        err = rec.unsafe(system, "is a file")
        assert err.code == "GINN-E-SETUP-PATH-UNSAFE"
        assert err.exit_code == 77
        assert "inspect" in err.remedy
        assert "aside" not in err.remedy
        assert "move" not in err.remedy
        assert "\n" not in err.remedy


@pytest.mark.fr("NFR-040")
def test_a_record_refusal_asks_for_a_copy_before_any_change() -> None:
    err = rec.unsafe(Path("/etc/ginn/setup-alice.json"), "holds a Host Setup Record")
    assert "keep a copy" in err.remedy
    assert "aside" not in err.remedy
    assert err.message == (
        "Host Setup refuses /etc/ginn/setup-alice.json: holds a Host Setup Record"
    )
