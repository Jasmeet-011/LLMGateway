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

The child gets only what it needs. `LLMGATEWAY_BASE_URL` is set or removed explicitly, so
a CI job that has it set does not change what is checked here.
"""

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
INTEGRATION_FILE = TESTS_DIR / "test_compose_stack.py"
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


def _write(kind, target):
    with open(os.environ["ATTEMPT_LOG"], "a", encoding="utf-8") as log:
        log.write(f"{kind} {target!r}\\n")


def _getaddrinfo(host, *args, **kwargs):
    _write("getaddrinfo", host)
    raise socket.gaierror("refused by the guard test")


def _connect(self, address):
    _write("connect", address)
    raise ConnectionRefusedError("refused by the guard test")


socket.getaddrinfo = _getaddrinfo
socket.socket.connect = _connect
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


def _expected_count() -> int:
    source = INTEGRATION_FILE.read_text(encoding="utf-8")
    return len(re.findall(r"^(?:async )?def test_", source, flags=re.MULTILINE))


def _run_integration_file(tmp_path: Path, base_url: str | None) -> Run:
    (tmp_path / "pytest.ini").write_text(INI, encoding="utf-8")
    (tmp_path / "conftest.py").write_text(RECORDER, encoding="utf-8")
    (tmp_path / "test_compose_stack.py").write_text(
        INTEGRATION_FILE.read_text(encoding="utf-8"), encoding="utf-8"
    )
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
            str(tmp_path / "test_compose_stack.py"),
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


def _assert_every_test_failed(run: Run) -> None:
    expected = _expected_count()
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
