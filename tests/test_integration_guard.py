"""The guard in `tests/test_compose_stack.py` (D-081), checked with no Docker.

The integration tests must never go green with nothing tested, and must never reach a
host that is not local. This file is not marked `integration`, so the default run proves
both. It runs the integration file in a child pytest, in a scratch directory, selected
with `-m integration` as CI does, and reads the child's junit XML:

- `LLMGATEWAY_BASE_URL` unset, or empty: every test FAILS. Not skipped, not passed, not
  an error in setup, and the message names the variable.
- A host other than `127.0.0.1` or `localhost`: every test fails, and no request was
  attempted. The child's `conftest.py` replaces `socket.getaddrinfo` and
  `socket.socket.connect` with functions that write down each attempt and refuse it, so
  nothing leaves the machine whatever the guard does, and the parent reads the list.
- A control: with `127.0.0.1` and with `localhost` the guard lets the tests go on, so
  they do attempt a connection (and fail, since there is no stack). Without it, an empty
  attempt list for a wrong host could mean only that the recorder is broken.

M14 (D-094): the integration files also run `docker compose` to make keys and read the
table. The child's recorder therefore refuses and writes down every process start, through
`subprocess.Popen`, so "nothing happened" covers docker commands as well as connections:
with a bad or missing URL no docker command is started, and with a local one the first
docker command is attempted (and refused), which is the control. The child gets
`tests/compose_support.py` next to the integration file, which is where the guard now
lives. Every integration file is run this way: `test_compose_stack.py` by the tests from
M13, and `test_compose_stack_auth.py` by the tests at the end of this file.

The child gets only what it needs. `LLMGATEWAY_BASE_URL` is set or removed explicitly, so
a CI job that has it set does not change what is checked here.
"""

import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
INTEGRATION_FILE = TESTS_DIR / "test_compose_stack.py"
AUTH_INTEGRATION_FILE = TESTS_DIR / "test_compose_stack_auth.py"
SUPPORT_FILE = TESTS_DIR / "compose_support.py"
VARIABLE = "LLMGATEWAY_BASE_URL"

INI = """\
[pytest]
markers =
    integration: needs the Compose stack at LLMGATEWAY_BASE_URL
addopts = --strict-markers
"""

# Every attempt to resolve a name or connect is written down and refused.
RECORDER = """
import os
import socket
import subprocess


def _write(kind, target):
    with open(os.environ["ATTEMPT_LOG"], "a", encoding="utf-8") as log:
        log.write(f"{kind} {target!r}\\n")


def _getaddrinfo(host, *args, **kwargs):
    # The port is written too (M14), so a test can see which address was asked for.
    _write("getaddrinfo", (host, args[0] if args else None))
    raise socket.gaierror("refused by the guard test")


def _connect(self, address):
    _write("connect", address)
    raise ConnectionRefusedError("refused by the guard test")


def _popen(self, args, *more, **kwargs):
    _write("popen", args)
    raise OSError("refused by the guard test")


socket.getaddrinfo = _getaddrinfo
socket.socket.connect = _connect
subprocess.Popen.__init__ = _popen
"""


@dataclass(frozen=True)
class Case:
    name: str
    state: str
    text: str


@dataclass(frozen=True)
class Run:
    returncode: int
    cases: list[Case]
    attempts: list[str]
    output: str


def _expected_count(file: Path = INTEGRATION_FILE) -> int:
    source = file.read_text(encoding="utf-8")
    return len(re.findall(r"^(?:async )?def test_", source, flags=re.MULTILINE))


def _run_integration_file(
    tmp_path: Path,
    base_url: str | None,
    file: Path = INTEGRATION_FILE,
    *,
    stubbed_repo: bool = False,
) -> Run:
    """Runs `file` in a child pytest. With `stubbed_repo` (M16), the files go in a `tests`
    directory of a fake repository root holding a stub `src/llmgateway/ratelimit.lua`, because
    the rate limit tests read the script from the repository before their first docker command,
    and a test that failed on a missing file would never make the attempt the control counts."""
    base = tmp_path / "tests" if stubbed_repo else tmp_path
    base.mkdir(exist_ok=True)
    if stubbed_repo:
        lua = tmp_path / "src" / "llmgateway" / "ratelimit.lua"
        lua.parent.mkdir(parents=True, exist_ok=True)
        lua.write_text("return {1, 0}\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text(INI, encoding="utf-8")
    (tmp_path / "conftest.py").write_text(RECORDER, encoding="utf-8")
    (base / SUPPORT_FILE.name).write_text(
        SUPPORT_FILE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (base / file.name).write_text(file.read_text(encoding="utf-8"), encoding="utf-8")
    log = tmp_path / "attempts.log"
    report = tmp_path / "report.xml"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (VARIABLE, "PYTEST_ADDOPTS", "PYTEST_PLUGINS")
    }
    env["ATTEMPT_LOG"] = str(log)
    if base_url is not None:
        env[VARIABLE] = base_url
    # The command is fixed text and a copy of this repo's own test file.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-m",
            "integration",
            f"--junitxml={report}",
            str(base / file.name),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    assert report.exists(), output
    cases: list[Case] = []
    for element in ET.parse(report).getroot().iter("testcase"):  # noqa: S314
        state, text = "passed", ""
        for child in element:
            if child.tag in ("failure", "error", "skipped"):
                state = "failed" if child.tag == "failure" else child.tag
                text = f"{child.get('message', '')}\n{child.text or ''}"
        cases.append(Case(element.get("name", ""), state, text))
    attempts = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return Run(result.returncode, cases, attempts, output)


def _assert_every_test_failed(run: Run, file: Path = INTEGRATION_FILE) -> None:
    expected = _expected_count(file)
    assert expected >= 4, "the integration file lost tests"
    assert len(run.cases) == expected, run.output
    assert [(c.name, c.state) for c in run.cases] == [(c.name, "failed") for c in run.cases], (
        run.output
    )
    assert run.returncode == 1, run.output


@pytest.mark.parametrize("base_url", [None, ""], ids=["unset", "empty"])
def test_integration_tests_fail_and_never_skip_without_a_base_url(
    tmp_path: Path, base_url: str | None
) -> None:
    run = _run_integration_file(tmp_path, base_url)
    _assert_every_test_failed(run)
    for case in run.cases:
        assert VARIABLE in case.text, (case.name, case.text)
    assert run.attempts == []


@pytest.mark.parametrize(
    "base_url",
    [
        "http://gateway.invalid:8000",
        "http://127.0.0.2:8000",
        "http://localhost.invalid:8000",
        "http://127.0.0.1@gateway.invalid:8000",
        "http://gateway.invalid:8000/127.0.0.1",
    ],
    ids=["unknown_host", "other_loopback_address", "localhost_prefix", "userinfo", "in_the_path"],
)
def test_integration_tests_fail_before_any_request_for_a_host_that_is_not_local(
    tmp_path: Path, base_url: str
) -> None:
    run = _run_integration_file(tmp_path, base_url)
    _assert_every_test_failed(run)
    for case in run.cases:
        assert VARIABLE in case.text, (case.name, case.text)
    assert run.attempts == []


@pytest.mark.parametrize(
    "base_url", ["http://127.0.0.1:1", "http://localhost:1"], ids=["127_0_0_1", "localhost"]
)
def test_integration_tests_do_attempt_a_connection_to_a_local_host(
    tmp_path: Path, base_url: str
) -> None:
    # The control for the two tests above: the recorder sees real attempts, and the guard
    # lets a local address through. With no stack behind it, each test still fails.
    run = _run_integration_file(tmp_path, base_url)
    _assert_every_test_failed(run)
    assert len(run.attempts) >= len(run.cases), run.output


@pytest.mark.parametrize(
    "base_url",
    [
        "http://notlocalhost:8000",
        "http://xlocalhost:8000",
        "http://1.127.0.0.1:8000",
        "http://localhost.:8000",
        "http://[::1]:8000",
        "http://localhost:80@gateway.invalid:8000",
        "http://127.0.0.1:80@gateway.invalid:8000",
    ],
    ids=[
        "suffix_localhost",
        "prefix_letter_localhost",
        "suffix_loopback",
        "trailing_dot",
        "ipv6",
        "userinfo_with_port_like_local",
        "userinfo_with_port_like_loopback",
    ],
)
def test_integration_tests_fail_before_any_request_for_a_host_that_only_ends_like_a_local_one(
    tmp_path: Path, base_url: str
) -> None:
    # D-081: the host must be 127.0.0.1 or localhost. A host that merely ends with one of
    # them, or is another loopback form, is not either, and must not get a lookup.
    run = _run_integration_file(tmp_path, base_url)
    _assert_every_test_failed(run)
    for case in run.cases:
        assert VARIABLE in case.text, (case.name, case.text)
    assert run.attempts == []


@pytest.mark.parametrize(
    "base_url",
    ["ftp://127.0.0.1:21", "ws://localhost:8000", "file://localhost/tmp"],
    ids=["ftp", "ws", "file"],
)
def test_integration_tests_fail_with_the_guard_message_for_a_scheme_that_is_not_http(
    tmp_path: Path, base_url: str
) -> None:
    # The host is local, so only the scheme is wrong. Every test fails before any request
    # and the failure is the guard's, naming the variable, not an httpx error from later.
    # D-081 names the scheme rule (http or https), the host rule and the failure.
    run = _run_integration_file(tmp_path, base_url)
    _assert_every_test_failed(run)
    for case in run.cases:
        assert VARIABLE in case.text, (case.name, case.text)
        assert "UnsupportedProtocol" not in case.text, (case.name, case.text)
    assert run.attempts == []


# M14: the key tests, which run docker as well as send requests (D-094)

NOT_LOCAL = [
    pytest.param(None, id="unset"),
    pytest.param("", id="empty"),
    pytest.param("http://gateway.invalid:8000", id="unknown_host"),
    pytest.param("http://127.0.0.1:80@gateway.invalid:8000", id="userinfo_with_port_like_local"),
    pytest.param("http://localhost.:8000", id="trailing_dot"),
    pytest.param("ftp://127.0.0.1:21", id="ftp_scheme"),
]


@pytest.mark.parametrize("file", [INTEGRATION_FILE, AUTH_INTEGRATION_FILE], ids=["stack", "auth"])
def test_the_recorder_refuses_and_writes_down_a_docker_command(tmp_path: Path, file: Path) -> None:
    # The control for the docker half of every test below: with a local address the guard
    # passes, and the first docker command a test starts is attempted, so it is recorded.
    # Without this an empty list for a bad URL could mean only that the recorder is blind.
    run = _run_integration_file(tmp_path, "http://127.0.0.1:1", file)
    _assert_every_test_failed(run, file)
    docker = [attempt for attempt in run.attempts if attempt.startswith("popen")]
    assert docker, run.output
    assert all("docker" in attempt and "compose" in attempt for attempt in docker), docker
    assert any(re.search(r"llmgateway\.keys|psql", attempt) for attempt in docker), docker


@pytest.mark.parametrize("base_url", NOT_LOCAL)
def test_the_key_tests_fail_before_any_docker_command_or_request_without_a_local_url(
    tmp_path: Path, base_url: str | None
) -> None:
    run = _run_integration_file(tmp_path, base_url, AUTH_INTEGRATION_FILE)
    _assert_every_test_failed(run, AUTH_INTEGRATION_FILE)
    for case in run.cases:
        assert VARIABLE in case.text, (case.name, case.text)
    assert run.attempts == []


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:1", "http://localhost:1"])
def test_the_key_tests_do_attempt_docker_or_a_connection_for_a_local_url(
    tmp_path: Path, base_url: str
) -> None:
    run = _run_integration_file(tmp_path, base_url, AUTH_INTEGRATION_FILE)
    _assert_every_test_failed(run, AUTH_INTEGRATION_FILE)
    assert len(run.attempts) >= len(run.cases), run.output


@pytest.mark.parametrize("base_url", [None, "http://gateway.invalid:8000"], ids=["unset", "remote"])
def test_the_stack_tests_start_no_docker_command_without_a_local_url(
    tmp_path: Path, base_url: str | None
) -> None:
    # test_compose_stack.py now makes keys with docker; the guard still comes first.
    run = _run_integration_file(tmp_path, base_url)
    _assert_every_test_failed(run)
    assert run.attempts == []


def test_the_outage_test_is_collected_last_in_the_whole_run(tmp_path: Path) -> None:
    # Both integration files together, in the order pytest would run them: the test that
    # stops Postgres is the final one, whatever the file names sort to.
    for file in (INTEGRATION_FILE, AUTH_INTEGRATION_FILE, SUPPORT_FILE):
        (tmp_path / file.name).write_text(file.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text(INI, encoding="utf-8")
    shutil.copy(TESTS_DIR / "conftest.py", tmp_path / "conftest.py")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", VARIABLE)}
    # The command is fixed text and copies of this repo's own test files.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-m",
            "integration",
            "--collect-only",
            "-q",
            str(tmp_path / AUTH_INTEGRATION_FILE.name),
            str(tmp_path / INTEGRATION_FILE.name),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    ids = [line for line in result.stdout.splitlines() if "::" in line]
    assert len(ids) == _expected_count() + _expected_count(AUTH_INTEGRATION_FILE), result.stdout
    assert ids[-1].endswith(
        "::test_zz_outage_a_valid_key_is_503_with_postgres_stopped_and_200_when_it_returns"
    )
    assert sum("zz_outage" in line for line in ids) == 1


@pytest.mark.parametrize(
    ("base_url", "host", "port"),
    [
        pytest.param("http://127.0.0.1:1", "127.0.0.1", 1, id="loopback_address"),
        pytest.param("http://localhost:2", "localhost", 2, id="localhost"),
    ],
)
def test_the_stack_tests_connect_to_the_address_they_were_given(
    tmp_path: Path, base_url: str, host: str, port: int
) -> None:
    # Added in Mode 2: a guard that returned some other local address passed every
    # test above, which only counted attempts. The first test sends a request.
    run = _run_integration_file(tmp_path, base_url)
    _assert_every_test_failed(run)
    lookups = [attempt for attempt in run.attempts if attempt.startswith("getaddrinfo")]
    assert lookups, run.attempts
    assert set(lookups) == {f"getaddrinfo ('{host}', {port})"}, lookups


# M15: the usage tests, which also run docker, and whose failure-row test runs a script in the
# api container (D-100)

USAGE_INTEGRATION_FILE = TESTS_DIR / "test_compose_stack_usage.py"


@pytest.mark.parametrize("base_url", NOT_LOCAL)
def test_the_usage_tests_fail_before_any_docker_command_or_request_without_a_local_url(
    tmp_path: Path, base_url: str | None
) -> None:
    run = _run_integration_file(tmp_path, base_url, USAGE_INTEGRATION_FILE)
    _assert_every_test_failed(run, USAGE_INTEGRATION_FILE)
    for case in run.cases:
        assert VARIABLE in case.text, (case.name, case.text)
    assert run.attempts == []


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:1", "http://localhost:1"])
def test_the_usage_tests_do_attempt_docker_or_a_connection_for_a_local_url(
    tmp_path: Path, base_url: str
) -> None:
    run = _run_integration_file(tmp_path, base_url, USAGE_INTEGRATION_FILE)
    _assert_every_test_failed(run, USAGE_INTEGRATION_FILE)
    assert len(run.attempts) >= len(run.cases), run.output
    docker = [attempt for attempt in run.attempts if attempt.startswith("popen")]
    assert docker, run.output
    assert all("docker" in attempt and "compose" in attempt for attempt in docker), docker


def test_the_outage_test_is_still_collected_last_with_the_usage_tests_in_the_run(
    tmp_path: Path,
) -> None:
    # Written before M16: the Postgres outage test is the last of three files' tests, and the
    # only outage test among them.
    files = (INTEGRATION_FILE, AUTH_INTEGRATION_FILE, USAGE_INTEGRATION_FILE, SUPPORT_FILE)
    for file in files:
        (tmp_path / file.name).write_text(file.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text(INI, encoding="utf-8")
    shutil.copy(TESTS_DIR / "conftest.py", tmp_path / "conftest.py")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", VARIABLE)}
    # The command is fixed text and copies of this repo's own test files.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-m",
            "integration",
            "--collect-only",
            "-q",
            str(tmp_path / USAGE_INTEGRATION_FILE.name),
            str(tmp_path / AUTH_INTEGRATION_FILE.name),
            str(tmp_path / INTEGRATION_FILE.name),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    ids = [line for line in result.stdout.splitlines() if "::" in line]
    assert len(ids) == sum(
        _expected_count(file)
        for file in (INTEGRATION_FILE, AUTH_INTEGRATION_FILE, USAGE_INTEGRATION_FILE)
    ), result.stdout
    assert ids[-1].endswith(
        "::test_zz_outage_a_valid_key_is_503_with_postgres_stopped_and_200_when_it_returns"
    )
    assert sum("zz_outage" in line for line in ids) == 1
    assert any("test_compose_stack_usage.py" in line for line in ids)


# M16: the rate limit tests, which run docker, send requests to a second gateway on 127.0.0.1:8001
# and stop Redis (D-109)

RATELIMIT_INTEGRATION_FILE = TESTS_DIR / "test_compose_stack_ratelimit.py"
POSTGRES_OUTAGE = "test_zz_outage_a_valid_key_is_503_with_postgres_stopped_and_200_when_it_returns"
REDIS_OUTAGE = "test_zz_outage_a_valid_key_is_200_with_redis_stopped_and_the_limiter_recovers"


@pytest.mark.parametrize("base_url", NOT_LOCAL)
def test_the_ratelimit_tests_fail_before_any_docker_command_or_request_without_a_local_url(
    tmp_path: Path, base_url: str | None
) -> None:
    run = _run_integration_file(tmp_path, base_url, RATELIMIT_INTEGRATION_FILE, stubbed_repo=True)
    _assert_every_test_failed(run, RATELIMIT_INTEGRATION_FILE)
    for case in run.cases:
        assert VARIABLE in case.text, (case.name, case.text)
    assert run.attempts == []


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:1", "http://localhost:1"])
def test_the_ratelimit_tests_do_attempt_docker_or_a_connection_for_a_local_url(
    tmp_path: Path, base_url: str
) -> None:
    run = _run_integration_file(tmp_path, base_url, RATELIMIT_INTEGRATION_FILE, stubbed_repo=True)
    _assert_every_test_failed(run, RATELIMIT_INTEGRATION_FILE)
    assert len(run.attempts) >= len(run.cases), run.output
    docker_commands = [attempt for attempt in run.attempts if attempt.startswith("popen")]
    assert docker_commands, run.output
    assert all("docker" in attempt for attempt in docker_commands), docker_commands
    assert any("redis-cli" in attempt for attempt in docker_commands), docker_commands


@pytest.mark.parametrize(
    "address",
    [
        "http://127.0.0.1:8001",
        "http://localhost:8001",
        "https://127.0.0.1:8001",
    ],
)
def test_the_shared_guard_lets_a_local_address_through_and_returns_it_without_a_slash(
    address: str,
) -> None:
    import compose_support

    assert compose_support.local_url(address + "/", what="the address") == address


@pytest.mark.parametrize(
    "address",
    [
        "http://gateway.invalid:8001",
        "http://127.0.0.2:8001",
        "http://127.0.0.1@gateway.invalid:8001",
        "http://localhost.:8001",
        "http://[::1]:8001",
        "ftp://127.0.0.1:8001",
        "",
    ],
)
def test_the_shared_guard_fails_for_an_address_that_is_not_local_naming_what_it_checked(
    address: str,
) -> None:
    import compose_support

    with pytest.raises(pytest.fail.Exception) as info:
        compose_support.local_url(address, what="the second gateway address")
    assert "the second gateway address" in str(info.value)


def test_the_second_gateway_address_is_port_8001_on_the_loopback_address_and_is_guarded_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import compose_support

    monkeypatch.delenv(VARIABLE, raising=False)
    with pytest.raises(pytest.fail.Exception) as info:
        compose_support.second_gateway_url()
    assert VARIABLE in str(info.value)
    monkeypatch.setenv(VARIABLE, "http://127.0.0.1:8000")
    assert compose_support.second_gateway_url() == "http://127.0.0.1:8001"
    monkeypatch.setenv(VARIABLE, "http://gateway.invalid:8000")
    with pytest.raises(pytest.fail.Exception):
        compose_support.second_gateway_url()


def test_both_outage_tests_are_collected_last_with_the_ratelimit_tests_in_the_run(
    tmp_path: Path,
) -> None:
    files = (
        INTEGRATION_FILE,
        AUTH_INTEGRATION_FILE,
        RATELIMIT_INTEGRATION_FILE,
        USAGE_INTEGRATION_FILE,
        SUPPORT_FILE,
    )
    for file in files:
        (tmp_path / file.name).write_text(file.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text(INI, encoding="utf-8")
    shutil.copy(TESTS_DIR / "conftest.py", tmp_path / "conftest.py")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", VARIABLE)}
    # The command is fixed text and copies of this repo's own test files.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-m",
            "integration",
            "--collect-only",
            "-q",
            str(tmp_path / USAGE_INTEGRATION_FILE.name),
            str(tmp_path / RATELIMIT_INTEGRATION_FILE.name),
            str(tmp_path / AUTH_INTEGRATION_FILE.name),
            str(tmp_path / INTEGRATION_FILE.name),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    ids = [line for line in result.stdout.splitlines() if "::" in line]
    assert len(ids) == sum(
        _expected_count(file)
        for file in (
            INTEGRATION_FILE,
            AUTH_INTEGRATION_FILE,
            RATELIMIT_INTEGRATION_FILE,
            USAGE_INTEGRATION_FILE,
        )
    ), result.stdout
    assert sum("zz_outage" in line for line in ids) == 2
    # Each stops a service and restores it; both run after every other test, in file order.
    assert sorted(line.rsplit("::", 1)[1] for line in ids[-2:]) == sorted(
        [POSTGRES_OUTAGE, REDIS_OUTAGE]
    )
    assert any("test_compose_stack_ratelimit.py" in line for line in ids)
