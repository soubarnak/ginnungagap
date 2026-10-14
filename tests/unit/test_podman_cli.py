# SPDX-License-Identifier: Apache-2.0
"""PodmanCli: verbose echo, digest-only pulls, retries, error mapping, env allowlist, replay shim.

Most tests run the real PodmanCli against tests/fakes/podman_bin/podman. The shim replays
captured fixtures and never runs a real podman, so these tests need no container runtime.

PodmanCli passes podman only an allowlist of the parent environment (CR-006), so the shim cannot
read its settings from the test process. A wrapper named `podman` comes first on PATH. It sources
a settings file and then runs the shim. The tests change the settings with `use_cases` and
`use_fixture_dir`, which write that file.
"""

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ginn.errors import GinnError
from ginn.ops import retry
from ginn.podman import cli as podman_cli
from ginn.podman.adapter import PATH_NOT_FOUND_REASON, CreateSpec, Podman, RetryHook
from ginn.podman.cli import PodmanCli
from ginn.store.paths import OwnedPaths

REPO = Path(__file__).resolve().parents[2]
SHIM_DIR = REPO / "tests" / "fakes" / "podman_bin"
FIXTURES = REPO / "tests" / "fixtures" / "podman"
DIGEST = "sha256:d56c381f961d307a21b3ca004cf1e3910f106644aefb1f43e654c8a56c4fd395"
DIGEST_REF = f"docker.io/library/alpine@{DIGEST}"
ALPINE_ID = "320994c3b997e2ec6433f717f153e108023c5bec8fefa8d76b83451d16d05ea8"
CONTAINER = "01a08e08c6e7270161286e5d469a09bac2727b1abcd1a7761e236910be6ea4de"
NETWORK_STDERR = (
    "dial tcp: lookup registry-1.docker.io: no such host",
    "net/http: TLS handshake timeout",
    "connect: no route to host",
    "lookup registry-1.docker.io: temporary failure in name resolution",
    "dial tcp 52.1.2.3:443: connect: connection refused",
    "read tcp 10.0.2.15:41622->52.1.2.3:443: i/o timeout",
    "unexpected EOF",
    "pinging container registry: received unexpected HTTP status: 503 Service Unavailable",
    "toomanyrequests: 429 Too Many Requests",
)
# Parent variables that a user's own Podman setup reads. None may reach podman (CR-006, DR-010).
LEAKED_ENV = {
    "CONTAINER_HOST": "unix:///run/user/1000/podman/podman.sock",
    "CONTAINER_CONNECTION": "remote-box",
    "CONTAINER_SSHKEY": "/home/user/.ssh/id_ed25519",
    "STORAGE_DRIVER": "vfs",
    "STORAGE_OPTS": "overlay.mountopt=nodev",
    "CONTAINERS_CONF_OVERRIDE": "/home/user/override.conf",
    "CONTAINERS_REGISTRIES_CONF": "/home/user/registries.conf",
    "XDG_CONFIG_HOME": "/home/user/.config-alt",
    "XDG_DATA_HOME": "/home/user/.data-alt",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "GINN_UNRELATED_SECRET": "not-for-podman",
}
# Proxy variables pass through, so a pull behind a proxy still reaches the registry (CR-006).
PROXY_ENV = {
    "HTTP_PROXY": "http://proxy.example:3128",
    "HTTPS_PROXY": "http://proxy.example:3128",
    "NO_PROXY": "localhost,127.0.0.1",
    "http_proxy": "http://proxy.example:3128",
    "https_proxy": "http://proxy.example:3128",
    "no_proxy": "localhost,127.0.0.1",
}
# Parent variables that pass through. PATH stays as it is: subprocess finds `podman` with it.
PASSED_ENV = {
    "HOME": "/home/tester",
    "TERM": "xterm-256color",
    "USER": "tester",
    "LOGNAME": "tester",
    "LANG": "en_GB.UTF-8",
    "LC_ALL": "en_GB.UTF-8",
    **PROXY_ENV,
}
PINNED_ENV = {"XDG_RUNTIME_DIR", "CONTAINERS_CONF", "CONTAINERS_STORAGE_CONF", "TMPDIR"}
SETTINGS_ENV = "GINN_FAKE_PODMAN_SETTINGS"
Verbose = Callable[[str], None]


def _read_settings(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, quoted = line.removeprefix("export ").partition("=")
        values[name] = shlex.split(quoted)[0]
    return values


def _write_settings(path: Path, values: dict[str, str]) -> None:
    text = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in values.items())
    path.write_text(text, encoding="utf-8")


def _set_shim(name: str, value: str | None) -> None:
    """Set one shim variable for the next call, or unset it when `value` is None."""
    path = Path(os.environ[SETTINGS_ENV])
    values = _read_settings(path)
    if value is None:
        values.pop(name, None)
    else:
        values[name] = value
    _write_settings(path, values)


@pytest.fixture
def shim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put the podman wrapper for the replay shim first on PATH, and return the shim's log."""
    log = tmp_path / "shim.jsonl"
    settings = tmp_path / "shim-settings.sh"
    bin_dir = tmp_path / "shim-bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "podman"
    wrapper.write_text(
        "#!/bin/sh\n"
        f". {shlex.quote(str(settings))}\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(SHIM_DIR / "podman"))} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]))
    # The test process only. PodmanCli never passes this name to podman.
    monkeypatch.setenv(SETTINGS_ENV, str(settings))
    _write_settings(
        settings,
        {"GINN_FAKE_PODMAN_FIXTURES": str(FIXTURES), "GINN_FAKE_PODMAN_LOG": str(log)},
    )
    return log


def use_cases(cases: str) -> None:
    """Map shim route keys to fixture cases, as `key=case,key=case`."""
    _set_shim("GINN_FAKE_PODMAN_CASE", cases)


def use_fixture_dir(tmp_path: Path, cases: dict[str, Any]) -> None:
    """Replay the given envelopes from a private fixture directory, with no case mapping."""
    directory = tmp_path / "cases"
    directory.mkdir()
    for name, body in cases.items():
        (directory / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")
    _set_shim("GINN_FAKE_PODMAN_FIXTURES", str(directory))
    _set_shim("GINN_FAKE_PODMAN_CASE", None)


def spy_env(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str] | None]:
    """Record the `env` argument of every podman client that starts, and run each one as usual.

    `subprocess.run` starts its child with Popen, and exec_interactive uses Popen directly, so the
    spy is on Popen: each call is recorded once.
    """
    seen: list[dict[str, str] | None] = []
    real_popen = subprocess.Popen

    class SpyPopen(real_popen):  # type: ignore[valid-type,misc]
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            seen.append(kwargs.get("env"))
            super().__init__(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", SpyPopen)
    return seen


def host_paths(tmp_path: Path) -> OwnedPaths:
    """Owned Paths under tmp_path, with the runtime root laid out as Host Setup leaves it.

    The runtime directory is /run/ginn/<uid> under tmp_path, mode 0700 and owned by the test user.
    """
    system_run = tmp_path / "run"
    (system_run / "ginn" / str(os.getuid())).mkdir(parents=True, mode=0o700, exist_ok=True)
    return OwnedPaths.from_env({"HOME": str(tmp_path / "home")}, system_run=system_run)


def configured_paths(tmp_path: Path) -> OwnedPaths:
    """Owned Paths with the owned directories and ginn's config files in place."""
    paths = host_paths(tmp_path)
    paths.ensure()
    paths.ensure_config()
    return paths


def explicit_cli(paths: OwnedPaths) -> PodmanCli:
    """The explicit constructor. It has no Owned Paths, so it never writes ginn's config files."""
    return PodmanCli(
        paths.podman_root,
        paths.podman_runroot,
        tmpdir=paths.podman_tmpdir,
        image_tmp=paths.image_tmp,
        containers_conf=paths.podman_containers_conf,
        storage_conf=paths.podman_storage_conf,
        runtime_root=paths.runtime,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def make_cli(tmp_path: Path, verbose: Verbose | None = None) -> PodmanCli:
    return PodmanCli.from_paths(configured_paths(tmp_path), verbose=verbose)


def logged(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def global_flags(tmp_path: Path) -> list[str]:
    """The global flags of a default adapter, which names the owned CDI directory (FR-069)."""
    paths = host_paths(tmp_path)
    return [
        "--remote=false",
        "--root",
        str(paths.podman_root),
        "--runroot",
        str(paths.podman_runroot),
        "--tmpdir",
        str(paths.podman_tmpdir),
        "--runtime",
        "crun",
        "--cdi-spec-dir",
        str(paths.cdi),
    ]


def expect_error(call: Callable[[], object]) -> GinnError:
    with pytest.raises(GinnError) as info:
        call()
    return info.value


@pytest.mark.fr("NFR-042")
def test_verbose_gets_the_exact_argv_before_podman_runs(shim: Path, tmp_path: Path) -> None:
    seen: list[tuple[str, bool]] = []
    cli = make_cli(tmp_path, lambda line: seen.append((line, shim.exists())))
    cli.info()
    expected = ["podman", *global_flags(tmp_path), "info", "--format", "json"]
    assert seen == [(shlex.join(expected), False)]
    assert cli.history == [expected]
    assert logged(shim)[0]["argv"] == expected[1:]


@pytest.mark.fr("NFR-042")
def test_verbose_line_splits_back_to_the_same_argv(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases("exec=exec-ok")
    lines: list[str] = []
    cli = make_cli(tmp_path, lines.append)
    cli.exec(CONTAINER, ["sh", "-c", "echo 'a b' && printf '%s\\n' \"$HOME\""])
    assert len(lines) == 1
    assert shlex.split(lines[0]) == cli.history[0]
    assert lines[0] == shlex.join(cli.history[0])


@pytest.mark.fr("IR-020")
def test_global_flags_come_before_the_subcommand(shim: Path, tmp_path: Path) -> None:
    paths = configured_paths(tmp_path)
    cli = PodmanCli.from_paths(paths, cdi_spec_dir=tmp_path / "cdi")
    cli.info()
    argv = cli.history[0]
    assert argv[1:] == [
        "--remote=false",
        "--root",
        str(paths.podman_root),
        "--runroot",
        str(paths.podman_runroot),
        "--tmpdir",
        str(paths.podman_tmpdir),
        "--runtime",
        "crun",
        "--cdi-spec-dir",
        str(tmp_path / "cdi"),
        "info",
        "--format",
        "json",
    ]


@pytest.mark.fr("IR-020")
def test_remote_mode_is_forced_off_even_when_container_host_is_set(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # podman(1): a set CONTAINER_HOST makes --remote default to true. The flag overrides that.
    monkeypatch.setenv("CONTAINER_HOST", "unix:///run/user/1000/podman/podman.sock")
    cli = make_cli(tmp_path)
    cli.info()
    assert cli.history[0][1] == "--remote=false"
    assert logged(shim)[0]["argv"][0] == "--remote=false"


@pytest.mark.fr("DR-010")
def test_tmpdir_and_image_tmp_are_required_so_podman_never_uses_the_users_tmp(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError):
        PodmanCli(  # type: ignore[call-arg]
            tmp_path / "root",
            tmp_path / "runroot",
            containers_conf=tmp_path / "containers.conf",
            storage_conf=tmp_path / "storage.conf",
            runtime_root=tmp_path / "runtime",
        )


@pytest.mark.fr("DR-010")
def test_every_call_carries_root_runroot_tmpdir_and_runtime(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases("exec=exec-ok,image-exists=image-exists-absent")
    flags = global_flags(tmp_path)
    cli = make_cli(tmp_path)
    cli.info()
    cli.ps()
    cli.logs(CONTAINER, 1)
    cli.top(CONTAINER)
    assert cli.image_exists(DIGEST_REF) is False
    cli.exec(CONTAINER, ["true"])
    cli.exec(CONTAINER, ["true"], capture=False)
    assert len(cli.history) == 7
    for argv in cli.history:
        assert argv[0] == "podman"
        assert argv[1 : 1 + len(flags)] == flags, argv
    # The shim logs the argv without the program name.
    assert [entry["argv"][0 : len(flags)] for entry in logged(shim)] == [flags] * 7


@pytest.mark.fr("FR-069")
def test_the_owned_cdi_directory_is_named_on_every_call_by_default(
    shim: Path, tmp_path: Path
) -> None:
    """`from_paths` names the owned CDI directory before the subcommand, so `start` resolves
    the NVIDIA spec that `create` writes there (FR-069, S-06). An explicit directory still wins."""
    paths = configured_paths(tmp_path)
    cli = PodmanCli.from_paths(paths)
    cli.info()
    argv = cli.history[0]
    assert argv[argv.index("--cdi-spec-dir") + 1] == str(paths.cdi)
    assert argv.index("--cdi-spec-dir") < argv.index("info")

    other = PodmanCli.from_paths(paths, cdi_spec_dir=tmp_path / "other-cdi")
    other.info()
    assert other.history[0][other.history[0].index("--cdi-spec-dir") + 1] == str(
        tmp_path / "other-cdi"
    )


@pytest.mark.fr("IR-020")
def test_runtime_defaults_to_crun_and_is_configurable(shim: Path, tmp_path: Path) -> None:
    paths = configured_paths(tmp_path)
    cli = PodmanCli.from_paths(paths)
    cli.info()
    assert cli.history[0][cli.history[0].index("--runtime") + 1] == "crun"

    runc = PodmanCli.from_paths(paths, oci_runtime="runc")
    runc.info()
    assert runc.history[0][runc.history[0].index("--runtime") + 1] == "runc"

    with pytest.raises(ValueError, match="oci runtime"):
        PodmanCli.from_paths(paths, oci_runtime="-crun")


@pytest.mark.fr("DR-010")
@pytest.mark.fr("CR-006")
def test_user_podman_settings_in_the_parent_environment_never_reach_podman(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = spy_env(monkeypatch)
    for name, value in LEAKED_ENV.items():
        monkeypatch.setenv(name, value)
    for name, value in PASSED_ENV.items():
        monkeypatch.setenv(name, value)
    # A login session exports its own runtime directory. Podman must not see it.
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    paths = configured_paths(tmp_path)
    cli = PodmanCli.from_paths(paths)
    cli.info()
    use_cases("exec=exec-ok")
    cli.exec_interactive(CONTAINER, ["true"])

    assert len(seen) == 2
    for env in seen:
        assert env is not None
        for name in LEAKED_ENV:
            assert name not in env, name
        assert env["XDG_RUNTIME_DIR"] == str(paths.runtime)
        assert env["CONTAINERS_CONF"] == str(paths.podman_containers_conf)
        assert env["CONTAINERS_STORAGE_CONF"] == str(paths.podman_storage_conf)
        assert env["TMPDIR"] == str(paths.image_tmp)
        assert env["PATH"] == os.environ["PATH"]
        for name, value in PASSED_ENV.items():
            assert env[name] == value, name
        allowed = PINNED_ENV | set(PASSED_ENV) | {"PATH"}
        assert all(name in allowed or name.startswith("LC_") for name in env), sorted(env)


@pytest.mark.fr("CR-006")
def test_image_downloads_use_the_owned_image_tmp_on_disk(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = spy_env(monkeypatch)
    paths = configured_paths(tmp_path)
    PodmanCli.from_paths(paths).info()
    env = seen[0]
    assert env is not None
    assert env["TMPDIR"] == str(paths.image_tmp)
    # Images are large, so the directory is on disk under the data root, not on tmpfs under runtime.
    assert paths.image_tmp.is_relative_to(paths.data)
    assert not paths.image_tmp.is_relative_to(paths.runtime)


@pytest.mark.fr("DR-010")
@pytest.mark.fr("NFR-040")
@pytest.mark.parametrize("config", ["containers.conf", "storage.conf"])
def test_the_explicit_constructor_refuses_a_missing_config_file_before_podman_runs(
    shim: Path, tmp_path: Path, config: str
) -> None:
    paths = configured_paths(tmp_path)
    cli = explicit_cli(paths)
    missing = paths.config / config
    missing.unlink()
    err = expect_error(cli.info)
    assert err.code == "GINN-E-INTERNAL"
    assert str(missing) in str(err.details["detail"])
    assert cli.history == []
    assert logged(shim) == []


@pytest.mark.fr("DR-010")
@pytest.mark.fr("NFR-040")
def test_from_paths_writes_missing_config_files_before_each_call(
    shim: Path, tmp_path: Path
) -> None:
    paths = host_paths(tmp_path)
    paths.ensure()
    assert not paths.podman_containers_conf.exists()
    assert not paths.podman_storage_conf.exists()
    cli = PodmanCli.from_paths(paths)
    cli.info()
    assert _mode(paths.podman_containers_conf) == 0o600
    assert _mode(paths.podman_storage_conf) == 0o600
    # A file removed later is written again before the next call, and the call succeeds.
    paths.podman_storage_conf.unlink()
    cli.info()
    assert paths.podman_storage_conf.exists()
    assert len(logged(shim)) == 2


@pytest.mark.fr("DR-010")
@pytest.mark.fr("CR-006")
def test_from_paths_refuses_a_symbolic_link_in_place_of_a_config_file_and_writes_nothing(
    shim: Path, tmp_path: Path
) -> None:
    paths = host_paths(tmp_path)
    paths.ensure()
    outside = tmp_path / "outside.conf"
    paths.podman_containers_conf.parent.mkdir(parents=True, exist_ok=True)
    paths.podman_containers_conf.symlink_to(outside)
    cli = PodmanCli.from_paths(paths)
    err = expect_error(cli.info)
    assert err.code == "GINN-E-INTERNAL"
    assert "symbolic link" in str(err.details["detail"])
    assert cli.history == []
    assert logged(shim) == []
    assert not outside.exists()


@pytest.mark.fr("DR-010")
@pytest.mark.fr("CR-006")
@pytest.mark.parametrize(
    "damage",
    ["config-symlink", "config-directory", "tmpdir-missing", "tmpdir-symlink", "tmpdir-file"],
)
def test_a_config_file_or_tmpdir_that_is_not_a_plain_path_refuses_the_call(
    shim: Path, tmp_path: Path, damage: str
) -> None:
    paths = configured_paths(tmp_path)
    cli = explicit_cli(paths)
    if damage == "config-symlink":
        target = paths.podman_containers_conf
        copy = tmp_path / "copy.conf"
        copy.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(copy)
    elif damage == "config-directory":
        target = paths.podman_containers_conf
        target.unlink()
        target.mkdir()
    else:
        target = paths.image_tmp
        target.rmdir()
        if damage == "tmpdir-symlink":
            real = tmp_path / "real-tmp"
            real.mkdir()
            target.symlink_to(real)
        elif damage == "tmpdir-file":
            target.write_text("not a directory", encoding="utf-8")
    err = expect_error(cli.info)
    assert err.code == "GINN-E-INTERNAL"
    assert str(target) in str(err.details["detail"])
    assert cli.history == []
    assert logged(shim) == []


@pytest.mark.fr("DR-033")
@pytest.mark.fr("FR-123")
def test_session_and_supervisor_podman_both_see_the_one_runtime_root(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = spy_env(monkeypatch)
    system_run = tmp_path / "run"
    runtime = system_run / "ginn" / str(os.getuid())
    runtime.mkdir(parents=True, mode=0o700)
    home = str(tmp_path / "home")
    # The supervisor exports XDG_RUNTIME_DIR=/run/ginn/<uid>; a session exports /run/user/<uid>.
    # Both must reach podman as the same runtime root.
    for parent_xdg in (str(runtime), "/run/user/1000"):
        env = {"HOME": home, "XDG_RUNTIME_DIR": parent_xdg}
        paths = OwnedPaths.from_env(env, system_run=system_run)
        paths.ensure()
        paths.ensure_config()
        PodmanCli.from_paths(paths).info()
    assert [env["XDG_RUNTIME_DIR"] for env in seen if env is not None] == [
        str(runtime),
        str(runtime),
    ]
    assert len(logged(shim)) == 2
    flags = global_flags(tmp_path)
    assert all(entry["argv"][0 : len(flags)] == flags for entry in logged(shim))


@pytest.mark.fr("DR-010")
@pytest.mark.fr("NFR-040")
def test_a_fresh_tree_needs_no_setup_before_a_from_paths_call(shim: Path, tmp_path: Path) -> None:
    # Only the runtime root exists, as the supervisor leaves it. No owned directory or config yet.
    paths = host_paths(tmp_path)
    assert not paths.data.exists()
    assert not paths.config.exists()
    PodmanCli.from_paths(paths).info()
    assert _mode(paths.image_tmp) == 0o700
    assert _mode(paths.podman_containers_conf) == 0o600
    assert _mode(paths.podman_storage_conf) == 0o600
    assert len(logged(shim)) == 1


@pytest.mark.fr("FR-025")
def test_a_wider_owned_directory_is_tightened_and_recorded_without_printing(
    shim: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An owned directory left at 0755 is tightened by the next call. The adapter records the change
    # for the CLI to print, and writes nothing itself.
    paths = host_paths(tmp_path)
    paths.data.mkdir(parents=True)
    os.chmod(paths.data, 0o755)  # noqa: S103 - fixture mode bits, not a host file
    cli = PodmanCli.from_paths(paths)
    cli.info()
    assert _mode(paths.data) == 0o700
    assert len(cli.tightened) == 1
    assert str(paths.data) in cli.tightened[0]
    assert "from 755 to 700" in cli.tightened[0]
    out, err = capsys.readouterr()
    assert out == "" and err == ""
    # The next call has nothing left to tighten, and the earlier notice is kept.
    cli.info()
    assert len(cli.tightened) == 1


@pytest.mark.fr("DR-010")
@pytest.mark.fr("NFR-040")
@pytest.mark.parametrize("damage", ["missing", "wrong-mode"])
def test_from_paths_never_creates_or_repairs_the_runtime_root(
    shim: Path, tmp_path: Path, damage: str
) -> None:
    # The runtime root belongs to the supervisor (ADR-004). A call refuses it and changes nothing.
    paths = host_paths(tmp_path)
    if damage == "missing":
        paths.runtime.rmdir()
    else:
        os.chmod(paths.runtime, 0o755)  # noqa: S103 - fixture mode bits, not a host file
    cli = PodmanCli.from_paths(paths)
    err = expect_error(cli.info)
    if damage == "missing":
        assert err.code == "GINN-E-HOST-NOT-SET-UP"
        assert not paths.runtime.exists()
    else:
        assert err.code == "GINN-E-RUNTIME-DIR-UNSAFE"
        assert _mode(paths.runtime) == 0o755
    assert not paths.data.exists()
    assert not paths.config.exists()
    assert cli.history == []
    assert logged(shim) == []


@pytest.mark.fr("FR-011")
def test_pull_by_tag_is_refused_before_podman_runs(shim: Path, tmp_path: Path) -> None:
    lines: list[str] = []
    cli = make_cli(tmp_path, lines.append)
    with pytest.raises(ValueError, match="FR-011"):
        cli.pull("docker.io/library/alpine:3")
    assert lines == []
    assert cli.history == []
    assert logged(shim) == []


@pytest.mark.fr("FR-011")
def test_pull_by_digest_then_inspects_the_image(shim: Path, tmp_path: Path) -> None:
    use_cases("pull=pull-ok,image-inspect=image-inspect-alpine")
    cli = make_cli(tmp_path)
    image = cli.pull(DIGEST_REF)
    assert image.id == ALPINE_ID
    assert image.digest == DIGEST
    flags = global_flags(tmp_path)
    assert cli.history == [
        ["podman", *flags, "pull", "--quiet", DIGEST_REF],
        ["podman", *flags, "image", "inspect", "--format", "json", DIGEST_REF],
    ]


@pytest.mark.fr("FR-011")
def test_digest_mismatch_fixture_maps_to_image_unverified(shim: Path, tmp_path: Path) -> None:
    use_cases("pull=pull-digest-mismatch")
    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF))
    assert err.code == "GINN-E-IMAGE-UNVERIFIED"
    assert err.exit_code == 65
    assert err.details["distro"] == "docker.io/library/alpine"
    assert err.details["exit"] == 125
    assert "Digest did not match" in str(err.details["stderr"])


@pytest.mark.fr("FR-120")
def test_network_pull_fixture_maps_to_network(shim: Path, tmp_path: Path) -> None:
    use_cases("pull=pull-network-error")
    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF))
    assert err.code == "GINN-E-NETWORK"
    assert err.exit_code == 69
    assert err.details["exit"] == 125
    assert str(err.details["stderr"]).startswith("Error: initializing source docker://")
    assert "dial tcp" in str(err.details["stderr"])


@pytest.mark.fr("NFR-040")
def test_unknown_container_fixture_maps_to_internal(shim: Path, tmp_path: Path) -> None:
    use_cases("container-inspect=container-inspect-missing")
    err = expect_error(lambda: make_cli(tmp_path).inspect("nonexist"))
    assert err.code == "GINN-E-INTERNAL"
    assert err.exit_code == 70
    assert err.details["exit"] == 125
    assert err.details["stderr"] == 'Error: no such container "nonexist"'


@pytest.mark.fr("NFR-040")
def test_image_exists_is_false_for_exit_one_and_true_for_exit_zero(
    shim: Path, tmp_path: Path
) -> None:
    use_cases("image-exists=image-exists-absent")
    assert make_cli(tmp_path).image_exists(DIGEST_REF) is False
    use_fixture_dir(tmp_path, {"image-exists": {"exit": 0}})
    assert make_cli(tmp_path).image_exists(DIGEST_REF) is True


@pytest.mark.fr("FR-120")
@pytest.mark.parametrize("line", NETWORK_STDERR)
def test_registry_and_network_messages_map_to_network(
    line: str, shim: Path, tmp_path: Path
) -> None:
    use_fixture_dir(
        tmp_path,
        {"pull": {"stdout": "", "stderr": f"Error: pinging registry: {line}\n", "exit": 125}},
    )
    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF))
    assert err.code == "GINN-E-NETWORK"


@pytest.mark.fr("NFR-040")
def test_network_text_with_another_exit_status_is_internal(shim: Path, tmp_path: Path) -> None:
    use_fixture_dir(
        tmp_path,
        {"pull": {"stdout": "", "stderr": "Error: dial tcp: i/o timeout\n", "exit": 1}},
    )
    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF))
    assert err.code == "GINN-E-INTERNAL"


@pytest.mark.fr("FR-069")
def test_unresolvable_cdi_devices_map_to_nvidia_cdi_not_internal(
    shim: Path, tmp_path: Path
) -> None:
    """A GPU start whose CDI devices podman cannot resolve is GINN-E-NVIDIA-CDI, with the decisive
    line, and not an internal error (FR-069). The remedy names the spec and `ginn doctor`."""
    stderr = "Error: setting up CDI devices: unresolvable CDI devices nvidia.com/gpu=all\n"
    use_fixture_dir(tmp_path, {"start": {"stdout": "", "stderr": stderr, "exit": 125}})
    err = expect_error(lambda: make_cli(tmp_path).start(CONTAINER))
    assert err.code == "GINN-E-NVIDIA-CDI"
    assert err.exit_code == 1
    assert err.details["exit"] == 125
    assert str(err.details["stderr"]).endswith("unresolvable CDI devices nvidia.com/gpu=all")
    assert "ginn doctor" in err.remedy


@pytest.mark.fr("NFR-040")
def test_status_code_inside_a_digest_is_not_an_http_status(shim: Path, tmp_path: Path) -> None:
    hexdigest = "sha256:" + "a503" + "0" * 60
    use_fixture_dir(
        tmp_path,
        {"exec": {"stdout": "", "stderr": f"Error: no blob {hexdigest}\n", "exit": 125}},
    )
    err = expect_error(lambda: make_cli(tmp_path).start(CONTAINER))
    assert err.code == "GINN-E-INTERNAL"


@pytest.mark.fr("FR-120")
def test_retry_hook_runs_three_times_then_network_error(shim: Path, tmp_path: Path) -> None:
    use_cases("pull=pull-network-error")
    seen: list[tuple[int, str]] = []

    def hook(attempt: int, err: GinnError) -> bool:
        seen.append((attempt, err.code))
        return True

    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF, retry_hook=hook))
    assert err.code == "GINN-E-NETWORK"
    assert seen == [(1, "GINN-E-NETWORK"), (2, "GINN-E-NETWORK"), (3, "GINN-E-NETWORK")]
    assert sum(1 for entry in logged(shim) if "pull" in entry["argv"]) == 4


@pytest.mark.fr("FR-120")
def test_retry_hook_returning_false_stops_after_one_attempt(shim: Path, tmp_path: Path) -> None:
    use_cases("pull=pull-network-error")
    calls: list[int] = []

    def hook(attempt: int, err: GinnError) -> bool:
        calls.append(attempt)
        return False

    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF, retry_hook=hook))
    assert err.code == "GINN-E-NETWORK"
    assert calls == [1]
    assert sum(1 for entry in logged(shim) if "pull" in entry["argv"]) == 1


@pytest.mark.fr("FR-011")
def test_digest_mismatch_is_never_retried(shim: Path, tmp_path: Path) -> None:
    use_cases("pull=pull-digest-mismatch")
    calls: list[int] = []

    def hook(attempt: int, err: GinnError) -> bool:
        calls.append(attempt)
        return True

    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF, retry_hook=hook))
    assert err.code == "GINN-E-IMAGE-UNVERIFIED"
    assert calls == []


@pytest.mark.fr("IR-020")
def test_info_is_parsed_from_the_captured_output(shim: Path, tmp_path: Path) -> None:
    info = make_cli(tmp_path).info()
    assert info.version == "5.8.8"
    assert info.rootless is True
    assert info.oci_runtime == "runc"


@pytest.mark.fr("IR-020")
def test_ps_passes_label_filters_and_parses_both_containers(shim: Path, tmp_path: Path) -> None:
    containers = make_cli(tmp_path).ps({"ginn.realm": "t211"})
    assert [c.name for c in containers] == ["t211-alpine", "t211-run"]
    argv = logged(shim)[0]["argv"]
    assert argv[argv.index("--filter") + 1] == "label=ginn.realm=t211"
    assert "--all" in argv


@pytest.mark.fr("IR-020")
def test_inspect_parses_the_container_fixture(shim: Path, tmp_path: Path) -> None:
    use_cases("container-inspect=container-inspect-alpine")
    container = make_cli(tmp_path).inspect(CONTAINER)
    assert container.name == "t211-alpine"
    assert container.image == "docker.io/library/alpine:3"


@pytest.mark.fr("FR-011")
def test_create_returns_id_and_refuses_privileged_spec(shim: Path, tmp_path: Path) -> None:
    use_cases("create=create-ok")
    cli = make_cli(tmp_path)
    spec = CreateSpec(name="ginn-arch-g1", image_digest_ref=DIGEST_REF, network="host")
    assert cli.create(spec) == CONTAINER
    assert logged(shim)[0]["argv"][-1] == DIGEST_REF


@pytest.mark.fr("NFR-SEC-001")
def test_create_refuses_privileged_spec_before_podman_runs(shim: Path, tmp_path: Path) -> None:
    cli = make_cli(tmp_path)
    spec = CreateSpec(
        name="ginn-arch-g1",
        image_digest_ref=DIGEST_REF,
        network="host",
        security_opts=["--privileged"],
    )
    with pytest.raises(ValueError, match="NFR-SEC-001"):
        cli.create(spec)
    assert cli.history == []
    assert logged(shim) == []


@pytest.mark.fr("NFR-SEC-005")
def test_exec_builds_an_argv_list_with_option_terminator(shim: Path, tmp_path: Path) -> None:
    use_cases("exec=exec-ok")
    result = make_cli(tmp_path).exec(
        CONTAINER,
        ["sh", "-c", "echo hi"],
        user="1000:1000",
        env={"B": "2", "A": "1"},
        workdir="/home/alice",
    )
    assert result.exit_code == 0
    assert result.stdout == "hi\n"
    argv = logged(shim)[0]["argv"]
    assert argv[-5:] == ["--", CONTAINER, "sh", "-c", "echo hi"]
    assert argv[argv.index("exec") + 1 : argv.index("--")] == [
        "--user",
        "1000:1000",
        "--env",
        "A=1",
        "--env",
        "B=2",
        "--workdir",
        "/home/alice",
    ]


@pytest.mark.fr("FR-068")
def test_exec_interactive_hands_the_client_pid_to_the_caller_before_it_waits(
    shim: Path, tmp_path: Path
) -> None:
    """`on_spawn` gets the podman client's pid as it starts, so enter can forward a signal to it."""
    use_fixture_dir(tmp_path, {"exec": {"stdout": "", "stderr": "", "exit": 7}})
    seen: list[int] = []
    assert make_cli(tmp_path).exec_interactive(CONTAINER, ["false"], on_spawn=seen.append) == 7
    assert len(seen) == 1 and seen[0] > 0 and seen[0] != os.getpid()


@pytest.mark.fr("FR-068")
def test_exec_interactive_calls_on_exit_once_the_wait_has_reaped_the_client(
    shim: Path, tmp_path: Path
) -> None:
    """`on_exit` runs right after the wait returns, and by then the client's pid is reaped, so the
    caller cannot signal a pid that was reused (FR-068)."""
    use_fixture_dir(tmp_path, {"exec": {"stdout": "", "stderr": "", "exit": 7}})
    pids: list[int] = []
    reaped: list[bool] = []

    def on_exit() -> None:
        try:
            os.kill(pids[0], 0)
        except ProcessLookupError:
            reaped.append(True)
        else:
            reaped.append(False)

    status = make_cli(tmp_path).exec_interactive(
        CONTAINER, ["false"], on_spawn=pids.append, on_exit=on_exit
    )
    assert status == 7
    assert reaped == [True]


@pytest.mark.fr("FR-068")
def test_a_signal_sent_to_the_client_ends_it_and_the_status_is_128_plus_the_signal(
    tmp_path: Path,
) -> None:
    """No handler is involved: a real client (a shell that execs sleep) is sent SIGTERM through its
    pid, and the wait returns 143, as the convention for a command that a signal ends (FR-068)."""
    wrapper = tmp_path / "podman-sleep"
    wrapper.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    wrapper.chmod(0o755)
    cli = PodmanCli.from_paths(configured_paths(tmp_path), binary=str(wrapper))
    status = cli.exec_interactive(
        CONTAINER, ["true"], on_spawn=lambda pid: os.kill(pid, signal.SIGTERM)
    )
    assert status == 128 + signal.SIGTERM


@pytest.mark.fr("IR-011")
def test_exec_interactive_returns_the_inner_status(shim: Path, tmp_path: Path) -> None:
    use_fixture_dir(tmp_path, {"exec": {"stdout": "", "stderr": "", "exit": 7}})
    assert make_cli(tmp_path).exec_interactive(CONTAINER, ["false"]) == 7
    result = make_cli(tmp_path).exec(CONTAINER, ["false"], capture=False)
    assert (result.exit_code, result.stdout, result.stderr) == (7, "", "")


@pytest.mark.fr("FR-061")
@pytest.mark.fr("NFR-SEC-005")
def test_pass_names_reach_podman_as_env_name_and_no_value_reaches_the_argv(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """podman copies a passed name's value from its own environment, so the argv has the name
    only (ADR-004 amendment, ADR-010, docs/capabilities.md, Session environment row)."""
    proxy_url = "http://user:hunter2@proxy.example:3128"
    monkeypatch.setenv("http_proxy", proxy_url)
    use_fixture_dir(tmp_path, {"exec": {"stdout": "", "stderr": "", "exit": 0}})
    status = make_cli(tmp_path).exec_interactive(
        CONTAINER, ["env"], env={"LANG": "C.UTF-8"}, pass_env=["http_proxy"]
    )
    assert status == 0
    argv = logged(shim)[0]["argv"]
    flags = argv[argv.index("exec") + 1 : argv.index("--")]
    assert flags == ["--env", "LANG=C.UTF-8", "--env", "http_proxy"]
    assert not any(token.startswith("http_proxy=") for token in argv)
    assert all(proxy_url not in token for token in argv)


@pytest.mark.fr("NFR-SEC-005")
def test_a_pass_name_is_a_plain_name_and_is_not_also_given_a_value(
    shim: Path, tmp_path: Path
) -> None:
    cli = make_cli(tmp_path)
    with pytest.raises(ValueError):
        cli.exec_interactive(CONTAINER, ["true"], pass_env=["NAME=value"])
    with pytest.raises(ValueError):
        cli.exec_interactive(CONTAINER, ["true"], env={"http_proxy": "x"}, pass_env=["http_proxy"])
    assert logged(shim) == []


@pytest.mark.fr("FR-042")
@pytest.mark.fr("NFR-SEC-001")
def test_container_root_exec_adds_no_privilege_to_the_argv(shim: Path, tmp_path: Path) -> None:
    """`--root` is `--user 0:0` inside the container. `_exec_args` has no privileged option at all,
    so the argv carries only --tty, --interactive, --user, --env and --workdir (NFR-SEC-001)."""
    use_fixture_dir(tmp_path, {"exec": {"stdout": "", "stderr": "", "exit": 0}})
    make_cli(tmp_path).exec_interactive(CONTAINER, ["pacman", "-S", "htop"], user="0:0")
    argv = logged(shim)[0]["argv"]
    assert argv[argv.index("exec") + 1 : argv.index("--")] == ["--user", "0:0"]
    assert "--privileged" not in argv


def _interrupt_handlers() -> tuple[object, object]:
    return (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGQUIT))


class _FakeClient:
    """A stand-in for the podman client that exec_interactive starts with Popen.

    `wait` returns `returncode`, or raises `raises`. `kill` is recorded, so a test can see that an
    interrupted wait kills the client before the interruption propagates.
    """

    pid = 4321

    def __init__(self, returncode: int = 0, raises: BaseException | None = None) -> None:
        self.returncode = returncode
        self.raises = raises
        self.killed = False

    def wait(self) -> int:
        if self.raises is not None:
            raise self.raises
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@pytest.mark.fr("IR-011")
def test_exec_interactive_without_a_tty_catches_interrupts_while_podman_runs(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl-C reaches the whole foreground group, ginn included. Without a TTY, ginn holds SIGINT
    and SIGQUIT in a do-nothing handler while podman runs, so podman's status comes back. The
    handler is not SIG_IGN, because an ignored signal would survive execve into podman."""
    seen: dict[str, object] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> _FakeClient:
        seen["handlers"] = _interrupt_handlers()
        return _FakeClient(130)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    before = _interrupt_handlers()
    status = make_cli(tmp_path).exec_interactive(CONTAINER, ["sleep", "9"])
    assert status == 130
    sigint, sigquit = seen["handlers"]
    assert callable(sigint) and sigint is not signal.SIG_IGN
    assert callable(sigquit) and sigquit is not signal.SIG_IGN
    assert _interrupt_handlers() == before


@pytest.mark.fr("IR-011")
def test_a_child_started_under_the_guard_does_not_inherit_an_ignored_sigint_or_sigquit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real child: podman is a wrapper on PATH that writes its own SigIgn line to a file. The
    child is started by exec_interactive while the guard holds the signals. SIGINT and SIGQUIT must
    not be in its ignored mask, because an ignored signal survives execve (IR-011)."""
    status_file = tmp_path / "sig-status.txt"
    wrapper_dir = tmp_path / "sig-bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "podman"
    wrapper.write_text(
        f"#!/bin/sh\ngrep '^SigIgn:' /proc/self/status > {shlex.quote(str(status_file))}\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    assert make_cli(tmp_path).exec_interactive(CONTAINER, ["true"]) == 0

    name, _, value = status_file.read_text(encoding="utf-8").partition(":")
    assert name == "SigIgn"
    mask = int(value.strip(), 16)
    assert not mask & (1 << (signal.SIGINT - 1))
    assert not mask & (1 << (signal.SIGQUIT - 1))


@pytest.mark.fr("IR-011")
def test_exec_interactive_with_a_tty_leaves_the_signals_to_the_terminal(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> _FakeClient:
        seen["handlers"] = _interrupt_handlers()
        return _FakeClient(0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    before = _interrupt_handlers()
    assert make_cli(tmp_path).exec_interactive(CONTAINER, ["sh"], tty=True) == 0
    assert seen["handlers"] == before


@pytest.mark.fr("IR-011")
def test_the_interrupt_handlers_come_back_when_the_child_raises(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted wait kills and reaps the client, as subprocess.run does, then re-raises."""
    client = _FakeClient(raises=KeyboardInterrupt())
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: client)
    before = _interrupt_handlers()
    with pytest.raises(KeyboardInterrupt):
        make_cli(tmp_path).exec_interactive(CONTAINER, ["sh"])
    assert _interrupt_handlers() == before
    assert client.killed is True


@pytest.mark.fr("IR-011")
def test_off_the_main_thread_the_interrupt_handlers_are_not_touched(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signal handlers can be set only from the main thread, so another thread runs its child with
    the handlers it already has, and no ValueError is raised."""
    seen: dict[str, object] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> _FakeClient:
        seen["handlers"] = _interrupt_handlers()
        return _FakeClient(130)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    before = _interrupt_handlers()
    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            outcome["status"] = make_cli(tmp_path).exec_interactive(CONTAINER, ["sh"])
        except BaseException as exc:  # reported to the test thread below
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert outcome == {"status": 130}
    assert seen["handlers"] == before


@pytest.mark.fr("IR-020")
def test_top_and_logs_are_parsed_from_the_captured_output(shim: Path, tmp_path: Path) -> None:
    cli = make_cli(tmp_path)
    assert cli.top(CONTAINER) == [(1, 1040, "sleep")]
    assert cli.logs(CONTAINER, 2) == "one\ntwo\nthree\n"
    argv = logged(shim)[1]["argv"]
    assert argv[argv.index("--tail") + 1] == "2"


@pytest.mark.fr("IR-011")
@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ('Error: container "x" is not running\n', "GINN-E-INTERNAL"),
        ("Error: dial tcp: i/o timeout\n", "GINN-E-NETWORK"),
    ],
)
def test_exec_exit_125_is_podman_failure_not_the_command_status(
    shim: Path,
    tmp_path: Path,
    stderr: str,
    code: str,
) -> None:
    use_fixture_dir(tmp_path, {"exec": {"stdout": "", "stderr": stderr, "exit": 125}})
    err = expect_error(lambda: make_cli(tmp_path).exec(CONTAINER, ["true"]))
    assert err.code == code
    assert err.details["op"] == "exec"
    assert err.details["exit"] == 125


@pytest.mark.fr("IR-011")
@pytest.mark.parametrize("status", [126, 127])
def test_exec_passes_the_command_status_126_and_127_through(
    shim: Path, tmp_path: Path, status: int
) -> None:
    stderr = "OCI runtime exec failed: not executable\n"
    use_fixture_dir(tmp_path, {"exec": {"stdout": "", "stderr": stderr, "exit": status}})
    result = make_cli(tmp_path).exec(CONTAINER, ["/bin/x"])
    assert (result.exit_code, result.stdout, result.stderr) == (status, "", stderr)


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("dst", [Path("-rf"), Path("out"), Path("--help")])
def test_cp_from_refuses_a_destination_that_is_not_absolute(
    shim: Path, tmp_path: Path, dst: Path
) -> None:
    cli = make_cli(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        cli.cp_from(CONTAINER, "/etc/hosts", dst)
    assert cli.history == []
    assert logged(shim) == []


@pytest.mark.fr("NFR-SEC-005")
def test_cp_from_with_an_absolute_destination_passes_container_and_paths_as_argv(
    shim: Path, tmp_path: Path
) -> None:
    use_fixture_dir(tmp_path, {"cp": {"stdout": "", "stderr": "", "exit": 0}})
    dst = tmp_path / "hosts-copy"
    make_cli(tmp_path).cp_from(CONTAINER, "/etc/hosts", dst)
    argv = logged(shim)[0]["argv"]
    assert argv[-3:] == ["cp", f"{CONTAINER}:/etc/hosts", str(dst)]


@pytest.mark.fr("NFR-040")
def test_missing_binary_maps_to_internal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    cli = PodmanCli.from_paths(configured_paths(tmp_path), binary="ginn-no-such-podman")
    err = expect_error(cli.info)
    assert err.code == "GINN-E-INTERNAL"
    assert "cannot run ginn-no-such-podman" in str(err.details["detail"])


@pytest.mark.fr("NFR-061")
def test_cli_satisfies_the_adapter_protocol(tmp_path: Path) -> None:
    adapter: Podman = make_cli(tmp_path)
    assert adapter.info is not None


@pytest.mark.fr("NFR-061")
def test_retry_hook_type_matches_the_protocol() -> None:
    def hook(attempt: int, err: GinnError) -> bool:
        return attempt < 0

    typed: RetryHook = hook
    assert typed(1, GinnError("GINN-E-NETWORK", "m", "r", 69)) is False


@pytest.mark.fr("NFR-061")
def test_shim_never_runs_a_real_podman() -> None:
    source = (SHIM_DIR / "podman").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.exec", "os.spawn", "os.system", "pty"):
        assert forbidden not in source


@pytest.mark.fr("FR-120")
def test_the_podman_client_classifies_network_text_with_the_one_list_of_ops_retry() -> None:
    """The podman client and the package managers share NETWORK_PATTERNS (T8.1, FR-120)."""
    assert podman_cli.NETWORK_PATTERNS is retry.NETWORK_PATTERNS


@pytest.mark.fr("FR-019")
def test_rmi_of_an_image_in_use_is_internal_with_the_reason_named(
    shim: Path, tmp_path: Path
) -> None:
    """No catalogue code names an image in use, so the code stays INTERNAL, with the reason."""
    stderr = "Error: image used by 3f2a: image is in use by a container\n"
    use_fixture_dir(tmp_path, {"rmi": {"stdout": "", "stderr": stderr, "exit": 2}})
    err = expect_error(lambda: make_cli(tmp_path).rmi(DIGEST_REF))
    assert err.code == "GINN-E-INTERNAL"
    assert err.details["reason"] == "the image is in use by a container, so rmi refused it"
    assert err.details["op"] == "rmi"


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("dst", ["etc/machine-id", "-rf", "/etc/machine-id\n"])
def test_cp_into_refuses_a_destination_that_is_not_a_plain_absolute_container_path(
    shim: Path, tmp_path: Path, dst: str
) -> None:
    src = tmp_path / "machine-id"
    src.write_text("0" * 32 + "\n", encoding="ascii")
    cli = make_cli(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        cli.cp_into(CONTAINER, src, dst)
    assert cli.history == []
    assert logged(shim) == []


@pytest.mark.fr("NFR-SEC-005")
def test_cp_into_passes_the_host_file_and_the_container_path_as_argv(
    shim: Path, tmp_path: Path
) -> None:
    use_fixture_dir(tmp_path, {"cp": {"stdout": "", "stderr": "", "exit": 0}})
    src = tmp_path / "machine-id"
    src.write_text("0" * 32 + "\n", encoding="ascii")
    make_cli(tmp_path).cp_into(CONTAINER, src, "/etc/machine-id")
    argv = logged(shim)[0]["argv"]
    assert argv[-3:] == ["cp", str(src), f"{CONTAINER}:/etc/machine-id"]


@pytest.mark.fr("FR-080")
def test_cp_from_a_path_the_container_lacks_carries_the_no_such_path_reason(
    shim: Path, tmp_path: Path
) -> None:
    """F3: podman's not-found wording becomes the reason that the ops layer reads. The code stays
    GINN-E-INTERNAL, so only the create's init check turns it into GINN-E-NO-INIT."""
    stderr = '"/sbin/init" could not be found on container x: no such file or directory\n'
    use_fixture_dir(tmp_path, {"cp": {"stdout": "", "stderr": stderr, "exit": 125}})
    with pytest.raises(GinnError) as caught:
        make_cli(tmp_path).cp_from(CONTAINER, "/sbin/init", tmp_path / "init")
    assert caught.value.code == "GINN-E-INTERNAL"
    assert caught.value.details["reason"] == PATH_NOT_FOUND_REASON
