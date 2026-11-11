"""Integration tests: the Compose stack's api, over a real socket (D-078, D-081).

These need `docker compose up` to be running, and are marked `integration`, so a plain
`uv run pytest` and `check.ps1` do not select them. CI runs `uv run pytest -m integration`
against the stack, and so can a laptop.

The gateway's address is read from `LLMGATEWAY_BASE_URL`. If it is unset or empty, every
test here FAILS with a message that says so. It never skips: a CI job that lost the
variable must not go green with nothing tested. The scheme must be `http` or `https` and
the parsed host exactly `127.0.0.1` or `localhost`, or the test fails before any request,
so these can only reach a local stack,
which runs the mock (D-078). `tests/test_integration_guard.py` runs this file with the
variable unset and with other hosts, with no Docker, and checks that all of it fails and
that nothing is sent.

The guard runs inside each test, not in a fixture, because pytest reports a fixture's
failure as an error, and D-081 says the test fails.

The tests use real httpx over the socket, and use no fake of the gateway.

M14 (D-094): the guard and the helpers moved to `tests/compose_support.py`, which the guard
test copies next to this file, so it can still run this file alone. Every chat test now
sends a key made by the real CLI in the api container (`docker compose exec`), because
every route but `/healthz` needs one (D-089). `/healthz` is public and is still asked with
no key. Each test calls `base_url()` first, before any docker command or request.
"""

import json
from collections.abc import Sequence
from typing import Any

import pytest
from compose_support import BODY, base_url, gateway, shared_key

from llmgateway.errors import ErrorBody
from llmgateway.schema import ChatResponse, StreamDelta, StreamEnd

pytestmark = pytest.mark.integration


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
    base_url()
    with gateway() as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_to_the_mock_alias_is_a_chat_response_from_the_mock() -> None:
    base_url()
    with gateway(shared_key()) as client:
        response = client.post("/v1/chat", json=BODY)
    assert response.status_code == 200, response.text
    chat = ChatResponse.model_validate(response.json())
    assert chat.provider == "mock"
    assert chat.provider_instance == "mock"
    assert chat.model == "mock-1"
    assert response.headers["x-request-id"] == chat.id


def test_streamed_chat_is_an_event_stream_of_deltas_and_one_end() -> None:
    base_url()
    with gateway(shared_key()) as client:
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
    base_url()
    with gateway(shared_key()) as client:
        response = client.post("/v1/chat", json={**BODY, "model": "not-a-configured-alias"})
    assert response.status_code == 400, response.text
    error = ErrorBody.model_validate(response.json())
    assert error.error.code == "unknown_model"
    assert error.error.request_id == response.headers["x-request-id"]
