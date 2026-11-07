"""Every new row of FLOW.md section 13's streaming tables, through the app (M12).

The adapter files pin each row's class, flag, status and reason against the adapter
alone. This file reads each row across, end to end, with the real adapter over
`httpx.MockTransport`: the same provider outcome before the first event is a JSON error
with the row's client status, and after it an `error` event with the row's client code.
`stream_failed` gives the class, status and reason as the router saw them, and
`attempt_failed` shows whether the router would have retried, which is the row's
Retryable column with D-052 applied.

Names M12 adds are reached at call time, so this file collects and each test fails on
its own.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from stream_support import (
    ANTHROPIC_KEY,
    CLAUDE_ENTRY,
    CLAUDE_STOP,
    GEMINI_ENTRY,
    GEMINI_KEY,
    PROVIDER_TEXT,
    Gateway,
    Upstream,
    attempt_line,
    claude_error,
    claude_frame,
    claude_message_delta,
    claude_start,
    claude_text,
    event_texts,
    event_types,
    failed_line,
    gemini_chunk,
    gemini_frame,
    gemini_text,
    lines_starting,
    parse_events,
    request_line,
    sse_response,
    stream_body,
)

from llmgateway.errors import ErrorBody
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter

MESSAGES = {
    "provider_unavailable": "The provider is unavailable.",
    "provider_rate_limited": "The provider is rate limiting the gateway.",
    "provider_bad_request": "The provider rejected the request.",
    "provider_auth_error": "The gateway could not authenticate with the provider.",
}


@dataclass(frozen=True)
class Row:
    """One row of a streaming table, as the app must show it."""

    frames: tuple[bytes, ...]
    error: str
    status: int | str
    reason: str
    retries: bool
    client_status: int
    code: str


def _claude_row(error_type: str, *rest: Any) -> Row:
    return Row((claude_error(error_type),), *rest)


CLAUDE_ROWS = [
    pytest.param(
        _claude_row(
            "invalid_request_error", "ProviderBadRequest", 400, "http_status", False, 400,
            "provider_bad_request",
        ),
        id="invalid_request_error",
    ),
    pytest.param(
        _claude_row(
            "authentication_error", "ProviderAuthError", 401, "auth", False, 502,
            "provider_auth_error",
        ),
        id="authentication_error",
    ),
    pytest.param(
        _claude_row(
            "billing_error", "ProviderAuthError", 402, "billing", False, 502,
            "provider_auth_error",
        ),
        id="billing_error",
    ),
    pytest.param(
        _claude_row(
            "permission_error", "ProviderAuthError", 403, "auth", False, 502,
            "provider_auth_error",
        ),
        id="permission_error",
    ),
    pytest.param(
        _claude_row(
            "not_found_error", "ProviderBadRequest", 404, "http_status", False, 400,
            "provider_bad_request",
        ),
        id="not_found_error",
    ),
    pytest.param(
        _claude_row(
            "request_too_large", "ProviderBadRequest", 413, "http_status", False, 400,
            "provider_bad_request",
        ),
        id="request_too_large",
    ),
    pytest.param(
        _claude_row(
            "rate_limit_error", "ProviderRateLimited", 429, "rate_limited", True, 503,
            "provider_rate_limited",
        ),
        id="rate_limit_error",
    ),
    pytest.param(
        _claude_row(
            "api_error", "ProviderUnavailable", 500, "http_status", True, 502,
            "provider_unavailable",
        ),
        id="api_error",
    ),
    pytest.param(
        _claude_row(
            "overloaded_error", "ProviderUnavailable", 529, "http_status", True, 502,
            "provider_unavailable",
        ),
        id="overloaded_error",
    ),
    pytest.param(
        _claude_row(
            "an_error_added_later", "ProviderUnavailable", "-", "stream_error", True, 502,
            "provider_unavailable",
        ),
        id="any_other_error_type",
    ),
    pytest.param(
        Row(
            (claude_frame({"type": "error", "error": "no object"}),),
            "ProviderUnavailable", "-", "stream_error", True, 502, "provider_unavailable",
        ),
        id="any_other_error_shape",
    ),
    pytest.param(
        Row(
            (b"data: [1]\n\n",),
            "ProviderUnavailable", 200, "malformed_body", False, 502, "provider_unavailable",
        ),
        id="a_frame_not_a_json_object",
    ),
    pytest.param(
        Row(
            (claude_text(5),),
            "ProviderUnavailable", 200, "malformed_body", False, 502, "provider_unavailable",
        ),
        id="a_field_with_the_wrong_type",
    ),
    pytest.param(
        Row(
            (CLAUDE_STOP,),
            "ProviderUnavailable", 200, "malformed_body", False, 502, "provider_unavailable",
        ),
        id="message_stop_with_no_stop_reason",
    ),
    pytest.param(
        Row(
            (b"data: \xff\n\n",),
            "ProviderUnavailable", 200, "malformed_body", False, 502, "provider_unavailable",
        ),
        id="a_line_not_utf8",
    ),
    pytest.param(
        Row(
            (),
            "ProviderUnavailable", 200, "stream_truncated", False, 502, "provider_unavailable",
        ),
        id="the_body_ends_before_message_stop",
    ),
]  # fmt: skip

GEMINI_ROWS = [
    pytest.param(
        Row(
            (gemini_frame({"promptFeedback": {"blockReason": "SAFETY"}}),),
            "ProviderBadRequest", 200, "prompt_blocked", False, 400, "provider_bad_request",
        ),
        id="a_block_reason",
    ),
    pytest.param(
        Row(
            (gemini_frame({"error": {"code": 503, "message": PROVIDER_TEXT}}),),
            "ProviderUnavailable", "-", "stream_error", True, 502, "provider_unavailable",
        ),
        id="a_top_level_error",
    ),
    pytest.param(
        Row(
            (b"data: [1]\r\n\r\n",),
            "ProviderUnavailable", 200, "malformed_body", False, 502, "provider_unavailable",
        ),
        id="a_chunk_not_a_json_object",
    ),
    pytest.param(
        Row(
            (gemini_frame({"candidates": "x"}),),
            "ProviderUnavailable", 200, "malformed_body", False, 502, "provider_unavailable",
        ),
        id="candidates_not_a_list",
    ),
    pytest.param(
        Row(
            (gemini_frame({"candidates": ["x"]}),),
            "ProviderUnavailable", 200, "malformed_body", False, 502, "provider_unavailable",
        ),
        id="a_first_candidate_failing_d041",
    ),
    pytest.param(
        Row(
            (gemini_frame(gemini_chunk(finish="STOP", usage={"promptTokenCount": "x"})),),
            "ProviderUnavailable", 200, "malformed_body", False, 502, "provider_unavailable",
        ),
        id="a_count_failing_d041",
    ),
    pytest.param(
        Row(
            (gemini_frame(gemini_chunk(finish="STOP")),),
            "ProviderUnavailable", 200, "malformed_body", False, 502, "provider_unavailable",
        ),
        id="a_finish_reason_and_no_usage",
    ),
    pytest.param(
        Row(
            (b"data: \xff\r\n\r\n",),
            "ProviderUnavailable", 200, "malformed_body", False, 502, "provider_unavailable",
        ),
        id="a_line_not_utf8",
    ),
    pytest.param(
        Row(
            (),
            "ProviderUnavailable", 200, "stream_truncated", False, 502, "provider_unavailable",
        ),
        id="the_body_ends_with_no_finish_reason",
    ),
]  # fmt: skip


def _claude(lead: tuple[bytes, ...], row: Row, retry: dict[str, Any]) -> tuple[Gateway, Upstream]:
    upstream = Upstream(lambda _: sse_response(*lead, *row.frames))
    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=upstream.transport)
    return Gateway(adapter, entries=(CLAUDE_ENTRY,), retry=retry), upstream


def _gemini(lead: tuple[bytes, ...], row: Row, retry: dict[str, Any]) -> tuple[Gateway, Upstream]:
    upstream = Upstream(lambda _: sse_response(*lead, *row.frames))
    adapter = GeminiAdapter(GEMINI_KEY, transport=upstream.transport)
    return Gateway(adapter, entries=(GEMINI_ENTRY,), retry=retry), upstream


Build = Callable[[tuple[bytes, ...], Row, dict[str, Any]], tuple[Gateway, Upstream]]

BOTH = [
    *[
        pytest.param(_claude, CLAUDE_ENTRY, (claude_start(),), claude_text("Hi"), *case.values,
                     id=f"claude-{case.id}")
        for case in CLAUDE_ROWS
    ],
    *[
        pytest.param(_gemini, GEMINI_ENTRY, (), gemini_text("Hi"), *case.values,
                     id=f"gemini-{case.id}")
        for case in GEMINI_ROWS
    ],
]  # fmt: skip

ARGUMENTS = ("build", "entry", "lead", "text", "row")


@pytest.mark.parametrize(ARGUMENTS, BOTH)
@pytest.mark.asyncio
async def test_before_the_first_event_the_row_is_a_json_error(
    build: Build,
    entry: Any,
    lead: tuple[bytes, ...],
    text: bytes,
    row: Row,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway, upstream = build(lead, row, {"max_attempts": 1})
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == row.client_status
    error = ErrorBody.model_validate(response.json()).error
    assert error.code == row.code
    assert error.message == MESSAGES[row.code]
    assert upstream.open_bodies == 0
    assert f" status={row.client_status} " in request_line(all_logs)
    assert lines_starting(all_logs, "stream_failed") == []


@pytest.mark.parametrize(ARGUMENTS, BOTH)
@pytest.mark.asyncio
async def test_before_the_first_event_the_row_decides_the_retry(
    build: Build,
    entry: Any,
    lead: tuple[bytes, ...],
    text: bytes,
    row: Row,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway, upstream = build(lead, row, {"max_attempts": 2, "base_delay": 0.25})
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert len(upstream.requests) == (2 if row.retries else 1)
    request_id = response.headers["x-request-id"]
    retry = "yes" if row.retries else "no"
    assert lines_starting(all_logs, "attempt_failed")[0] == attempt_line(
        entry, 1, row.status, row.reason, row.error, retry, request_id=request_id
    )


@pytest.mark.parametrize(ARGUMENTS, BOTH)
@pytest.mark.asyncio
async def test_after_the_first_event_the_row_is_an_error_event(
    build: Build,
    entry: Any,
    lead: tuple[bytes, ...],
    text: bytes,
    row: Row,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway, upstream = build((*lead, text), row, {"max_attempts": 3, "base_delay": 0.25})
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    events = parse_events(response.content)
    assert event_types(events) == ["delta", "error"]
    assert event_texts(events) == "Hi"
    request_id = response.headers["x-request-id"]
    assert events[-1] == {
        "type": "error",
        "code": row.code,
        "message": MESSAGES[row.code],
        "request_id": request_id,
    }
    assert len(upstream.requests) == 1
    assert upstream.open_bodies == 0
    assert lines_starting(all_logs, "stream_failed") == [
        failed_line(entry, row.status, row.reason, row.error, request_id=request_id)
    ]
    assert lines_starting(all_logs, "attempt_failed") == []
    assert " status=200 " in request_line(all_logs)


# The rows that come with the transport, either side of the line

TRANSPORT = [
    pytest.param(
        httpx.ReadTimeout, "ProviderTimeout", "timeout", 504, "provider_timeout",
        "The provider did not respond in time.", id="read_timeout",
    ),
    pytest.param(
        httpx.ReadError, "ProviderUnavailable", "connection", 502, "provider_unavailable",
        "The provider is unavailable.", id="read_error",
    ),
    pytest.param(
        httpx.RemoteProtocolError, "ProviderUnavailable", "connection", 502,
        "provider_unavailable", "The provider is unavailable.", id="protocol_error",
    ),
]  # fmt: skip


@pytest.mark.parametrize(("failure", "error", "reason", "status", "code", "message"), TRANSPORT)
@pytest.mark.parametrize(
    ("make", "entry", "lead", "text"),
    [
        pytest.param(
            lambda t: ClaudeAdapter(ANTHROPIC_KEY, transport=t),
            CLAUDE_ENTRY, (claude_start(),), claude_text("Hi"), id="claude",
        ),
        pytest.param(
            lambda t: GeminiAdapter(GEMINI_KEY, transport=t),
            GEMINI_ENTRY, (), gemini_text("Hi"), id="gemini",
        ),
    ],
)  # fmt: skip
@pytest.mark.asyncio
async def test_a_transport_failure_is_json_before_and_an_event_after(
    make: Callable[[httpx.MockTransport], Any],
    entry: Any,
    lead: tuple[bytes, ...],
    text: bytes,
    failure: type[httpx.TransportError],
    error: str,
    reason: str,
    status: int,
    code: str,
    message: str,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    before = Upstream(lambda _: sse_response(*lead, failure("failed")))
    gateway = Gateway(make(before.transport), entries=(entry,))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == status
    assert ErrorBody.model_validate(response.json()).error.code == code

    after = Upstream(lambda _: sse_response(*lead, text, failure("failed")))
    gateway = Gateway(make(after.transport), entries=(entry,))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    events = parse_events(response.content)
    assert event_types(events) == ["delta", "error"]
    assert (events[-1]["code"], events[-1]["message"]) == (code, message)
    assert lines_starting(all_logs, "stream_failed") == [
        failed_line(entry, "-", reason, error, request_id=response.headers["x-request-id"])
    ]
    assert before.open_bodies == after.open_bodies == 0


@pytest.mark.parametrize(
    ("make", "entry"),
    [
        pytest.param(
            lambda t: ClaudeAdapter(ANTHROPIC_KEY, transport=t), CLAUDE_ENTRY, id="claude"
        ),
        pytest.param(lambda t: GeminiAdapter(GEMINI_KEY, transport=t), GEMINI_ENTRY, id="gemini"),
    ],
)
@pytest.mark.parametrize(
    ("status", "client_status", "code"),
    [
        pytest.param(401, 502, "provider_auth_error", id="401"),
        pytest.param(402, 502, "provider_auth_error", id="402"),
        pytest.param(429, 503, "provider_rate_limited", id="429"),
        pytest.param(400, 400, "provider_bad_request", id="400"),
        pytest.param(500, 502, "provider_unavailable", id="500"),
        pytest.param(302, 502, "provider_unavailable", id="302"),
    ],
)
@pytest.mark.asyncio
async def test_a_non_2xx_answer_to_a_stream_is_the_usual_json_error(
    make: Callable[[httpx.MockTransport], Any],
    entry: Any,
    status: int,
    client_status: int,
    code: str,
) -> None:
    body = json.dumps({"error": {"message": PROVIDER_TEXT}}).encode()
    upstream = Upstream(lambda _: sse_response(body, status=status))
    gateway = Gateway(make(upstream.transport), entries=(entry,))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == client_status
    assert ErrorBody.model_validate(response.json()).error.code == code
    assert PROVIDER_TEXT not in response.text
    assert upstream.open_bodies == 0


@pytest.mark.asyncio
async def test_a_gemini_invalid_key_on_a_stream_is_the_502_auth_error(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    # D-048's body check on the stream path, where the body arrives unread.
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": PROVIDER_TEXT,
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                        "domain": "googleapis.com",
                    }
                ],
            }
        }
    ).encode()
    upstream = Upstream(lambda _: sse_response(body, status=400))
    adapter = GeminiAdapter(GEMINI_KEY, transport=upstream.transport)
    gateway = Gateway(adapter, entries=(GEMINI_ENTRY,))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 502
    assert ErrorBody.model_validate(response.json()).error.code == "provider_auth_error"
    assert PROVIDER_TEXT not in response.text
    assert lines_starting(all_logs, "attempt_failed") == [
        attempt_line(
            GEMINI_ENTRY,
            1,
            400,
            "API_KEY_INVALID",
            "ProviderAuthError",
            "no",
            request_id=response.headers["x-request-id"],
        )
    ]


@pytest.mark.asyncio
async def test_claude_finish_row_needs_the_message_delta_to_succeed() -> None:
    # The control for the malformed rows: the same frames with the stop reason serve.
    frames = (claude_start(), claude_text("Hi"), claude_message_delta(), CLAUDE_STOP)
    upstream = Upstream(lambda _: sse_response(*frames))
    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=upstream.transport)
    gateway = Gateway(adapter, entries=(CLAUDE_ENTRY,))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert event_types(parse_events(response.content)) == ["delta", "end"]
