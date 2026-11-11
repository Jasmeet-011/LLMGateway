"""Integration tests: the Compose stack's api, over a real socket (D-078, D-081).

These need `docker compose up` to be running, and are marked `integration`, so a plain
`uv run pytest` and `check.ps1` do not select them. CI runs `uv run pytest -m integration`
against the stack, and so can a laptop.

The gateway's address is read from `LLMGATEWAY_BASE_URL`. If it is unset or empty, every
test here FAILS with a message that says so. It never skips: a CI job that lost the
variable must not go green with nothing tested. The host must be `127.0.0.1` or
`localhost`, or the test fails before any request, so these can only reach a local stack,
which runs the mock (D-078). `tests/test_integration_guard.py` runs this file with the
variable unset and with other hosts, with no Docker, and checks that all of it fails and
that nothing is sent.

The guard runs inside each test, not in a fixture, because pytest reports a fixture's
failure as an error, and D-081 says the test fails.

The tests use real httpx over the socket, and use no fake of the gateway. Nothing here
imports another test module, so the guard test can run this file alone.
"""

import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from llmgateway.errors import ErrorBody
from llmgateway.schema import ChatResponse, StreamDelta, StreamEnd

pytestmark = pytest.mark.integration

VARIABLE = "LLMGATEWAY_BASE_URL"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# A stack that is up answers at once; a stalled build host is the only reason to wait.
TIMEOUT = 30.0

BODY: dict[str, Any] = {
    "model": "mock",
    "messages": [{"role": "user", "content": "say hello to the compose stack"}],
    "max_tokens": 32,
}


def base_url() -> str:
    """The stack's address, or a failure that says why not. Never a skip (D-081)."""
    value = os.environ.get(VARIABLE, "")
    if not value.strip():
        pytest.fail(
            f"{VARIABLE} is unset or empty: these tests need the Compose stack and its "
            f"address, for example {VARIABLE}=http://127.0.0.1:8000. They fail and never "
            "skip, so a run that lost the variable cannot pass with nothing tested."
        )
    parts = urlsplit(value.strip())
    host = parts.hostname
    if parts.scheme not in ("http", "https") or host not in LOCAL_HOSTS:
        pytest.fail(
            f"{VARIABLE} must be an http(s) URL whose host is one of {', '.join(LOCAL_HOSTS)}: "
            "these tests only ever talk to a local stack, and no request was sent."
        )
    return value.strip().rstrip("/")


@contextmanager
def gateway() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=base_url(), timeout=TIMEOUT) as client:
        yield client


def parse_frames(raw: bytes) -> list[dict[str, Any]]:
    """Reads the body strictly as D-073 writes it: `event: <type>\\ndata: <json>\\n\\n`."""
    assert raw.endswith(b"\n\n"), raw
    events: list[dict[str, Any]] = []
    for frame in raw[:-2].split(b"\n\n"):
        name_line, data_line = frame.split(b"\n")
        assert name_line.startswith(b"event: "), frame
        assert data_line.startswith(b"data: "), frame
        event: dict[str, Any] = json.loads(data_line[len(b"data: ") :])
        assert event["type"] == name_line[len(b"event: ") :].decode(), frame
        events.append(event)
    return events


def joined_text(events: Sequence[dict[str, Any]]) -> str:
    return "".join(event["text"] for event in events if event["type"] == "delta")


def test_healthz_is_200_with_status_ok() -> None:
    with gateway() as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_to_the_mock_alias_is_a_chat_response_from_the_mock() -> None:
    with gateway() as client:
        response = client.post("/v1/chat", json=BODY)
    assert response.status_code == 200, response.text
    chat = ChatResponse.model_validate(response.json())
    assert chat.provider == "mock"
    assert chat.provider_instance == "mock"
    assert chat.model == "mock-1"
    assert response.headers["x-request-id"] == chat.id


def test_streamed_chat_is_an_event_stream_of_deltas_and_one_end() -> None:
    with gateway() as client:
        plain = client.post("/v1/chat", json=BODY)
        with client.stream("POST", "/v1/chat", json={**BODY, "stream": True}) as response:
            response.read()
    assert plain.status_code == 200, plain.text
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse_frames(response.content)
    types = [event["type"] for event in events]
    assert types[-1] == "end", types
    assert types.count("end") == 1, types
    assert types.count("delta") >= 1 and set(types[:-1]) == {"delta"}, types
    for event in events[:-1]:
        assert StreamDelta.model_validate(event).text == event["text"]
    end = StreamEnd.model_validate(events[-1])
    assert end.id == response.headers["x-request-id"]
    assert (end.provider, end.provider_instance, end.model) == ("mock", "mock", "mock-1")
    # The deltas are the content: the same request answered whole says the same text.
    assert joined_text(events) == ChatResponse.model_validate(plain.json()).content


def test_unknown_alias_is_400_with_the_unknown_model_error() -> None:
    with gateway() as client:
        response = client.post("/v1/chat", json={**BODY, "model": "not-a-configured-alias"})
    assert response.status_code == 400, response.text
    error = ErrorBody.model_validate(response.json())
    assert error.error.code == "unknown_model"
    assert error.error.request_id == response.headers["x-request-id"]
