# SPDX-License-Identifier: Apache-2.0
"""PodmanCli: verbose echo, digest-only pulls, retries, error mapping, and the replay shim.

Most tests run the real PodmanCli against tests/fakes/podman_bin/podman on PATH. The shim replays
captured fixtures and never runs a real podman, so these tests need no container runtime.
"""

import json
import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ginn.errors import GinnError
from ginn.podman.adapter import CreateSpec, Podman, RetryHook
from ginn.podman.cli import PodmanCli

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
Verbose = Callable[[str], None]


@pytest.fixture
def shim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put the replay shim first on PATH, replay the captured fixtures, and return its log."""
    log = tmp_path / "shim.jsonl"
    monkeypatch.setenv("PATH", os.pathsep.join([str(SHIM_DIR), os.environ.get("PATH", "")]))
    monkeypatch.setenv("GINN_FAKE_PODMAN_FIXTURES", str(FIXTURES))
    monkeypatch.setenv("GINN_FAKE_PODMAN_LOG", str(log))
    monkeypatch.delenv("GINN_FAKE_PODMAN_CASE", raising=False)
    return log


def use_cases(monkeypatch: pytest.MonkeyPatch, cases: str) -> None:
    """Map shim route keys to fixture cases, as `key=case,key=case`."""
    monkeypatch.setenv("GINN_FAKE_PODMAN_CASE", cases)


def use_fixture_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cases: dict[str, Any]) -> None:
    """Replay the given envelopes from a private fixture directory, with no case mapping."""
    directory = tmp_path / "cases"
    directory.mkdir()
    for name, body in cases.items():
        (directory / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setenv("GINN_FAKE_PODMAN_FIXTURES", str(directory))
    monkeypatch.delenv("GINN_FAKE_PODMAN_CASE", raising=False)


def pinned(tmp_path: Path) -> dict[str, Any]:
    """The required keyword arguments: ginn's tmpdir, runtime root and pinned config files."""
    return {
        "tmpdir": tmp_path / "tmp",
        "containers_conf": tmp_path / "containers.conf",
        "storage_conf": tmp_path / "storage.conf",
        "runtime_root": tmp_path / "runtime",
    }


def make_cli(tmp_path: Path, verbose: Verbose | None = None) -> PodmanCli:
    return PodmanCli(tmp_path / "root", tmp_path / "runroot", verbose=verbose, **pinned(tmp_path))


def logged(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def global_flags(tmp_path: Path) -> list[str]:
    return [
        "--root",
        str(tmp_path / "root"),
        "--runroot",
        str(tmp_path / "runroot"),
        "--tmpdir",
        str(tmp_path / "tmp"),
        "--runtime",
        "crun",
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
    use_cases(monkeypatch, "exec=exec-ok")
    lines: list[str] = []
    cli = make_cli(tmp_path, lines.append)
    cli.exec(CONTAINER, ["sh", "-c", "echo 'a b' && printf '%s\\n' \"$HOME\""])
    assert len(lines) == 1
    assert shlex.split(lines[0]) == cli.history[0]
    assert lines[0] == shlex.join(cli.history[0])


@pytest.mark.fr("IR-020")
def test_global_flags_come_before_the_subcommand(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = PodmanCli(
        tmp_path / "root",
        tmp_path / "runroot",
        cdi_spec_dir=tmp_path / "cdi",
        **pinned(tmp_path),
    )
    cli.info()
    argv = cli.history[0]
    assert argv[1:] == [
        "--root",
        str(tmp_path / "root"),
        "--runroot",
        str(tmp_path / "runroot"),
        "--tmpdir",
        str(tmp_path / "tmp"),
        "--runtime",
        "crun",
        "--cdi-spec-dir",
        str(tmp_path / "cdi"),
        "info",
        "--format",
        "json",
    ]


@pytest.mark.fr("DR-010")
def test_tmpdir_is_required_so_podman_never_uses_the_users_libpod_tmp(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        PodmanCli(  # type: ignore[call-arg]
            tmp_path / "root",
            tmp_path / "runroot",
            containers_conf=tmp_path / "containers.conf",
            storage_conf=tmp_path / "storage.conf",
        )


@pytest.mark.fr("DR-010")
def test_every_call_carries_root_runroot_tmpdir_and_runtime(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "exec=exec-ok,image-exists=image-exists-absent")
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
        assert argv[1:9] == global_flags(tmp_path), argv
    # The shim logs the argv without the program name.
    assert [entry["argv"][0:8] for entry in logged(shim)] == [global_flags(tmp_path)] * 7


@pytest.mark.fr("IR-020")
def test_runtime_defaults_to_crun_and_is_configurable(shim: Path, tmp_path: Path) -> None:
    cli = make_cli(tmp_path)
    cli.info()
    assert cli.history[0][cli.history[0].index("--runtime") + 1] == "crun"

    runc = PodmanCli(
        tmp_path / "root", tmp_path / "runroot", oci_runtime="runc", **pinned(tmp_path)
    )
    runc.info()
    assert runc.history[0][runc.history[0].index("--runtime") + 1] == "runc"

    with pytest.raises(ValueError, match="oci runtime"):
        PodmanCli(tmp_path / "root", tmp_path / "runroot", oci_runtime="-crun", **pinned(tmp_path))


@pytest.mark.fr("DR-010")
def test_user_containers_and_storage_configuration_are_not_read(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, str] | None] = []
    real_run = subprocess.run

    def spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs.get("env"))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setenv("CONTAINERS_CONF_OVERRIDE", str(tmp_path / "user-override.conf"))
    # A login session exports its own runtime directory. Podman must not see it.
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    cli = make_cli(tmp_path)
    cli.info()
    use_cases(monkeypatch, "exec=exec-ok")
    cli.exec_interactive(CONTAINER, ["true"])

    assert len(seen) == 2
    for env in seen:
        assert env is not None
        assert env["CONTAINERS_CONF"] == str(tmp_path / "containers.conf")
        assert env["CONTAINERS_STORAGE_CONF"] == str(tmp_path / "storage.conf")
        assert "CONTAINERS_CONF_OVERRIDE" not in env
        assert env["PATH"] == os.environ["PATH"]


@pytest.mark.fr("DR-033")
@pytest.mark.fr("FR-123")
def test_session_and_supervisor_podman_both_see_the_one_runtime_root(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, str] | None] = []
    real_run = subprocess.run

    def spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs.get("env"))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    runtime_root = tmp_path / "run" / "ginn" / "1000"
    # The supervisor exports XDG_RUNTIME_DIR=/run/ginn/<uid>; a session exports /run/user/<uid>.
    # Both must reach podman as the same runtime root.
    for parent_xdg in (str(runtime_root), "/run/user/1000"):
        monkeypatch.setenv("XDG_RUNTIME_DIR", parent_xdg)
        PodmanCli(
            tmp_path / "root",
            tmp_path / "runroot",
            **{**pinned(tmp_path), "runtime_root": runtime_root},
        ).info()
    assert [env["XDG_RUNTIME_DIR"] for env in seen if env is not None] == [
        str(runtime_root),
        str(runtime_root),
    ]
    assert len(logged(shim)) == 2
    assert all(entry["argv"][0:8] == global_flags(tmp_path) for entry in logged(shim))


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
def test_pull_by_digest_then_inspects_the_image(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "pull=pull-ok,image-inspect=image-inspect-alpine")
    cli = make_cli(tmp_path)
    image = cli.pull(DIGEST_REF)
    assert image.id == ALPINE_ID
    assert image.digest == DIGEST
    assert cli.history == [
        ["podman", *global_flags(tmp_path), "pull", "--quiet", DIGEST_REF],
        ["podman", *global_flags(tmp_path), "image", "inspect", "--format", "json", DIGEST_REF],
    ]


@pytest.mark.fr("FR-011")
def test_digest_mismatch_fixture_maps_to_image_unverified(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "pull=pull-digest-mismatch")
    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF))
    assert err.code == "GINN-E-IMAGE-UNVERIFIED"
    assert err.exit_code == 65
    assert err.details["distro"] == "docker.io/library/alpine"
    assert err.details["exit"] == 125
    assert "Digest did not match" in str(err.details["stderr"])


@pytest.mark.fr("FR-120")
def test_network_pull_fixture_maps_to_network(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "pull=pull-network-error")
    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF))
    assert err.code == "GINN-E-NETWORK"
    assert err.exit_code == 69
    assert err.details["exit"] == 125
    assert str(err.details["stderr"]).startswith("Error: initializing source docker://")
    assert "dial tcp" in str(err.details["stderr"])


@pytest.mark.fr("NFR-040")
def test_unknown_container_fixture_maps_to_internal(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "container-inspect=container-inspect-missing")
    err = expect_error(lambda: make_cli(tmp_path).inspect("nonexist"))
    assert err.code == "GINN-E-INTERNAL"
    assert err.exit_code == 70
    assert err.details["exit"] == 125
    assert err.details["stderr"] == 'Error: no such container "nonexist"'


@pytest.mark.fr("NFR-040")
def test_image_exists_is_false_for_exit_one_and_true_for_exit_zero(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "image-exists=image-exists-absent")
    assert make_cli(tmp_path).image_exists(DIGEST_REF) is False
    use_fixture_dir(monkeypatch, tmp_path, {"image-exists": {"exit": 0}})
    assert make_cli(tmp_path).image_exists(DIGEST_REF) is True


@pytest.mark.fr("FR-120")
@pytest.mark.parametrize("line", NETWORK_STDERR)
def test_registry_and_network_messages_map_to_network(
    line: str, shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fixture_dir(
        monkeypatch,
        tmp_path,
        {"pull": {"stdout": "", "stderr": f"Error: pinging registry: {line}\n", "exit": 125}},
    )
    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF))
    assert err.code == "GINN-E-NETWORK"


@pytest.mark.fr("NFR-040")
def test_network_text_with_another_exit_status_is_internal(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fixture_dir(
        monkeypatch,
        tmp_path,
        {"pull": {"stdout": "", "stderr": "Error: dial tcp: i/o timeout\n", "exit": 1}},
    )
    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF))
    assert err.code == "GINN-E-INTERNAL"


@pytest.mark.fr("NFR-040")
def test_status_code_inside_a_digest_is_not_an_http_status(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hexdigest = "sha256:" + "a503" + "0" * 60
    use_fixture_dir(
        monkeypatch,
        tmp_path,
        {"exec": {"stdout": "", "stderr": f"Error: no blob {hexdigest}\n", "exit": 125}},
    )
    err = expect_error(lambda: make_cli(tmp_path).start(CONTAINER))
    assert err.code == "GINN-E-INTERNAL"


@pytest.mark.fr("FR-120")
def test_retry_hook_runs_three_times_then_network_error(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "pull=pull-network-error")
    seen: list[tuple[int, str]] = []

    def hook(attempt: int, err: GinnError) -> bool:
        seen.append((attempt, err.code))
        return True

    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF, retry_hook=hook))
    assert err.code == "GINN-E-NETWORK"
    assert seen == [(1, "GINN-E-NETWORK"), (2, "GINN-E-NETWORK"), (3, "GINN-E-NETWORK")]
    assert sum(1 for entry in logged(shim) if "pull" in entry["argv"]) == 4


@pytest.mark.fr("FR-120")
def test_retry_hook_returning_false_stops_after_one_attempt(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "pull=pull-network-error")
    calls: list[int] = []

    def hook(attempt: int, err: GinnError) -> bool:
        calls.append(attempt)
        return False

    err = expect_error(lambda: make_cli(tmp_path).pull(DIGEST_REF, retry_hook=hook))
    assert err.code == "GINN-E-NETWORK"
    assert calls == [1]
    assert sum(1 for entry in logged(shim) if "pull" in entry["argv"]) == 1


@pytest.mark.fr("FR-011")
def test_digest_mismatch_is_never_retried(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "pull=pull-digest-mismatch")
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
def test_inspect_parses_the_container_fixture(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "container-inspect=container-inspect-alpine")
    container = make_cli(tmp_path).inspect(CONTAINER)
    assert container.name == "t211-alpine"
    assert container.image == "docker.io/library/alpine:3"


@pytest.mark.fr("FR-011")
def test_create_returns_id_and_refuses_privileged_spec(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "create=create-ok")
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
def test_exec_builds_an_argv_list_with_option_terminator(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_cases(monkeypatch, "exec=exec-ok")
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


@pytest.mark.fr("IR-011")
def test_exec_interactive_returns_the_inner_status(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fixture_dir(monkeypatch, tmp_path, {"exec": {"stdout": "", "stderr": "", "exit": 7}})
    assert make_cli(tmp_path).exec_interactive(CONTAINER, ["false"]) == 7
    result = make_cli(tmp_path).exec(CONTAINER, ["false"], capture=False)
    assert (result.exit_code, result.stdout, result.stderr) == (7, "", "")


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
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    code: str,
) -> None:
    use_fixture_dir(monkeypatch, tmp_path, {"exec": {"stdout": "", "stderr": stderr, "exit": 125}})
    err = expect_error(lambda: make_cli(tmp_path).exec(CONTAINER, ["true"]))
    assert err.code == code
    assert err.details["op"] == "exec"
    assert err.details["exit"] == 125


@pytest.mark.fr("IR-011")
@pytest.mark.parametrize("status", [126, 127])
def test_exec_passes_the_command_status_126_and_127_through(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    stderr = "OCI runtime exec failed: not executable\n"
    use_fixture_dir(
        monkeypatch, tmp_path, {"exec": {"stdout": "", "stderr": stderr, "exit": status}}
    )
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
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fixture_dir(monkeypatch, tmp_path, {"cp": {"stdout": "", "stderr": "", "exit": 0}})
    dst = tmp_path / "hosts-copy"
    make_cli(tmp_path).cp_from(CONTAINER, "/etc/hosts", dst)
    argv = logged(shim)[0]["argv"]
    assert argv[-3:] == ["cp", f"{CONTAINER}:/etc/hosts", str(dst)]


@pytest.mark.fr("NFR-040")
def test_missing_binary_maps_to_internal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    cli = PodmanCli(
        tmp_path / "root", tmp_path / "runroot", binary="ginn-no-such-podman", **pinned(tmp_path)
    )
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
