# SPDX-License-Identifier: Apache-2.0
"""`ginn rebuild` in the CLI (FR-021, FR-022, FR-107, IR-010, IR-011).

Two groups. The first replaces `rebuild_realm` with a recorder, as test_cli_create does, so the
argument rule, the JSON and text shapes, the GINN-W-REPLAY-MISSING warnings, the progress lines and
the seams that the command passes are checked without a podman rebuild. The second runs the real
`rebuild_realm` on the `world` fixture, where the refusals of a realm that cannot rebuild happen
before any write. A running realm is accepted by the command and left to the operation, which stops
and starts it: that is proven on ScriptedPodman in tests/unit/test_ops_rebuild.py.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.cli.commands import rebuild as rebuild_cmd
from ginn.cli.commands.rebuild import SPEC
from ginn.domain import RealmName, RealmState
from ginn.errors import warn
from ginn.ops.rebuild import RebuildResult
from tests.unit.realm_cli_support import World, make_realm, record_of, stored


def _result(
    *,
    state: RealmState = RealmState.STOPPED,
    replay_missing: tuple[str, ...] = (),
    warnings: tuple[Any, ...] = (),
    old_removed: bool = True,
) -> RebuildResult:
    """The result of a rebuild that made generation 2 of `arch`, with the given notices."""
    record = record_of("arch", state, "c" * 64)
    return RebuildResult(
        record=replace(record, generation=2),
        warnings=warnings,
        replay_missing=replay_missing,
        old_removed=old_removed,
    )


def _recorded(monkeypatch: pytest.MonkeyPatch, result: RebuildResult) -> list[dict[str, Any]]:
    """Replace rebuild_realm with a recorder that returns `result`. Each call is kept."""
    calls: list[dict[str, Any]] = []

    def fake_rebuild(
        env: Any, podman: Any, name: RealmName, *, session: Any, gid: int, hooks: Any
    ) -> RebuildResult:
        calls.append({"name": name, "session": session, "gid": gid, "hooks": hooks})
        return result

    monkeypatch.setattr(rebuild_cmd, "rebuild_realm", fake_rebuild)
    return calls


@pytest.mark.fr("IR-010")
def test_the_command_is_named_rebuild_with_an_example() -> None:
    assert SPEC.name == "rebuild"
    assert SPEC.example == "ginn rebuild arch --json"


@pytest.mark.fr("FR-021")
def test_the_rebuild_takes_one_realm_and_asks_no_confirmation(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """IR-010 and FR-021 name no yes for a rebuild, so `--yes` is an unknown option (exit 2)."""
    calls = _recorded(monkeypatch, _result())
    assert main(["rebuild", "arch", "--yes"]) == 2
    assert calls == []
    assert main(["rebuild", "arch"]) == 0
    assert [call["name"] for call in calls] == [RealmName("arch")]


@pytest.mark.fr("FR-021")
def test_the_json_result_is_the_record_view_with_the_generation_and_the_replay(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _result()
    _recorded(monkeypatch, result)

    assert main(["--json", "rebuild", "arch"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out == {
        "name": "arch",
        "state": "stopped",
        "restart_required": False,
        "share_skipped": [],
        "generation": 2,
        "image_digest": result.record.image_digest,
        "replay_missing": [],
        "old_removed": True,
    }


@pytest.mark.fr("FR-021")
def test_the_text_result_names_the_generation_and_the_state(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _recorded(monkeypatch, _result())

    assert main(["rebuild", "arch"]) == 0

    assert capsys.readouterr().out == "realm arch rebuilt: generation 2, state stopped\n"


@pytest.mark.fr("FR-022")
def test_a_package_no_longer_upstream_is_a_json_warning_and_the_rebuild_completes(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notice = warn("GINN-W-REPLAY-MISSING", package="gone")
    _recorded(monkeypatch, _result(replay_missing=("gone",), warnings=(notice,)))

    assert main(["--json", "rebuild", "arch"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["replay_missing"] == ["gone"]
    assert out["state"] == "stopped"
    assert [w["code"] for w in out["warnings"]] == ["GINN-W-REPLAY-MISSING"]


@pytest.mark.fr("FR-022")
def test_a_package_no_longer_upstream_is_named_on_stderr_in_text_mode(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notice = warn("GINN-W-REPLAY-MISSING", package="gone")
    _recorded(monkeypatch, _result(replay_missing=("gone",), warnings=(notice,)))

    assert main(["rebuild", "arch"]) == 0

    captured = capsys.readouterr()
    assert captured.err.startswith("GINN-W-REPLAY-MISSING: ")
    assert "package gone is no longer available upstream" in captured.err
    assert captured.out == "realm arch rebuilt: generation 2, state stopped\n"


@pytest.mark.fr("FR-021")
def test_an_old_root_that_could_not_be_removed_is_named_in_text_and_json(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _recorded(monkeypatch, _result(old_removed=False))
    assert main(["rebuild", "arch"]) == 0
    text = capsys.readouterr().out
    assert text.splitlines()[1].startswith("note: an older root was not removed")

    assert main(["--json", "rebuild", "arch"]) == 0
    assert json.loads(capsys.readouterr().out)["old_removed"] is False


@pytest.mark.fr("FR-021")
def test_the_command_passes_the_host_user_the_group_and_the_cli_seams(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _recorded(monkeypatch, _result())

    assert main(["rebuild", "arch"]) == 0

    (call,) = calls
    seams = _context.seams()
    hooks = call["hooks"]
    assert call["session"].host_home == world.home
    assert call["session"].uid == os.getuid()
    assert call["gid"] == os.getgid()
    assert hooks.helper == seams.helper
    assert hooks.mountinfo == seams.mountinfo
    assert hooks.lifecycle.exists is seams.exists
    assert hooks.lifecycle.mountinfo == seams.mountinfo
    assert isinstance(hooks.program, str) and hooks.program != ""


@pytest.mark.fr("FR-107")
def test_each_journal_step_is_a_progress_line_on_stderr_in_json_mode(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def rebuild_with_steps(*_args: Any, hooks: Any, **_kwargs: Any) -> RebuildResult:
        assert hooks.on_step is not None
        hooks.on_step("pulled")
        return _result()

    monkeypatch.setattr(rebuild_cmd, "rebuild_realm", rebuild_with_steps)

    assert main(["--json", "rebuild", "arch"]) == 0

    lines = capsys.readouterr().err.splitlines()
    assert json.loads(lines[0]) == {
        "progress": {"step": "pulled", "message": "rebuild: step pulled done"}
    }


@pytest.mark.fr("FR-021")
def test_a_running_realm_is_accepted_and_the_operation_stops_and_starts_it(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command does not refuse a running realm. The operation stops it, rebuilds and starts it
    again, so the result reads running (ops/rebuild.py, step 3 and step 8)."""
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    calls = _recorded(monkeypatch, _result(state=RealmState.RUNNING))

    assert main(["--json", "rebuild", "arch"]) == 0

    assert [call["name"] for call in calls] == [RealmName("arch")]
    assert json.loads(capsys.readouterr().out)["state"] == "running"


@pytest.mark.fr("FR-021")
def test_an_unknown_realm_is_not_found_before_any_podman_call(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "rebuild", "nosuch"]) == 3
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "GINN-E-NO-SUCH-REALM"
    assert world.fake.calls == []


@pytest.mark.fr("FR-021")
def test_a_realm_that_cannot_rebuild_is_refused_before_any_write(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    before = make_realm(world, "arch", RealmState.BROKEN, running=False)

    assert main(["--json", "rebuild", "arch"]) == 4

    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "GINN-E-REALM-STATE"
    assert stored(world, "arch") == before
    assert world.fake.calls == []
