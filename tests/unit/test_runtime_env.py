# SPDX-License-Identifier: Apache-2.0
"""Session environment for exec and launch: the allowlist, the session binds and the skips.

Covers runtime/env.py. The host is never read: `file_type` is a fake, and the host environment is a
plain dict.
"""

import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from ginn.domain.enums import Tier
from ginn.runtime.env import (
    PASSED_VARIABLES,
    PROXY_VARIABLES,
    X11_DIR,
    FileType,
    SessionEnv,
    session_env,
)
from ginn.runtime.mounts import Exists, SessionBind, attach_plan, runtime_dir

pytestmark = [pytest.mark.fr("FR-046"), pytest.mark.fr("FR-061")]

UID = 1000
USER = "alice"
HOST_RUNTIME = "/run/user/1000"
REALM_RUNTIME = runtime_dir(UID)
WAYLAND = f"{HOST_RUNTIME}/wayland-1"
PIPEWIRE = f"{HOST_RUNTIME}/pipewire-0"
PULSE = f"{HOST_RUNTIME}/pulse/native"
X0 = f"{X11_DIR}/X0"
WAYLAND_REALM = f"{REALM_RUNTIME}/wayland-0"
AUTHORITY_REALM = f"{REALM_RUNTIME}/ginn-xauthority"
ALLOWED = {
    "HOME",
    "XDG_RUNTIME_DIR",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "XAUTHORITY",
    *PASSED_VARIABLES,
}


@dataclass(frozen=True)
class Layout:
    home: Path
    owned: tuple[Path, ...]


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    home = tmp_path / "home" / USER
    home.mkdir(parents=True)
    return Layout(
        home=home,
        owned=(
            tmp_path / "xdg" / "data" / "ginn",
            tmp_path / "xdg" / "config" / "ginn",
            tmp_path / "xdg" / "state" / "ginn",
            tmp_path / "run" / "ginn" / str(UID),
        ),
    )


def fake_fs(paths: Sequence[str], kinds: Mapping[str, int] | None = None) -> FileType:
    """A file-type view in which exactly `paths` exist. The host is not read.

    A path is a socket by default, and `/tmp/.X11-unix` is a directory. `kinds` names another type
    for a path in `paths`.
    """
    given = {} if kinds is None else kinds
    table = {
        path: given.get(path, stat.S_IFDIR if path == X11_DIR else stat.S_IFSOCK) for path in paths
    }
    return table.get


def present_only(paths: Sequence[str]) -> Exists:
    """The bool view that the mount plan takes. Exactly `paths` exist. The host is not read."""
    return frozenset(paths).__contains__


def run(
    layout: Layout,
    host_env: Mapping[str, str],
    present: Sequence[str] = (),
    kinds: Mapping[str, int] | None = None,
) -> SessionEnv:
    return session_env(
        host_env,
        home=layout.home,
        uid=UID,
        owned_paths=layout.owned,
        file_type=fake_fs(present, kinds),
    )


def messages(result: SessionEnv) -> list[str]:
    return [record.message() for record in result.skipped]


def test_wayland_session_golden(layout: Layout) -> None:
    result = run(
        layout,
        {
            "WAYLAND_DISPLAY": "wayland-1",
            "XDG_RUNTIME_DIR": HOST_RUNTIME,
            "LANG": "en_GB.UTF-8",
            "TZ": "Europe/London",
        },
        present=[WAYLAND],
    )
    assert result.env == {
        "HOME": str(layout.home),
        "XDG_RUNTIME_DIR": REALM_RUNTIME,
        "WAYLAND_DISPLAY": "wayland-0",
        "LANG": "en_GB.UTF-8",
        "TZ": "Europe/London",
    }
    assert result.binds == [SessionBind(WAYLAND, WAYLAND_REALM, "rw")]
    assert result.skipped == []
    assert result.pass_through == []


def test_x11_session_golden(layout: Layout) -> None:
    authority = str(layout.home / ".Xauthority")
    result = run(
        layout,
        {"DISPLAY": ":0", "XAUTHORITY": authority, "XDG_RUNTIME_DIR": HOST_RUNTIME},
        present=[X0, X11_DIR, authority],
        kinds={authority: stat.S_IFREG},
    )
    assert result.env == {
        "HOME": str(layout.home),
        "XDG_RUNTIME_DIR": REALM_RUNTIME,
        "DISPLAY": ":0",
        "XAUTHORITY": AUTHORITY_REALM,
    }
    assert result.binds == [
        SessionBind(X11_DIR, X11_DIR, "ro", True),
        SessionBind(authority, AUTHORITY_REALM, "ro"),
    ]
    assert result.skipped == []


def test_wayland_and_x11_session_golden(layout: Layout) -> None:
    result = run(
        layout,
        {
            "WAYLAND_DISPLAY": "wayland-1",
            "DISPLAY": ":1.0",
            "XDG_RUNTIME_DIR": HOST_RUNTIME,
        },
        present=[WAYLAND, f"{X11_DIR}/X1", X11_DIR],
    )
    assert result.env["WAYLAND_DISPLAY"] == "wayland-0"
    assert result.env["DISPLAY"] == ":1.0"
    assert [bind.host for bind in result.binds] == [WAYLAND, X11_DIR]


def test_session_with_audio_binds_pipewire_and_pulse(layout: Layout) -> None:
    result = run(
        layout,
        {"XDG_RUNTIME_DIR": HOST_RUNTIME},
        present=[PIPEWIRE, PULSE],
    )
    assert result.binds == [
        SessionBind(PIPEWIRE, f"{REALM_RUNTIME}/pipewire-0", "rw"),
        SessionBind(PULSE, f"{REALM_RUNTIME}/pulse/native", "rw"),
    ]
    assert "PULSE_SERVER" not in result.env


def test_session_with_no_audio_socket_has_no_audio_binds_and_no_skip(layout: Layout) -> None:
    result = run(
        layout, {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": HOST_RUNTIME}, [WAYLAND]
    )
    assert [bind.realm for bind in result.binds] == [WAYLAND_REALM]
    assert result.skipped == []


def test_always_sets_home_and_runtime_dir_even_with_no_session(layout: Layout) -> None:
    result = run(layout, {})
    assert result.env == {"HOME": str(layout.home), "XDG_RUNTIME_DIR": REALM_RUNTIME}
    assert result.binds == []
    assert result.skipped == []


def test_missing_wayland_socket_drops_the_variable_and_reports(layout: Layout) -> None:
    result = run(layout, {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": HOST_RUNTIME})
    assert "WAYLAND_DISPLAY" not in result.env
    assert result.binds == []
    assert messages(result) == ["session skipped: WAYLAND_DISPLAY (socket missing)"]


def test_absolute_wayland_display_binds_at_the_fixed_realm_name(layout: Layout) -> None:
    """The host's own socket name never reaches the realm. The realm always sees wayland-0."""
    custom = "/run/user/1000/custom-wl"
    result = run(layout, {"WAYLAND_DISPLAY": custom}, present=[custom])
    assert result.env["WAYLAND_DISPLAY"] == "wayland-0"
    assert result.binds == [SessionBind(custom, WAYLAND_REALM, "rw")]


@pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden"])
def test_wayland_name_must_be_a_plain_socket_name(layout: Layout, name: str) -> None:
    result = run(layout, {"WAYLAND_DISPLAY": name, "XDG_RUNTIME_DIR": HOST_RUNTIME}, [WAYLAND])
    assert "WAYLAND_DISPLAY" not in result.env
    assert result.binds == []
    assert messages(result) == ["session skipped: WAYLAND_DISPLAY (not a socket name)"]


def test_wayland_source_of_another_type_is_skipped_and_not_bound(layout: Layout) -> None:
    result = run(
        layout,
        {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": HOST_RUNTIME},
        present=[WAYLAND],
        kinds={WAYLAND: stat.S_IFDIR},
    )
    assert "WAYLAND_DISPLAY" not in result.env
    assert result.binds == []
    assert messages(result) == ["session skipped: WAYLAND_DISPLAY (not a socket)"]


@pytest.mark.fr("FR-068")
@pytest.mark.parametrize("display", ["bus", f"{HOST_RUNTIME}/bus"])
def test_wayland_display_that_names_the_dbus_socket_is_refused(
    layout: Layout, display: str
) -> None:
    """AS-012: the D-Bus session socket is not a display, even when it is a socket."""
    result = run(
        layout,
        {"WAYLAND_DISPLAY": display, "XDG_RUNTIME_DIR": HOST_RUNTIME},
        present=[f"{HOST_RUNTIME}/bus"],
    )
    assert "WAYLAND_DISPLAY" not in result.env
    assert result.binds == []
    assert messages(result) == [
        "session skipped: WAYLAND_DISPLAY (AS-012: the D-Bus session socket, not a display)"
    ]


@pytest.mark.fr("FR-068")
def test_wayland_display_linked_to_the_dbus_socket_is_refused(
    layout: Layout, tmp_path: Path
) -> None:
    """The name is `wl`, but it resolves to the D-Bus socket, so the link is refused as well."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    os.symlink(runtime / "bus", runtime / "wl")
    result = run(
        layout,
        {"WAYLAND_DISPLAY": "wl", "XDG_RUNTIME_DIR": str(runtime)},
        present=[str(runtime / "wl")],
    )
    assert "WAYLAND_DISPLAY" not in result.env
    assert result.binds == []
    assert messages(result) == [
        "session skipped: WAYLAND_DISPLAY (AS-012: the D-Bus session socket, not a display)"
    ]


def test_missing_x11_socket_drops_display(layout: Layout) -> None:
    result = run(layout, {"DISPLAY": ":0"})
    assert "DISPLAY" not in result.env
    assert result.binds == []
    assert messages(result) == ["session skipped: DISPLAY (socket missing)"]


def test_x11_socket_of_another_type_drops_display(layout: Layout) -> None:
    result = run(layout, {"DISPLAY": ":0"}, present=[X0, X11_DIR], kinds={X0: stat.S_IFREG})
    assert "DISPLAY" not in result.env
    assert result.binds == []
    assert messages(result) == ["session skipped: DISPLAY (not a socket)"]


def test_x11_directory_of_another_type_drops_display(layout: Layout) -> None:
    result = run(layout, {"DISPLAY": ":0"}, present=[X0, X11_DIR], kinds={X11_DIR: stat.S_IFREG})
    assert "DISPLAY" not in result.env
    assert result.binds == []
    assert messages(result) == ["session skipped: DISPLAY (not a directory)"]


@pytest.mark.parametrize("display", ["host.example:10.0", "localhost:10", "garbage"])
def test_remote_display_is_not_granted(layout: Layout, display: str) -> None:
    result = run(layout, {"DISPLAY": display}, present=[X0, X11_DIR])
    assert "DISPLAY" not in result.env
    assert result.binds == []
    assert messages(result) == ["session skipped: DISPLAY (only a local display is granted (:N))"]


def test_missing_authority_file_is_dropped_and_reported(layout: Layout) -> None:
    authority = str(layout.home / ".Xauthority")
    result = run(layout, {"XAUTHORITY": authority}, present=[])
    assert "XAUTHORITY" not in result.env
    assert result.binds == []
    assert messages(result) == ["session skipped: XAUTHORITY (file missing)"]


def test_authority_that_is_a_directory_is_skipped_and_not_bound(layout: Layout) -> None:
    authority = str(layout.home / ".Xauthority")
    result = run(
        layout,
        {"XAUTHORITY": authority},
        present=[authority],
        kinds={authority: stat.S_IFDIR},
    )
    assert "XAUTHORITY" not in result.env
    assert result.binds == []
    assert messages(result) == ["session skipped: XAUTHORITY (not a regular file)"]


def test_relative_authority_path_is_never_looked_up(layout: Layout) -> None:
    result = run(layout, {"XAUTHORITY": "relative/.Xauthority"}, present=["relative/.Xauthority"])
    assert "XAUTHORITY" not in result.env
    assert result.binds == []
    assert messages(result) == ["session skipped: XAUTHORITY (not an absolute path)"]


def test_authority_under_ssh_is_refused_by_the_dr004_rule(layout: Layout) -> None:
    key = str(layout.home / ".ssh" / "id_ed25519")
    result = run(layout, {"XAUTHORITY": key}, present=[key], kinds={key: stat.S_IFREG})
    assert "XAUTHORITY" not in result.env
    assert result.binds == []
    assert len(result.skipped) == 1
    assert "DR-004.ssh" in result.skipped[0].reason


@pytest.mark.fr("AS-011")
def test_authority_under_a_moved_applications_dir_is_refused(layout: Layout) -> None:
    """XDG_DATA_HOME moves the AS-011 rule, so the same refusal applies to a session bind."""
    moved = layout.home.parent / "xdg-data"
    authority = str(moved / "applications" / "xauth")
    result = run(
        layout,
        {"XAUTHORITY": authority, "XDG_DATA_HOME": str(moved)},
        present=[authority],
        kinds={authority: stat.S_IFREG},
    )
    assert "XAUTHORITY" not in result.env
    assert result.binds == []
    assert result.skipped[0].reason.startswith("AS-011.applications")


@pytest.mark.parametrize("path_suffix", ["x:y", "x,y"])
def test_authority_path_with_podman_separator_is_dropped(layout: Layout, path_suffix: str) -> None:
    raw = f"/run/user/1000/{path_suffix}"
    result = run(layout, {"XAUTHORITY": raw}, present=[raw], kinds={raw: stat.S_IFREG})
    assert "XAUTHORITY" not in result.env
    assert result.binds == []
    assert result.skipped[0].reason.startswith("contains ':' or ','")


@pytest.mark.parametrize(("socket", "host"), [("pipewire-0", PIPEWIRE), ("pulse/native", PULSE)])
def test_audio_source_of_another_type_is_reported_and_not_bound(
    layout: Layout, socket: str, host: str
) -> None:
    """A missing audio socket is silent. One that is present but not a socket is reported."""
    result = run(
        layout,
        {"XDG_RUNTIME_DIR": HOST_RUNTIME},
        present=[host],
        kinds={host: stat.S_IFREG},
    )
    assert result.binds == []
    assert messages(result) == [f"session skipped: {socket} (not a socket)"]


def test_locale_and_timezone_are_set_in_the_realm(layout: Layout) -> None:
    result = run(layout, {"LANG": "en_GB.UTF-8", "TZ": "Europe/London"})
    assert result.env["LANG"] == "en_GB.UTF-8"
    assert result.env["TZ"] == "Europe/London"
    assert result.pass_through == []


def test_proxy_names_are_passed_and_their_values_are_never_stored(layout: Layout) -> None:
    """ADR-010: the realm gets `--env NAME`, and podman reads the value. No value is kept here."""
    proxy_url = "http://user:hunter2@proxy.example:3128"
    host = {
        "LANG": "en_GB.UTF-8",
        "http_proxy": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "no_proxy": "localhost,127.0.0.1",
    }
    result = run(layout, host)
    assert result.pass_through == ["http_proxy", "HTTPS_PROXY", "no_proxy"]
    assert set(PROXY_VARIABLES).isdisjoint(result.env)
    assert result.env == {
        "HOME": str(layout.home),
        "XDG_RUNTIME_DIR": REALM_RUNTIME,
        "LANG": "en_GB.UTF-8",
    }
    assert "hunter2" not in repr(result)
    assert proxy_url not in repr(result)
    assert all(proxy_url not in value for value in result.env.values())


def test_value_with_newline_or_nul_is_dropped_and_its_text_is_not_reported(layout: Layout) -> None:
    proxy_url = "http://user:hunter2@proxy.example:3128\nINJECTED=1"
    result = run(layout, {"https_proxy": proxy_url, "LANG": "en_GB.UTF-8\0"})
    assert "https_proxy" not in result.env
    assert "https_proxy" not in result.pass_through
    assert "LANG" not in result.env
    assert messages(result) == [
        "session skipped: LANG (contains NUL or a newline)",
        "session skipped: https_proxy (contains NUL or a newline)",
    ]
    assert all("hunter2" not in message for message in messages(result))


def test_only_allowlisted_host_variables_reach_the_realm(layout: Layout) -> None:
    host = {
        "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
        "GPG_AGENT_INFO": "/run/user/1000/gnupg/S.gpg-agent:1:1",
        "GNUPGHOME": "/home/alice/.gnupg",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "CONTAINER_HOST": "unix:///run/podman.sock",
        "FOO": "bar",
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
    }
    result = run(layout, host, present=[])
    assert set(result.env) <= ALLOWED
    assert result.binds == []
    assert result.skipped == []
    assert result.pass_through == []


def test_host_runtime_falls_back_to_the_default_when_unset(layout: Layout) -> None:
    """With XDG_RUNTIME_DIR unset, the probe runs under /run/user/<uid>, where sockets live."""
    result = run(layout, {"WAYLAND_DISPLAY": "wayland-0"}, present=[f"/run/user/{UID}/wayland-0"])
    assert result.binds == [
        SessionBind(f"/run/user/{UID}/wayland-0", f"{REALM_RUNTIME}/wayland-0", "rw")
    ]


def test_relative_home_is_refused(layout: Layout) -> None:
    with pytest.raises(ValueError):
        session_env(
            {}, home=Path("alice"), uid=UID, owned_paths=layout.owned, file_type=fake_fs([])
        )


def test_every_bind_path_is_absolute_and_identical_or_under_the_runtime_dir(layout: Layout) -> None:
    authority = str(layout.home / ".Xauthority")
    result = run(
        layout,
        {
            "WAYLAND_DISPLAY": "wayland-1",
            "DISPLAY": ":0",
            "XAUTHORITY": authority,
            "XDG_RUNTIME_DIR": HOST_RUNTIME,
        },
        present=[WAYLAND, X0, X11_DIR, authority, PIPEWIRE, PULSE],
        kinds={authority: stat.S_IFREG},
    )
    for bind in result.binds:
        assert bind.host.startswith("/")
        assert bind.realm.startswith("/")
        assert bind.realm == bind.host or bind.realm.startswith(REALM_RUNTIME + "/")


@pytest.mark.fr("FR-061")
def test_session_binds_feed_the_attach_plan_unchanged(layout: Layout) -> None:
    """ADR-008 pairs and the attach entries come from one list, so they agree."""
    authority = str(layout.home / ".Xauthority")
    host = {
        "WAYLAND_DISPLAY": "wayland-1",
        "DISPLAY": ":0",
        "XAUTHORITY": authority,
        "XDG_RUNTIME_DIR": HOST_RUNTIME,
    }
    present = [WAYLAND, X0, X11_DIR, authority, PIPEWIRE, PULSE]
    result = run(layout, host, present=present, kinds={authority: stat.S_IFREG})
    plan = attach_plan(
        Tier.BASIC,
        host_home=layout.home,
        uid=UID,
        user=USER,
        owned_paths=layout.owned,
        session_binds=result.binds,
        env=host,
        exists=present_only([*present, str(layout.home / "Downloads")]),
    )
    session_triples = [
        (entry.host, entry.realm, entry.mode) for entry in plan.attach if entry.kind == "session"
    ]
    bind_triples = [(os.path.realpath(bind.host), bind.realm, bind.mode) for bind in result.binds]
    assert session_triples == bind_triples


@pytest.mark.fr("FR-064")
@pytest.mark.fr("FR-084")
def test_missing_wayland_socket_keeps_its_bind_for_a_later_retry(layout: Layout) -> None:
    result = run(layout, {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": HOST_RUNTIME})
    [skip] = result.skipped
    assert (skip.host, skip.realm, skip.mode, skip.is_dir, skip.missing) == (
        WAYLAND,
        WAYLAND_REALM,
        "rw",
        False,
        True,
    )


@pytest.mark.fr("FR-064")
def test_a_source_of_another_type_is_never_kept_for_a_retry(layout: Layout) -> None:
    host_env = {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": HOST_RUNTIME}
    result = run(layout, host_env, [WAYLAND], kinds={WAYLAND: stat.S_IFREG})
    [skip] = result.skipped
    assert skip.missing is False
    assert skip.realm == WAYLAND_REALM


@pytest.mark.fr("FR-064")
def test_missing_display_socket_keeps_the_directory_bind_for_a_retry(layout: Layout) -> None:
    result = run(layout, {"DISPLAY": ":0", "XDG_RUNTIME_DIR": HOST_RUNTIME}, [X11_DIR])
    [skip] = result.skipped
    assert (skip.host, skip.realm, skip.mode, skip.is_dir, skip.missing) == (
        X11_DIR,
        X11_DIR,
        "ro",
        True,
        True,
    )


AUDIO_REALMS = (f"{REALM_RUNTIME}/pipewire-0", f"{REALM_RUNTIME}/pulse/native")


@pytest.mark.fr("FR-084")
def test_absent_audio_sockets_are_kept_for_a_retry_and_not_reported(layout: Layout) -> None:
    result = run(layout, {"XDG_RUNTIME_DIR": HOST_RUNTIME})
    assert result.skipped == []
    audio = [(bind.host, bind.realm) for bind in result.absent if bind.realm in AUDIO_REALMS]
    assert audio == [
        (PIPEWIRE, f"{REALM_RUNTIME}/pipewire-0"),
        (PULSE, f"{REALM_RUNTIME}/pulse/native"),
    ]


@pytest.mark.fr("FR-084")
def test_a_present_audio_socket_is_not_listed_as_absent(layout: Layout) -> None:
    result = run(layout, {"XDG_RUNTIME_DIR": HOST_RUNTIME}, [PIPEWIRE])
    assert [bind.host for bind in result.absent if bind.realm in AUDIO_REALMS] == [PULSE]


@pytest.mark.fr("FR-084")
def test_an_empty_session_leaves_every_fixed_path_as_a_slot_with_no_host(layout: Layout) -> None:
    """An autostart has no session variables. Each fixed path is still a slot to fill later."""
    result = run(layout, {})
    assert result.skipped == []
    assert result.binds == []
    realms = {bind.realm for bind in result.absent}
    assert realms == {
        WAYLAND_REALM,
        AUTHORITY_REALM,
        X11_DIR,
        f"{REALM_RUNTIME}/pipewire-0",
        f"{REALM_RUNTIME}/pulse/native",
    }
    hosts = {bind.realm: bind.host for bind in result.absent if bind.realm not in AUDIO_REALMS}
    assert set(hosts.values()) == {""}


@pytest.mark.fr("FR-064")
def test_a_skip_without_a_bind_still_names_its_fixed_realm_path(layout: Layout) -> None:
    result = run(layout, {"WAYLAND_DISPLAY": "bad name", "XDG_RUNTIME_DIR": HOST_RUNTIME})
    [skip] = result.skipped
    assert (skip.reason, skip.realm, skip.missing) == ("not a socket name", WAYLAND_REALM, False)
