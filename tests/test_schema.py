import importlib
import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from llmgateway.providers.base import ProviderResult
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
        "provider_instance": "mock",
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
    # Through **extras, as written before the field existed (D-063); it changes nothing.
    extras: dict[str, Any] = {"provider_instance": "mock"}
    resp = ChatResponse(
        id="r1",
        provider="mock",
        model="mock-1",
        content="hello",
        finish_reason="stop",
        usage=Usage(input_tokens=3, output_tokens=2),
        **extras,
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


def _stream_end(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "end",
        "id": "gw_123",
        "provider": "claude",
        "provider_instance": "claude-primary",
        "model": "claude-haiku-5-5",
        "finish_reason": "stop",
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "provider_response_id": "msg_1",
    }
    body.update(overrides)
    return body


def _stream_error(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "error",
        "code": "provider_unavailable",
        "message": "The provider is unavailable.",
        "request_id": "gw_123",
    }
    body.update(overrides)
    return body


def test_stream_events_discriminated() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(StreamEvent)
    delta = adapter.validate_json('{"type": "delta", "text": "he"}')
    end = adapter.validate_json(json.dumps(_stream_end(finish_reason="max_tokens")))
    assert delta == StreamDelta(text="he")
    assert isinstance(end, StreamEnd)
    assert end.finish_reason == "max_tokens"
    with pytest.raises(ValidationError):
        adapter.validate_json('{"type": "unknown"}')


@pytest.mark.parametrize(
    "payload",
    [
        '{"type": "end", "finish_reason": "stop"}',
        # D-073: the two fields the old StreamEnd had are no longer an end event.
        '{"type": "end", "finish_reason": "stop",'
        ' "usage": {"input_tokens": 1, "output_tokens": 2}}',
        '{"type": "delta"}',
        '{"text": "hi"}',
        "{}",
    ],
)
def test_malformed_stream_events_rejected(payload: str) -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(StreamEvent)
    with pytest.raises(ValidationError):
        adapter.validate_json(payload)


# D-073: StreamEnd carries what ChatResponse carries, and StreamError joins the events.
# StreamError and ProviderStreamEnd are reached at call time, so this file collects and
# each test fails on its own until they exist.


def _stream_error_class() -> Any:
    return importlib.import_module("llmgateway.schema").StreamError


def _provider_stream_end() -> Any:
    return importlib.import_module("llmgateway.providers.base").ProviderStreamEnd


def _event_adapter() -> TypeAdapter[Any]:
    return TypeAdapter(StreamEvent)


def test_stream_end_has_exactly_the_fields_of_chat_response_but_content() -> None:
    fields = set(StreamEnd.model_fields) - {"type"}
    assert fields == set(ChatResponse.model_fields) - {"content"}


@pytest.mark.parametrize("name", sorted(set(ChatResponse.model_fields) - {"content"}))
def test_stream_end_fields_are_typed_as_in_chat_response(name: str) -> None:
    assert StreamEnd.model_fields[name].annotation == ChatResponse.model_fields[name].annotation
    assert StreamEnd.model_fields[name].metadata == ChatResponse.model_fields[name].metadata
    assert (
        StreamEnd.model_fields[name].is_required() == ChatResponse.model_fields[name].is_required()
    )


def test_stream_end_round_trips() -> None:
    end = StreamEnd.model_validate(_stream_end())
    assert json.loads(end.model_dump_json()) == _stream_end()


def test_stream_end_dumps_its_fields_in_the_documented_order() -> None:
    assert list(json.loads(StreamEnd.model_validate(_stream_end()).model_dump_json())) == [
        "type",
        "id",
        "provider",
        "provider_instance",
        "model",
        "finish_reason",
        "usage",
        "provider_response_id",
    ]


def test_a_null_provider_response_id_stays_in_the_json() -> None:
    body = _stream_end()
    del body["provider_response_id"]
    assert '"provider_response_id":null' in StreamEnd.model_validate(body).model_dump_json()


@pytest.mark.parametrize(
    "name", ["id", "provider", "provider_instance", "model", "finish_reason", "usage"]
)
def test_stream_end_requires_each_field(name: str) -> None:
    StreamEnd.model_validate(_stream_end())
    body = _stream_end()
    del body[name]
    with pytest.raises(ValidationError) as exc_info:
        StreamEnd.model_validate(body)
    assert exc_info.value.errors()[0]["loc"] == (name,)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"id": ""}, id="empty_id"),
        pytest.param({"provider": "openai"}, id="unknown_provider"),
        pytest.param({"provider_instance": ""}, id="empty_instance"),
        pytest.param({"provider_instance": "x" * 129}, id="long_instance"),
        pytest.param({"model": ""}, id="empty_model"),
        pytest.param({"model": "x" * 129}, id="long_model"),
        pytest.param({"finish_reason": "done"}, id="unknown_finish_reason"),
        pytest.param({"usage": {"input_tokens": -1, "output_tokens": 0}}, id="negative_usage"),
        pytest.param({"content": "text"}, id="content_is_not_a_field"),
        pytest.param({"unknown": 1}, id="unknown_field"),
    ],
)
def test_invalid_stream_ends_rejected(overrides: dict[str, Any]) -> None:
    # The control: only the override makes this body invalid.
    StreamEnd.model_validate(_stream_end())
    with pytest.raises(ValidationError):
        StreamEnd.model_validate(_stream_end(**overrides))


def test_stream_end_is_frozen() -> None:
    end: Any = StreamEnd.model_validate(_stream_end())
    with pytest.raises(ValidationError):
        end.id = "gw_other"


def test_stream_error_round_trips() -> None:
    error = _stream_error_class().model_validate(_stream_error())
    assert json.loads(error.model_dump_json()) == _stream_error()


def test_stream_error_has_the_fields_of_error_info_in_order() -> None:
    error = _stream_error_class().model_validate(_stream_error())
    assert list(json.loads(error.model_dump_json())) == ["type", "code", "message", "request_id"]


@pytest.mark.parametrize("name", ["code", "message", "request_id"])
def test_stream_error_requires_each_field(name: str) -> None:
    body = _stream_error()
    del body[name]
    with pytest.raises(ValidationError):
        _stream_error_class().model_validate(body)


@pytest.mark.parametrize("name", ["code", "message", "request_id"])
def test_stream_error_fields_must_be_non_empty(name: str) -> None:
    with pytest.raises(ValidationError):
        _stream_error_class().model_validate(_stream_error(**{name: ""}))


def test_stream_error_forbids_extra_fields_and_is_frozen() -> None:
    cls = _stream_error_class()
    with pytest.raises(ValidationError):
        cls.model_validate(_stream_error(details=[]))
    error = cls.model_validate(_stream_error())
    with pytest.raises(ValidationError):
        error.code = "other"


def test_stream_event_discriminates_all_three_types() -> None:
    adapter = _event_adapter()
    assert isinstance(adapter.validate_python({"type": "delta", "text": "x"}), StreamDelta)
    assert isinstance(adapter.validate_python(_stream_end()), StreamEnd)
    assert isinstance(adapter.validate_python(_stream_error()), _stream_error_class())


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"type": "error"}, id="an_error_with_no_fields"),
        pytest.param({"type": "error", "code": "x"}, id="an_error_with_only_a_code"),
        pytest.param({"code": "x", "message": "y", "request_id": "z"}, id="no_type"),
    ],
)
def test_malformed_stream_error_events_rejected(payload: dict[str, Any]) -> None:
    # Against a union that has the error type, so `{"type": "error"}` is not rejected
    # just for naming a type the union lacks.
    _stream_error_class()
    with pytest.raises(ValidationError):
        _event_adapter().validate_python(payload)


# ProviderStreamEnd (D-067): what an adapter yields last


def _end_arguments(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "finish_reason": "stop",
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "provider_response_id": "msg_1",
    }
    body.update(overrides)
    return body


def test_provider_stream_end_holds_the_non_content_fields_of_provider_result() -> None:
    result_fields = set(ProviderResult.model_fields) - {"content"}
    assert set(_provider_stream_end().model_fields) == result_fields


def test_provider_stream_end_response_id_is_optional() -> None:
    body = _end_arguments()
    del body["provider_response_id"]
    assert _provider_stream_end().model_validate(body).provider_response_id is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"provider_response_id": ""}, id="an_empty_id"),
        pytest.param({"finish_reason": "done"}, id="an_unknown_finish_reason"),
        pytest.param({"usage": None}, id="no_usage"),
        pytest.param({"content": "text"}, id="content"),
        pytest.param({"unknown": 1}, id="an_unknown_field"),
    ],
)
def test_invalid_provider_stream_ends_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _provider_stream_end().model_validate(_end_arguments(**overrides))


def test_provider_stream_end_is_frozen() -> None:
    end = _provider_stream_end().model_validate(_end_arguments())
    with pytest.raises(ValidationError):
        end.finish_reason = "other"
