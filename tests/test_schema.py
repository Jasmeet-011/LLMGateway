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


def _response(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "r1",
        "provider": "mock",
        "model": "mock-1",
        "content": "hello",
        "finish_reason": "stop",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    body.update(overrides)
    return body


def test_minimal_request_defaults() -> None:
    req = ChatRequest.model_validate(_request())
    assert req.messages == (Message(role="user", content="hi"),)
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
    assert req.stop == ("END",)
    assert req.stream is True


def test_json_input_arrays_validate() -> None:
    req = ChatRequest.model_validate_json(
        '{"model": "m", "messages": [{"role": "user", "content": "hi"}],'
        ' "max_tokens": 4, "stop": ["a", "b"]}'
    )
    assert req.messages == (Message(role="user", content="hi"),)
    assert req.stop == ("a", "b")


def test_missing_max_tokens_rejected() -> None:
    body = _request()
    del body["max_tokens"]
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest.model_validate(body)
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("max_tokens",)
    assert errors[0]["type"] == "missing"


@pytest.mark.parametrize(
    ("overrides", "loc"),
    [
        ({"messages": []}, ("messages",)),
        ({"messages": [{"role": "user", "content": ""}]}, ("messages", 0, "content")),
        ({"messages": [{"role": "system", "content": "x"}]}, ("messages", 0, "role")),
        (
            {"messages": [{"role": "user", "content": "x", "extra": 1}]},
            ("messages", 0, "extra"),
        ),
        ({"messages": [{"role": "assistant", "content": "x"}]}, ("messages",)),
        (
            {
                "messages": [
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ]
            },
            ("messages",),
        ),
        ({"model": ""}, ("model",)),
        ({"max_tokens": 0}, ("max_tokens",)),
        ({"temperature": -0.1}, ("temperature",)),
        ({"temperature": 1.5}, ("temperature",)),
        ({"top_p": -0.1}, ("top_p",)),
        ({"top_p": 1.01}, ("top_p",)),
        ({"stop": [""]}, ("stop", 0)),
        ({"system": ""}, ("system",)),
        ({"unknown_field": 1}, ("unknown_field",)),
        ({"stream": "yes"}, ("stream",)),
        ({"stream": 1}, ("stream",)),
    ],
)
def test_invalid_requests_rejected(overrides: dict[str, Any], loc: tuple[str | int, ...]) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest.model_validate(_request(**overrides))
    errors = exc_info.value.errors()
    assert len(errors) == 1, errors
    assert errors[0]["loc"] == loc


def test_boundary_values_accepted() -> None:
    req = ChatRequest.model_validate(_request(temperature=1.0, top_p=0.0))
    assert req.temperature == 1.0
    assert req.top_p == 0.0


def test_last_message_must_be_user() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest.model_validate(_request(messages=[{"role": "assistant", "content": "x"}]))
    assert "last message must have role 'user'" in exc_info.value.errors()[0]["msg"]


def test_first_message_role_not_restricted() -> None:
    # Unverified for both providers, so the schema deliberately has no first-message rule.
    req = ChatRequest.model_validate(
        _request(
            messages=[
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": "b"},
            ]
        )
    )
    assert req.messages[0].role == "assistant"


def test_request_is_immutable() -> None:
    req = ChatRequest.model_validate(_request(stop=["END"]))
    with pytest.raises(ValidationError):
        req.max_tokens = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        req.messages = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        req.messages[0] = Message(role="user", content="x")  # type: ignore[index]
    with pytest.raises(AttributeError):
        req.messages.append(Message(role="user", content="x"))  # type: ignore[attr-defined]
    assert req.stop is not None
    with pytest.raises(TypeError):
        req.stop[0] = "x"  # type: ignore[index]
    with pytest.raises(AttributeError):
        req.stop.append("x")  # type: ignore[attr-defined]


def test_request_is_hashable() -> None:
    a = ChatRequest.model_validate(_request(stop=["END"]))
    b = ChatRequest.model_validate(_request(stop=["END"]))
    c = ChatRequest.model_validate(_request(stop=["OTHER"]))
    assert a == b
    assert hash(a) == hash(b)
    assert a != c
    assert len({a, b, c}) == 2


def test_input_list_mutation_does_not_affect_request() -> None:
    messages = [{"role": "user", "content": "hi"}]
    req = ChatRequest.model_validate(_request(messages=messages))
    messages.append({"role": "user", "content": "later"})
    assert len(req.messages) == 1


@pytest.mark.parametrize("empty", [[], ()])
def test_empty_stop_normalized_to_none(empty: list[str] | tuple[str, ...]) -> None:
    omitted = ChatRequest.model_validate(_request())
    explicit_none = ChatRequest.model_validate(_request(stop=None))
    normalized = ChatRequest.model_validate(_request(stop=empty))
    assert normalized.stop is None
    assert normalized == explicit_none == omitted
    assert hash(normalized) == hash(explicit_none) == hash(omitted)
    assert normalized.model_dump() == omitted.model_dump()


def test_empty_stop_from_json_normalized_to_none() -> None:
    req = ChatRequest.model_validate_json(
        '{"model": "mock-1", "messages": [{"role": "user", "content": "hi"}],'
        ' "max_tokens": 16, "stop": []}'
    )
    assert req.stop is None
    assert req == ChatRequest.model_validate(_request())


def test_non_empty_stop_untouched() -> None:
    assert ChatRequest.model_validate(_request(stop=["a", "b"])).stop == ("a", "b")


def test_stop_with_empty_string_still_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest.model_validate(_request(stop=["a", ""]))
    errors = exc_info.value.errors()
    assert len(errors) == 1, errors
    assert errors[0]["loc"] == ("stop", 1)


# Coercion policy: pydantic lax mode stays on, except for `stream`.
def test_lax_numeric_coercion_kept() -> None:
    req = ChatRequest.model_validate(_request(max_tokens="16", temperature="0.5"))
    assert req.max_tokens == 16
    assert req.temperature == 0.5
    assert ChatRequest.model_validate(_request(temperature=1)).temperature == 1.0


@pytest.mark.parametrize("value", ["yes", "true", "0", 1, 0])
def test_stream_not_coerced_to_bool(value: object) -> None:
    # Lax mode would turn "yes" or 1 into True and silently enable streaming.
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(_request(stream=value))


def test_stream_json_bool_accepted_json_string_rejected() -> None:
    prefix = '{"model":"m","messages":[{"role":"user","content":"hi"}],"max_tokens":1,'
    assert ChatRequest.model_validate_json(prefix + '"stream":true}').stream is True
    with pytest.raises(ValidationError):
        ChatRequest.model_validate_json(prefix + '"stream":"yes"}')


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
    assert resp.provider_response_id is None


def test_response_provider_id_round_trip() -> None:
    resp = ChatResponse.model_validate(_response(provider_response_id="msg_123"))
    assert resp.provider_response_id == "msg_123"
    assert ChatResponse.model_validate_json(resp.model_dump_json()) == resp


@pytest.mark.parametrize(
    ("overrides", "loc"),
    [
        ({"id": ""}, ("id",)),
        ({"provider": "openai"}, ("provider",)),
        ({"finish_reason": "length"}, ("finish_reason",)),
    ],
)
def test_invalid_responses_rejected(overrides: dict[str, Any], loc: tuple[str | int, ...]) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChatResponse.model_validate(_response(**overrides))
    errors = exc_info.value.errors()
    assert len(errors) == 1, errors
    assert errors[0]["loc"] == loc


def test_empty_content_and_delta_text_allowed() -> None:
    # A model can legitimately return nothing (for example an immediate stop sequence).
    assert ChatResponse.model_validate(_response(content="")).content == ""
    assert StreamDelta(text="").text == ""


def test_negative_usage_rejected() -> None:
    with pytest.raises(ValidationError):
        Usage(input_tokens=-1, output_tokens=0)
    with pytest.raises(ValidationError) as exc_info:
        Usage(input_tokens=0, output_tokens=-1)
    assert exc_info.value.errors()[0]["loc"] == ("output_tokens",)


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


@pytest.mark.parametrize(
    "payload",
    [
        '{"type": "end", "finish_reason": "stop"}',
        '{"type": "delta"}',
        '{"text": "hi"}',
        "{}",
    ],
)
def test_malformed_stream_events_rejected(payload: str) -> None:
    adapter: TypeAdapter[StreamDelta | StreamEnd] = TypeAdapter(StreamEvent)
    with pytest.raises(ValidationError):
        adapter.validate_json(payload)
