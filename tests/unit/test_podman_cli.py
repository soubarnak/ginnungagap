# SPDX-License-Identifier: Apache-2.0
"""PodmanCli: verbose echo, digest-only pulls, retries, error mapping, and the replay shim.

Most tests run the real PodmanCli against tests/fakes/podman_bin/podman on PATH. The shim replays
captured fixtures and never runs a real podman, so these tests need no container runtime.
"""

import json
import os
import shlex
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


def make_cli(tmp_path: Path, verbose: Verbose | None = None) -> PodmanCli:
    return PodmanCli(tmp_path / "root", tmp_path / "runroot", verbose=verbose)


def logged(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def global_flags(tmp_path: Path) -> list[str]:
    return ["--root", str(tmp_path / "root"), "--runroot", str(tmp_path / "runroot")]


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
        tmpdir=tmp_path / "tmp",
        cdi_spec_dir=tmp_path / "cdi",
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
        "--cdi-spec-dir",
        str(tmp_path / "cdi"),
        "info",
        "--format",
        "json",
    ]


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


@pytest.mark.fr("NFR-040")
def test_missing_binary_maps_to_internal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    cli = PodmanCli(tmp_path / "root", tmp_path / "runroot", binary="ginn-no-such-podman")
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
