"""Tests for a stream after its first event, and for bugs (M12, D-068, D-069).

After the first event a client holds part of one provider's answer, so nothing may be
retried and no other entry may be tried, whatever the failure's class, `retryable` flag
or status. The stream ends with one `error` event carrying section 13's fixed code and
message, the body ends normally, and there is no `end`, so a client can tell a whole
answer from a partial one. Each failure class below goes through every one of those
claims: retryable ones are the ones a careless router would retry.

A bug must not escape: uvicorn would log the exception's text with a traceback, so it
gets an `internal_error` event, and the only sign in the logs is the class name on a
request line at ERROR. `Wire` is used for that, because `httpx.ASGITransport` raises
whatever the app raises and hides whether the body ended.

Names M12 adds are reached at call time, so this file collects and each test fails on
its own.
"""

import logging
from collections.abc import Callable
from typing import Any

import pytest
from stream_support import (
    ADAPTER_MESSAGE,
    ALIAS,
    FIRST,
    SECOND,
    Done,
    Fail,
    Gateway,
    Interrupted,
    OddProviderError,
    Outcomes,
    ScriptedStream,
    Text,
    Wire,
    auth_error,
    bad_request,
    drain,
    event_texts,
    event_types,
    failed_line,
    is_end,
    lines_starting,
    malformed,
    parse_events,
    rate_limited,
    request_line,
    request_record,
    router_lines,
    stream_body,
    texts,
    timeout_error,
    truncated_2xx,
    unavailable,
    until,
)

from llmgateway.providers.base import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)

RETRIES = {"max_attempts": 3, "base_delay": 0.25, "max_delay": 4.0}

ErrorFactory = Callable[[], ProviderError]

# Every class, with the status, reason, code and message section 13 gives it.
FAILURES = [
    pytest.param(
        timeout_error, "-", "timeout", "ProviderTimeout",
        "provider_timeout", "The provider did not respond in time.", id="read_timeout",
    ),
    pytest.param(
        lambda: unavailable(None, "connection"), "-", "connection", "ProviderUnavailable",
        "provider_unavailable", "The provider is unavailable.", id="dropped",
    ),
    pytest.param(
        lambda: unavailable(None, "stream_error"), "-", "stream_error", "ProviderUnavailable",
        "provider_unavailable", "The provider is unavailable.", id="stream_error",
    ),
    pytest.param(
        lambda: unavailable(529), 529, "http_status", "ProviderUnavailable",
        "provider_unavailable", "The provider is unavailable.", id="overloaded_in_stream",
    ),
    pytest.param(
        malformed, 200, "malformed_body", "ProviderUnavailable",
        "provider_unavailable", "The provider is unavailable.", id="malformed",
    ),
    pytest.param(
        truncated_2xx, 200, "stream_truncated", "ProviderUnavailable",
        "provider_unavailable", "The provider is unavailable.", id="truncated",
    ),
    pytest.param(
        lambda: rate_limited(None), 429, "rate_limited", "ProviderRateLimited",
        "provider_rate_limited", "The provider is rate limiting the gateway.", id="rate_limited",
    ),
    pytest.param(
        bad_request, 400, "http_status", "ProviderBadRequest",
        "provider_bad_request", "The provider rejected the request.", id="bad_request",
    ),
    pytest.param(
        lambda: ProviderBadRequest(ADAPTER_MESSAGE, status=200, reason="prompt_blocked"),
        200, "prompt_blocked", "ProviderBadRequest",
        "provider_bad_request", "The provider rejected the request.", id="prompt_blocked",
    ),
    pytest.param(
        auth_error, 401, "auth", "ProviderAuthError",
        "provider_auth_error", "The gateway could not authenticate with the provider.", id="auth",
    ),
    pytest.param(
        lambda: ProviderAuthError(ADAPTER_MESSAGE, status=402, reason="billing"),
        402, "billing", "ProviderAuthError",
        "provider_auth_error", "The gateway could not authenticate with the provider.",
        id="billing",
    ),
    pytest.param(
        lambda: OddProviderError(ADAPTER_MESSAGE), "-", "-", "OddProviderError",
        "provider_error", "The provider request failed.", id="unmapped",
    ),
]  # fmt: skip

CASES = ("make", "status", "reason", "name", "code", "message")


def _gateway(make: ErrorFactory) -> tuple[Gateway, ScriptedStream, ScriptedStream, ProviderError]:
    error = make()
    first = ScriptedStream([Text("one"), Text("two"), Fail(error)])
    second = ScriptedStream()
    return Gateway(first, second, retry=RETRIES), first, second, error


# The router's side


@pytest.mark.parametrize(CASES, FAILURES)
@pytest.mark.asyncio
async def test_the_failure_is_raised_once_and_then_iteration_stops(
    make: ErrorFactory, status: Any, reason: str, name: str, code: str, message: str
) -> None:
    gateway, _, _, error = _gateway(make)
    served = await gateway.open()
    events, failure = await drain(served)
    assert failure is error
    assert texts(events) == ["one", "two"]
    assert not any(is_end(event) for event in events)
    assert [event async for event in served] == []


@pytest.mark.parametrize(CASES, FAILURES)
@pytest.mark.asyncio
async def test_there_is_no_retry_and_no_other_entry(
    make: ErrorFactory,
    status: Any,
    reason: str,
    name: str,
    code: str,
    message: str,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway, first, second, _ = _gateway(make)
    await drain(await gateway.open())
    assert (first.calls, second.calls) == (1, 0)
    assert gateway.sleep.calls == []
    assert lines_starting(all_logs, "fallback_taken") == []
    assert lines_starting(all_logs, "attempt_failed") == []


@pytest.mark.parametrize(CASES, FAILURES)
@pytest.mark.asyncio
async def test_the_generator_is_closed_when_the_stream_fails(
    make: ErrorFactory, status: Any, reason: str, name: str, code: str, message: str
) -> None:
    gateway, first, _, _ = _gateway(make)
    await drain(await gateway.open())
    assert first.open_generators == 0


@pytest.mark.asyncio
async def test_a_generator_that_stops_with_no_end_after_a_delta_is_truncated() -> None:
    first = ScriptedStream([Text("one")])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    events, failure = await drain(await gateway.open())
    assert texts(events) == ["one"]
    assert type(failure) is ProviderUnavailable
    assert (failure.status, failure.reason) == (None, "stream_truncated")
    assert failure.retryable is True
    assert (first.calls, second.calls) == (1, 0)


@pytest.mark.asyncio
async def test_a_stream_gives_one_end_and_nothing_after_it() -> None:
    adapter = ScriptedStream([Text("one"), Done(), Text("past the end"), Done()])
    events, failure = await drain(await Gateway(adapter).open())
    assert failure is None
    assert texts(events) == ["one"]
    assert len(events) == 2
    assert is_end(events[1])
    assert adapter.open_generators == 0


@pytest.mark.asyncio
async def test_a_failure_past_the_end_is_not_reported() -> None:
    adapter = ScriptedStream([Text("one"), Done(), Fail(unavailable(503))])
    events, failure = await drain(await Gateway(adapter).open())
    assert failure is None
    assert is_end(events[-1])


@pytest.mark.asyncio
async def test_the_generator_is_closed_at_a_clean_end() -> None:
    adapter = ScriptedStream()
    await drain(await Gateway(adapter).open())
    assert adapter.open_generators == 0


@pytest.mark.asyncio
async def test_a_stream_that_fell_back_commits_to_the_entry_that_served(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    error = unavailable(None, "connection")
    first = ScriptedStream([Fail(unavailable(503))])
    second = ScriptedStream([Text("one"), Fail(error)])
    third = ScriptedStream()
    gateway = Gateway(first, second, third)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert event_types(parse_events(response.content)) == ["delta", "error"]
    assert (first.calls, second.calls, third.calls) == (1, 1, 0)
    assert lines_starting(all_logs, "stream_failed") == [
        failed_line(
            SECOND,
            "-",
            "connection",
            "ProviderUnavailable",
            request_id=response.headers["x-request-id"],
        )
    ]
    assert len(lines_starting(all_logs, "fallback_taken")) == 1


# What the client sees (D-069)


@pytest.mark.parametrize(CASES, FAILURES)
@pytest.mark.asyncio
async def test_the_client_gets_the_deltas_then_one_error_and_no_end(
    make: ErrorFactory, status: Any, reason: str, name: str, code: str, message: str
) -> None:
    gateway, _, _, _ = _gateway(make)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    events = parse_events(response.content)
    assert event_types(events) == ["delta", "delta", "error"]
    assert event_texts(events) == "onetwo"
    assert events[-1] == {
        "type": "error",
        "code": code,
        "message": message,
        "request_id": response.headers["x-request-id"],
    }


@pytest.mark.parametrize(CASES, FAILURES)
@pytest.mark.asyncio
async def test_one_stream_failed_line_and_the_request_line_keeps_200(
    make: ErrorFactory,
    status: Any,
    reason: str,
    name: str,
    code: str,
    message: str,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway, _, _, _ = _gateway(make)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    request_id = response.headers["x-request-id"]
    assert lines_starting(all_logs, "stream_failed") == [
        failed_line(FIRST, status, reason, name, request_id=request_id)
    ]
    record = next(r for r in all_logs.records if r.getMessage().startswith("stream_failed"))
    assert record.name == "llmgateway.router"
    assert record.levelno == logging.WARNING
    assert router_lines(all_logs) == lines_starting(all_logs, "stream_failed")
    line = request_line(all_logs)
    assert f"provider={FIRST.instance} model={ALIAS!r} status=200 latency_ms=" in line
    assert request_record(all_logs).levelno == logging.INFO
    assert " error=" not in line


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.asyncio
async def test_the_body_ends_normally_after_the_error_event(spec_version: str) -> None:
    gateway, first, _, _ = _gateway(lambda: unavailable(503))
    wire = Wire(stream_body(), spec_version=spec_version)
    async with wire.running(gateway.app()):
        await wire.finished()
    assert wire.status == 200
    assert wire.ended
    assert event_types(wire.events) == ["delta", "delta", "error"]
    assert first.open_generators == 0


@pytest.mark.asyncio
async def test_a_failure_right_after_the_first_event_keeps_the_200() -> None:
    adapter = ScriptedStream([Text("one"), Fail(timeout_error())])
    gateway = Gateway(adapter)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    assert event_types(parse_events(response.content)) == ["delta", "error"]


@pytest.mark.asyncio
async def test_truncation_after_a_delta_reaches_the_client_as_unavailable(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(ScriptedStream([Text("one")]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    events = parse_events(response.content)
    assert event_types(events) == ["delta", "error"]
    assert events[-1]["code"] == "provider_unavailable"
    assert lines_starting(all_logs, "stream_failed") == [
        failed_line(
            FIRST,
            "-",
            "stream_truncated",
            "ProviderUnavailable",
            request_id=response.headers["x-request-id"],
        )
    ]


@pytest.mark.parametrize(
    ("make", "name"),
    [
        pytest.param(lambda: ProviderTimeout(ADAPTER_MESSAGE), "ProviderTimeout", id="timeout"),
        pytest.param(lambda: unavailable(None, None), "ProviderUnavailable", id="unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_a_failure_with_no_reason_logs_dashes(
    make: ErrorFactory, name: str, all_logs: pytest.LogCaptureFixture
) -> None:
    gateway = Gateway(ScriptedStream([Text("one"), Fail(make())]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert lines_starting(all_logs, "stream_failed") == [
        failed_line(FIRST, "-", "-", name, request_id=response.headers["x-request-id"])
    ]


@pytest.mark.asyncio
async def test_a_clean_stream_logs_no_stream_lines_and_a_plain_request_line(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(ScriptedStream())
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    assert router_lines(all_logs) == []
    line = request_line(all_logs)
    assert f"provider={FIRST.instance} model={ALIAS!r} status=200 latency_ms=" in line
    assert line.endswith(f" provider_model={FIRST.model!r}")
    assert request_record(all_logs).levelno == logging.INFO


# A bug (D-069)


class SomeBug(Exception):
    """A class name a log line can only have got from the exception."""


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.asyncio
async def test_a_bug_after_the_first_event_does_not_escape_the_app(spec_version: str) -> None:
    adapter = ScriptedStream([Text("one"), Fail(SomeBug("a bug"))])
    gateway = Gateway(adapter)
    wire = Wire(stream_body(), spec_version=spec_version)
    async with wire.running(gateway.app()):
        await wire.finished()
    assert wire.status == 200
    assert wire.ended
    events = wire.events
    assert event_types(events) == ["delta", "error"]
    assert events[-1] == {
        "type": "error",
        "code": "internal_error",
        "message": "Internal server error.",
        "request_id": wire.headers["x-request-id"],
    }
    assert adapter.open_generators == 0


@pytest.mark.asyncio
async def test_a_bug_after_the_first_event_writes_no_stream_failed_line(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(ScriptedStream([Text("one"), Fail(SomeBug("a bug"))]))
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert event_types(wire.events) == ["delta", "error"]
    assert router_lines(all_logs) == []


@pytest.mark.asyncio
async def test_a_bug_after_the_first_event_writes_the_request_line_at_error(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(ScriptedStream([Text("one"), Fail(SomeBug("a bug"))]))
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    record = request_record(all_logs)
    assert record.levelno == logging.ERROR
    line = record.getMessage()
    assert f"provider={FIRST.instance} model={ALIAS!r} status=200 latency_ms=" in line
    assert line.endswith(f" provider_model={FIRST.model!r} error=SomeBug")


@pytest.mark.asyncio
async def test_a_bug_after_the_first_event_is_never_retried_or_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    first = ScriptedStream([Text("one"), Fail(SomeBug("a bug"))])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert (first.calls, second.calls) == (1, 0)
    assert gateway.sleep.calls == []
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_an_exception_past_except_exception_still_closes_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not a bug the gateway can answer, but the upstream and the permit must not leak.
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream([Text("one"), Fail(Interrupted())])
    gateway = Gateway(adapter)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        assert wire.task is not None
        await until(wire.task.done, "the app to stop")
    assert adapter.open_generators == 0
    assert outcomes.of(gateway.breaker()) == ["neutral"]
