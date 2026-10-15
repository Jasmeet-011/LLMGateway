from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from llmgateway.schema import (
    ChatRequest,
    ChatResponse,
    Message,
    StreamDelta,
    StreamEnd,
    StreamEvent,
    Usage,
)


def _request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "mock-1",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return body


def test_minimal_request_defaults() -> None:
    req = ChatRequest.model_validate(_request())
    assert req.messages == [Message(role="user", content="hi")]
    assert req.system is None
    assert req.temperature is None
    assert req.top_p is None
    assert req.stop is None
    assert req.stream is False


def test_full_request() -> None:
    req = ChatRequest.model_validate(
        _request(
            system="be brief",
            temperature=0.0,
            top_p=1.0,
            stop=["END"],
            stream=True,
            messages=[
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"},
            ],
        )
    )
    assert len(req.messages) == 3
    assert req.stream is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"messages": []},
        {"messages": [{"role": "user", "content": ""}]},
        {"messages": [{"role": "system", "content": "x"}]},
        {"model": ""},
        {"max_tokens": 0},
        {"temperature": -0.1},
        {"temperature": 1.5},
        {"top_p": 1.01},
        {"stop": [""]},
        {"system": ""},
        {"unknown_field": 1},
    ],
)
def test_invalid_requests_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(_request(**overrides))


def test_max_tokens_required() -> None:
    body = _request()
    del body["max_tokens"]
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(body)


def test_request_is_immutable() -> None:
    req = ChatRequest.model_validate(_request())
    with pytest.raises(ValidationError):
        req.max_tokens = 1  # type: ignore[misc]


def test_response_json_round_trip() -> None:
    resp = ChatResponse(
        id="r1",
        provider="mock",
        model="mock-1",
        content="hello",
        finish_reason="stop",
        usage=Usage(input_tokens=3, output_tokens=2),
    )
    assert ChatResponse.model_validate_json(resp.model_dump_json()) == resp
    assert resp.usage.total_tokens == 5


def test_negative_usage_rejected() -> None:
    with pytest.raises(ValidationError):
        Usage(input_tokens=-1, output_tokens=0)


def test_stream_events_discriminated() -> None:
    adapter: TypeAdapter[StreamDelta | StreamEnd] = TypeAdapter(StreamEvent)
    delta = adapter.validate_json('{"type": "delta", "text": "he"}')
    end = adapter.validate_json(
        '{"type": "end", "finish_reason": "max_tokens",'
        ' "usage": {"input_tokens": 1, "output_tokens": 16}}'
    )
    assert delta == StreamDelta(text="he")
    assert isinstance(end, StreamEnd)
    assert end.finish_reason == "max_tokens"
    with pytest.raises(ValidationError):
        adapter.validate_json('{"type": "unknown"}')
