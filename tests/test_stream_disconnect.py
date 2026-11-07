"""Tests for a client that leaves or stops reading (M12, D-072).

The owner's rule for M12: a disconnect closes the upstream promptly. Output nobody reads
is still billed, so the close cannot wait for the next chunk, and it cannot be left to
the garbage collector.

The trap D-072 names, read from the installed Starlette: when a disconnect's cancel lands
in `send`, `async for` leaves the generator suspended at its `yield`, and the upstream
stays open. Only a `send` blocked mid-write reproduces that, which `httpx.ASGITransport`
cannot do (it delivers `http.disconnect` after the response is complete). So:

- `Wire` drives the app with its own `receive` and `send`. "Promptly" means within a
  bounded number of event loop turns with the fake clock standing still, so no timer can
  be the cause, and a broken gateway fails in milliseconds. Each test runs on both sides
  of ASGI spec 2.4, because Starlette behaves differently on each and D-072 depends on
  neither.
- The last section is the real thing: real uvicorn on 127.0.0.1, in this process, a real
  local TCP upstream speaking SSE, a real adapter on a real `httpx.AsyncHTTPTransport`
  whose URL is rewritten to it, and a real client socket. `stream_idle_timeout` and
  httpx's read limit are set far too high to be the cause of anything. The only waits
  are on events; `GUARD` bounds them, and is explained where it is defined.

No test calls anything but 127.0.0.1. Names M12 adds are reached at call time, so this
file collects and each test fails on its own.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from stream_support import (
    ALIAS,
    ANTHROPIC_KEY,
    COOLDOWN,
    FIRST,
    GEMINI_KEY,
    PROMPT_MARKER,
    SECOND,
    TICK,
    Body,
    Done,
    Entry,
    Fail,
    Gateway,
    Hold,
    Outcomes,
    Park,
    ParkedSleep,
    ScriptedStream,
    Silence,
    Stall,
    Takes,
    Text,
    Upstream,
    Wire,
    abandoned_line,
    at_about,
    claude_start,
    claude_text,
    event_texts,
    event_types,
    gemini_text,
    gemini_thought,
    lines_starting,
    request_line,
    router_lines,
    stream_body,
    unavailable,
    until,
)

from llmgateway.app import create_app
from llmgateway.providers.base import ProviderResult, ProviderUnavailable
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.providers.mock import MockProvider
from llmgateway.router import RetryPolicy, Route, Router
from llmgateway.schema import ChatRequest, Provider, StreamDelta

SPECS = pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
# So high that nothing in these tests can be its doing.
NEVER = 3600.0
RETRIES = {"max_attempts": 3, "base_delay": 0.25, "max_delay": 4.0}


def _abandoned(all_logs: pytest.LogCaptureFixture) -> logging.LogRecord:
    (record,) = [r for r in all_logs.records if r.getMessage().startswith("stream_abandoned")]
    return record


def _request_id(wire: Wire) -> str:
    return wire.headers["x-request-id"]


# After the first event, with the cancel landing in `send` (D-072's trap)


@SPECS
@pytest.mark.asyncio
async def test_a_disconnect_during_a_blocked_send_closes_a_generator_parked_at_a_yield(
    spec_version: str,
    monkeypatch: pytest.MonkeyPatch,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream([Text("one"), Text("two"), Text("three"), Text("four"), Done()])
    second = ScriptedStream()
    gateway = Gateway(adapter, second, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), spec_version=spec_version, plans={2: Park()})
    async with wire.running(gateway.app()):
        await wire.wait_for(wire.send_blocked.is_set, "the second delta's send to block")
        # The generator is parked at a `yield`, with the cancel about to land in `send`.
        assert adapter.open_generators == 1
        wire.disconnect()
        await wire.wait_for(lambda: bool(adapter.exits), "the adapter's finally to run")
        await wire.finished()
    assert adapter.exits == ["closed"]
    assert wire.sends_cancelled == 1
    assert (adapter.calls, second.calls) == (1, 0)
    assert event_texts(wire.events) == "one"
    assert outcomes.of(gateway.breaker()) == ["neutral"]
    assert lines_starting(all_logs, "stream_abandoned") == [
        abandoned_line(FIRST, "client_gone", request_id=_request_id(wire))
    ]
    assert _abandoned(all_logs).levelno == logging.INFO
    assert _abandoned(all_logs).name == "llmgateway.router"
    assert router_lines(all_logs) == lines_starting(all_logs, "stream_abandoned")
    assert " status=200 " in request_line(all_logs)


@SPECS
@pytest.mark.asyncio
async def test_a_disconnect_while_the_provider_is_silent_cancels_the_read(
    spec_version: str,
    monkeypatch: pytest.MonkeyPatch,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    # Why a poll between events would not do: the loop is blocked on the provider.
    outcomes = Outcomes(monkeypatch)
    hold = Hold()
    adapter = ScriptedStream([Text("one"), hold, Text("late"), Done()])
    gateway = Gateway(adapter, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), spec_version=spec_version)
    async with wire.running(gateway.app()):
        await wire.wait_for(hold.reached.is_set, "the adapter to wait for the provider")
        await wire.wait_for(lambda: wire.event_count == 1, "the first event to reach the client")
        wire.disconnect()
        await wire.wait_for(lambda: bool(adapter.exits), "the adapter's finally to run")
        await wire.finished()
    assert adapter.exits == ["cancelled"]
    assert outcomes.of(gateway.breaker()) == ["neutral"]
    assert lines_starting(all_logs, "stream_abandoned") == [
        abandoned_line(FIRST, "client_gone", request_id=_request_id(wire))
    ]
    assert lines_starting(all_logs, "stream_failed") == []
    assert " status=200 " in request_line(all_logs)


@SPECS
@pytest.mark.asyncio
async def test_a_disconnect_closes_a_real_adapters_upstream(
    spec_version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stall() -> None:
        await asyncio.Event().wait()

    upstream = Upstream(
        lambda _: httpx.Response(
            200, stream=Body(claude_start(), claude_text("one"), claude_text("two"), stall)
        )
    )
    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=upstream.transport)
    gateway = Gateway(adapter, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), spec_version=spec_version, plans={2: Park()})
    async with wire.running(gateway.app()):
        await wire.wait_for(wire.send_blocked.is_set, "the second delta's send to block")
        assert upstream.open_bodies == 1
        wire.disconnect()
        await wire.wait_for(lambda: upstream.open_bodies == 0, "the upstream to be closed")
        await wire.finished()
    assert len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_a_client_can_leave_a_mock_stream_between_deltas(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # D-077: the mock yields to the loop before each delta so this can really happen.
    adapter = MockProvider()
    gateway = Gateway(adapter, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), plans={2: Park()})
    async with wire.running(gateway.app()):
        await wire.wait_for(wire.send_blocked.is_set, "the second delta's send to block")
        wire.disconnect()
        await wire.finished()
    assert adapter.call_count == 1
    assert lines_starting(all_logs, "stream_abandoned") == [
        abandoned_line(FIRST, "client_gone", request_id=_request_id(wire))
    ]


@pytest.mark.asyncio
async def test_a_disconnect_with_a_probe_frees_the_slot_and_keeps_the_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = Hold()
    adapter = ScriptedStream([Text("one"), hold, Done()])
    gateway = Gateway(adapter, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    gateway.half_open()
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.wait_for(hold.reached.is_set, "the probe to stream")
        assert gateway.breaker().acquire() is None
        wire.disconnect()
        await wire.finished()
    assert gateway.breaker().state == "half_open"
    assert gateway.breaker().acquire() is not None


@pytest.mark.asyncio
async def test_a_disconnect_after_the_response_ended_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream()
    gateway = Gateway(adapter)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
        wire.disconnect()
    assert router_lines(all_logs) == []
    assert outcomes.of(gateway.breaker()) == ["success"]


@pytest.mark.asyncio
async def test_a_stream_that_failed_is_not_also_abandoned(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream([Text("one"), Fail(unavailable(503))])
    gateway = Gateway(adapter)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert lines_starting(all_logs, "stream_abandoned") == []
    assert len(lines_starting(all_logs, "stream_failed")) == 1
    assert outcomes.of(gateway.breaker()) == ["failure"]


@pytest.mark.asyncio
async def test_a_cancel_from_outside_closes_the_generator_and_is_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A server shutting down cancels the app's task. `finally` closes every path (D-072).
    outcomes = Outcomes(monkeypatch)
    hold = Hold()
    adapter = ScriptedStream([Text("one"), hold, Done()])
    gateway = Gateway(adapter, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.wait_for(hold.reached.is_set, "the adapter to wait for the provider")
        assert wire.task is not None
        wire.task.cancel()
        await until(wire.task.done, "the app to stop")
    assert adapter.open_generators == 0
    assert outcomes.of(gateway.breaker()) == ["neutral"]


# ServedStream.aclose (D-072)


@pytest.mark.parametrize("reason", ["client_gone", "client_stalled"])
@pytest.mark.asyncio
async def test_aclose_closes_the_generator_and_writes_one_line(
    reason: str, monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream([Text("one"), Text("two"), Done()])
    gateway = Gateway(adapter)
    served = await gateway.open(request_id="gw_1")
    await served.aclose(reason=reason)
    assert adapter.open_generators == 0
    assert lines_starting(all_logs, "stream_abandoned") == [
        abandoned_line(FIRST, reason, request_id="gw_1")
    ]
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_aclose_defaults_to_client_gone(all_logs: pytest.LogCaptureFixture) -> None:
    served = await Gateway(ScriptedStream()).open()
    await served.aclose()
    assert lines_starting(all_logs, "stream_abandoned") == [abandoned_line(FIRST, "client_gone")]


@pytest.mark.asyncio
async def test_aclose_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream()
    gateway = Gateway(adapter)
    served = await gateway.open()
    await served.aclose()
    await served.aclose()
    await served.aclose(reason="client_stalled")
    assert adapter.exits == ["closed"]
    assert len(lines_starting(all_logs, "stream_abandoned")) == 1
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_aclose_on_a_stream_that_reached_its_end_does_nothing(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream())
    served = await gateway.open()
    async for _ in served:
        pass
    await served.aclose()
    assert router_lines(all_logs) == []
    assert outcomes.of(gateway.breaker()) == ["success"]


@pytest.mark.asyncio
async def test_aclose_on_a_stream_that_failed_does_nothing(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text("one"), Fail(unavailable(503))]))
    served = await gateway.open()
    with pytest.raises(ProviderUnavailable):
        async for _ in served:
            pass
    await served.aclose()
    assert lines_starting(all_logs, "stream_abandoned") == []
    assert outcomes.of(gateway.breaker()) == ["failure"]


# While opening (D-072): the attempt in flight is cancelled


@SPECS
@pytest.mark.asyncio
async def test_a_disconnect_while_opening_cancels_the_attempt_in_flight(
    spec_version: str,
    monkeypatch: pytest.MonkeyPatch,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    outcomes = Outcomes(monkeypatch)
    hold = Hold()
    first = ScriptedStream([hold, Text("late"), Done()])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), spec_version=spec_version)
    async with wire.running(gateway.app()):
        await wire.wait_for(hold.reached.is_set, "the attempt to be in flight")
        assert wire.sent == []
        wire.disconnect()
        await wire.wait_for(lambda: bool(first.exits), "the attempt to be cancelled")
        await wire.finished()
    assert first.exits == ["cancelled"]
    assert (first.calls, second.calls) == (1, 0)
    assert gateway.sleep.calls == []
    assert wire.sent == []
    assert all(message.get("status") != 200 for message in wire.sent_after_gone)
    assert outcomes.of(gateway.breaker()) == ["neutral"]
    assert outcomes.of(gateway.router.breaker(SECOND.instance)) == []
    assert router_lines(all_logs) == []
    assert f"provider={FIRST.instance} model={ALIAS!r} status=499 latency_ms=" in request_line(
        all_logs
    )


@SPECS
@pytest.mark.asyncio
async def test_a_disconnect_while_opening_closes_a_real_adapters_upstream(
    spec_version: str, monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    reached = asyncio.Event()

    async def stall() -> None:
        reached.set()
        await asyncio.Event().wait()

    upstream = Upstream(lambda _: httpx.Response(200, stream=Body(gemini_thought(), stall)))
    adapter = GeminiAdapter(GEMINI_KEY, transport=upstream.transport)
    gateway = Gateway(adapter, retry=RETRIES)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), spec_version=spec_version)
    async with wire.running(gateway.app()):
        await wire.wait_for(reached.is_set, "the read to stall after a thought")
        wire.disconnect()
        await wire.wait_for(lambda: upstream.open_bodies == 0, "the upstream to be closed")
        await wire.finished()
    assert len(upstream.requests) == 1
    assert " status=499 " in request_line(all_logs)


@pytest.mark.asyncio
async def test_a_disconnect_while_opening_on_a_probe_frees_the_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = Hold()
    adapter = ScriptedStream([hold, Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    gateway.half_open()
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.wait_for(hold.reached.is_set, "the probe to be in flight")
        assert gateway.breaker().acquire() is None
        wire.disconnect()
        await wire.finished()
    assert gateway.breaker().state == "half_open"
    assert gateway.breaker().acquire() is not None


@pytest.mark.asyncio
async def test_a_disconnect_during_a_backoff_wait_stops_at_once(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    sleep = ParkedSleep()
    first = ScriptedStream([Fail(unavailable(503))])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES, sleep=sleep)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.wait_for(sleep.waiting.is_set, "the backoff wait")
        wire.disconnect()
        await wire.finished()
    assert sleep.calls == [0.125]
    assert (first.calls, second.calls) == (1, 0)
    assert lines_starting(all_logs, "fallback_taken") == []
    assert len(lines_starting(all_logs, "attempt_failed")) == 1
    assert " status=499 " in request_line(all_logs)


@pytest.mark.asyncio
async def test_a_client_gone_before_anything_ran_gets_no_open_generator(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    adapter = ScriptedStream([Hold(), Done()])
    gateway = Gateway(adapter)
    wire = Wire(stream_body())
    wire.disconnect()
    async with wire.running(gateway.app()):
        await wire.finished()
    assert adapter.open_generators == 0
    assert " status=499 " in request_line(all_logs)


@pytest.mark.asyncio
async def test_a_stream_begun_before_the_breaker_opened_cannot_move_it_by_leaving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Its end changes nothing (D-057), and the cooldown does not restart.
    hold = Hold()
    adapter = ScriptedStream([Text("one"), hold, Done()])
    gateway = Gateway(adapter, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.wait_for(hold.reached.is_set, "the stream to run")
        gateway.trip()
        gateway.clock.advance(10.0)
        wire.disconnect()
        await wire.finished()
    assert gateway.breaker().state == "open"
    assert gateway.breaker().retry_after() == COOLDOWN - 10.0


# The watcher (D-072): the only reader of `receive`


@pytest.mark.asyncio
async def test_receive_has_one_reader_from_the_body_to_the_end_of_the_response() -> None:
    # A retry in the path, so the router's own client check runs: it must read the
    # watcher's state and not `receive`.
    first = ScriptedStream([Fail(unavailable(503))], [Text("Hello"), Done()])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert wire.status == 200
    assert wire.receive_calls == 2
    assert wire.receives_peak == 1


@pytest.mark.asyncio
async def test_the_watcher_ends_when_the_response_ends() -> None:
    gateway = Gateway(ScriptedStream())
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert wire.status == 200
    assert wire.receive_calls == 2
    assert wire.receives_waiting == 0


@pytest.mark.asyncio
async def test_the_watcher_ends_when_a_stream_ends_in_a_json_error() -> None:
    gateway = Gateway(ScriptedStream([Fail(unavailable(503))]))
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert wire.status == 502
    assert wire.receives_waiting == 0


# A client that stops reading (D-072)


@pytest.mark.parametrize(
    "index",
    [pytest.param(1, id="the_first_delta"), pytest.param(2, id="a_later_delta")],
)
@SPECS
@pytest.mark.asyncio
async def test_a_stalled_send_drops_the_stream_at_the_idle_limit(
    index: int,
    spec_version: str,
    monkeypatch: pytest.MonkeyPatch,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream([Text("one"), Text("two"), Text("three"), Done()])
    second = ScriptedStream()
    gateway = Gateway(adapter, second, idle=10.0)
    gateway.virtual_time(monkeypatch)
    start = gateway.clock()
    wire = Wire(
        stream_body(), spec_version=spec_version, clock=gateway.clock, plans={index: Stall()}
    )
    async with wire.running(gateway.app()):
        await wire.finished()
    assert at_about(gateway.clock(), start + 10.0)
    assert adapter.open_generators == 0
    assert adapter.exits == ["closed"]
    assert (adapter.calls, second.calls) == (1, 0)
    assert wire.sends_cancelled == 1
    # Returning without ending the body is what makes the server drop the connection.
    assert not wire.ended
    assert outcomes.of(gateway.breaker()) == ["neutral"]
    assert lines_starting(all_logs, "stream_abandoned") == [
        abandoned_line(FIRST, "client_stalled", request_id=_request_id(wire))
    ]
    assert _abandoned(all_logs).levelno == logging.INFO
    assert lines_starting(all_logs, "stream_failed") == []
    assert " status=200 " in request_line(all_logs)


@pytest.mark.asyncio
async def test_a_send_stalled_on_the_response_start_is_dropped_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream()
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    start = gateway.clock()
    wire = Wire(stream_body(), clock=gateway.clock, plans={0: Stall()})
    async with wire.running(gateway.app()):
        await wire.finished()
    assert at_about(gateway.clock(), start + 10.0)
    assert adapter.open_generators == 0
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_a_stalled_client_frees_a_probe_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = ScriptedStream([Text("one"), Text("two"), Done()])
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    gateway.half_open()
    wire = Wire(stream_body(), clock=gateway.clock, plans={1: Stall()})
    async with wire.running(gateway.app()):
        await wire.finished()
    assert gateway.breaker().state == "half_open"
    assert gateway.breaker().acquire() is not None


@pytest.mark.asyncio
async def test_a_slow_client_under_the_limit_on_every_send_is_not_dropped(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    adapter = ScriptedStream([Text("one"), Text("two"), Text("three"), Done()])
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    start = gateway.clock()
    slow = Takes(10.0 - TICK)
    wire = Wire(stream_body(), clock=gateway.clock, plans={i: slow for i in range(5)})
    async with wire.running(gateway.app()):
        await wire.finished()
    assert event_types(wire.events) == ["delta", "delta", "delta", "end"]
    assert wire.ended
    assert gateway.clock() - start > 30.0
    assert router_lines(all_logs) == []


@pytest.mark.asyncio
async def test_time_blocked_on_the_client_does_not_count_against_the_provider(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # Each gap holds 8 s on the client and 8 s on the provider: 16 s between events, under
    # a 10 s limit that counts only time spent on the adapter.
    adapter = ScriptedStream(
        [Text("one"), Silence(8.0), Text("two"), Silence(8.0), Text("three"), Silence(8.0), Done()]
    )
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    slow = Takes(8.0)
    wire = Wire(stream_body(), clock=gateway.clock, plans={1: slow, 2: slow, 3: slow})
    async with wire.running(gateway.app()):
        await wire.finished()
    assert event_types(wire.events) == ["delta", "delta", "delta", "end"]
    assert lines_starting(all_logs, "stream_failed") == []
    assert lines_starting(all_logs, "stream_abandoned") == []


@pytest.mark.asyncio
async def test_a_disconnect_during_a_stall_is_client_gone(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    adapter = ScriptedStream([Text("one"), Text("two"), Done()])
    gateway = Gateway(adapter, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), plans={1: Park()})
    async with wire.running(gateway.app()):
        await wire.wait_for(wire.send_blocked.is_set, "the send to block")
        wire.disconnect()
        await wire.finished()
    assert lines_starting(all_logs, "stream_abandoned") == [
        abandoned_line(FIRST, "client_gone", request_id=_request_id(wire))
    ]


# The real thing: real uvicorn, a real upstream, a real adapter, a real client socket

# Every wait below is on an event that a working gateway sets at once, and GUARD only
# turns a broken gateway into a failure. It is far above any host stall measured here (a
# 60 s limit once failed a healthy child coming out of Modern Standby), far below
# NEVER and the httpx limit below, so it cannot be mistaken for them, and below the 300 s
# per-test limit, because on Windows that limit ends the whole run (D-066).
GUARD = 200.0
LONG_TIMEOUT = httpx.Timeout(connect=30.0, read=NEVER, write=30.0, pool=30.0)


class RewritingTransport(httpx.AsyncBaseTransport):
    """Sends what the adapter asks for to the local upstream, over a real socket."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


class TcpUpstream:
    """A local server that answers a POST with SSE frames, then stalls, as a provider can.

    It reads until the peer closes, so `closed` is set by the connection really closing:
    nothing else in this class ever sets it.
    """

    def __init__(self, *frames: bytes) -> None:
        self.frames = frames
        self.requests: list[bytes] = []
        self.connections = 0
        self.stalled = asyncio.Event()
        self.closed = asyncio.Event()
        self.handlers: set[asyncio.Task[Any]] = set()
        self.server: asyncio.Server | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        port: int = self.server.sockets[0].getsockname()[1]
        return port

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.handlers.add(task)
        self.connections += 1
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":")[1])
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            self.requests.append(head + await reader.readexactly(length))
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                b"transfer-encoding: chunked\r\n\r\n"
            )
            for frame in self.frames:
                writer.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            await writer.drain()
            self.stalled.set()
            with suppress(ConnectionError):
                while await reader.read(65536):
                    pass
            self.closed.set()
        finally:
            writer.close()

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
        for task in self.handlers:
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)


class LineWaiter(logging.Handler):
    """Sets `seen` when a line starting with `prefix` is logged, at any level."""

    def __init__(self, logger: str, prefix: str) -> None:
        super().__init__(logging.DEBUG)
        self.logger = logging.getLogger(logger)
        self.prefix = prefix
        self.seen = asyncio.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if self.prefix in record.getMessage():
            self.seen.set()

    def __enter__(self) -> "LineWaiter":
        self.logger.addHandler(self)
        return self

    def __exit__(self, *_: object) -> None:
        self.logger.removeHandler(self)


@asynccontextmanager
async def _serving(app: FastAPI) -> AsyncIterator[int]:
    """A real uvicorn in this process, on an ephemeral port on 127.0.0.1."""
    # log_config=None leaves logging alone, so every line reaches `all_logs`.
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="off")
    server = uvicorn.Server(config)
    serving = asyncio.ensure_future(server.serve())
    try:
        while not server.started and not serving.done():  # noqa: ASYNC110
            await asyncio.sleep(0)
        assert server.started, "the server did not start"
        port: int = server.servers[0].sockets[0].getsockname()[1]
        yield port
    finally:
        server.should_exit = True
        server.force_exit = True
        await serving


def _post(port: int, body: dict[str, Any]) -> bytes:
    payload = json.dumps(body).encode()
    return (
        b"POST /v1/chat HTTP/1.1\r\nhost: 127.0.0.1\r\ncontent-type: application/json\r\n"
        + f"content-length: {len(payload)}\r\n\r\n".encode()
        + payload
    )


def _assert_no_content_logged(all_logs: pytest.LogCaptureFixture) -> None:
    # uvicorn, httpx and httpcore all log on these paths, at every level.
    written = all_logs.text + "".join(str(vars(record)) for record in all_logs.records)
    for marker in (PROMPT_MARKER, ANTHROPIC_KEY, GEMINI_KEY, "first delta"):
        assert marker not in written


async def _head_is_200(reader: asyncio.StreamReader) -> None:
    # Before any wait on the gateway, so an answer that is not a stream fails at once.
    head = await reader.readuntil(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 "), head


async def _upstream_stalls_with_nothing_sent(
    upstream: TcpUpstream, reader: asyncio.StreamReader
) -> None:
    # Nothing may reach the client before the first event, so the upstream's stall comes
    # first. A byte first fails at once instead of waiting out GUARD.
    stalled = asyncio.ensure_future(upstream.stalled.wait())
    byte = asyncio.ensure_future(reader.read(1))
    await asyncio.wait({stalled, byte}, return_when=asyncio.FIRST_COMPLETED)
    assert stalled.done(), byte.result()
    byte.cancel()


def _policy(**arguments: Any) -> RetryPolicy:
    return RetryPolicy(**arguments)


def _real_router(adapter: Any, entry: Entry, policy: RetryPolicy) -> Router:
    route = Route(provider=entry.instance, model=entry.model, adapter=adapter)
    return Router({ALIAS: [route]}, policy=policy)


CLAUDE_REAL = Entry("claude-instance", "claude-haiku-5-5")
GEMINI_REAL = Entry("gemini-instance", "gemini-3.5-flash-lite")


def _real_claude(transport: httpx.AsyncBaseTransport) -> Any:
    return ClaudeAdapter(ANTHROPIC_KEY, transport=transport, timeout=LONG_TIMEOUT)


def _real_gemini(transport: httpx.AsyncBaseTransport) -> Any:
    return GeminiAdapter(GEMINI_KEY, transport=transport, timeout=LONG_TIMEOUT)


REAL_ADAPTERS = [
    pytest.param(
        _real_claude,
        CLAUDE_REAL,
        (claude_start(), claude_text("first delta")),
        id="claude",
    ),
    pytest.param(
        _real_gemini,
        GEMINI_REAL,
        (gemini_thought(), gemini_text("first delta")),
        id="gemini",
    ),
]


@pytest.mark.parametrize(("make", "entry", "frames"), REAL_ADAPTERS)
@pytest.mark.asyncio
async def test_a_real_client_that_leaves_closes_the_real_upstream(
    make: Any,
    entry: Entry,
    frames: tuple[bytes, ...],
    monkeypatch: pytest.MonkeyPatch,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    outcomes = Outcomes(monkeypatch)
    upstream = TcpUpstream(*frames)
    upstream_port = await upstream.start()
    adapter = make(RewritingTransport(upstream_port))
    router = _real_router(adapter, entry, _policy(stream_idle_timeout=NEVER))
    client: asyncio.StreamWriter | None = None
    try:
        with (
            LineWaiter("llmgateway.router", "stream_abandoned") as abandoned,
            LineWaiter("llmgateway.request", " status=200 ") as finished,
        ):
            async with _serving(create_app(router=router)) as port:
                reader, client = await asyncio.open_connection("127.0.0.1", port)
                client.write(_post(port, stream_body()))
                await client.drain()
                async with asyncio.timeout(GUARD):
                    await _head_is_200(reader)
                    await reader.readuntil(b"event: delta\ndata: ")
                    first = await reader.readuntil(b"\n\n")
                    assert b"first delta" in first
                    # The upstream has stalled, and the client got the delta anyway.
                    await upstream.stalled.wait()
                    assert not upstream.closed.is_set()
                    client.close()
                    await upstream.closed.wait()
                    await abandoned.seen.wait()
                    await finished.seen.wait()
    finally:
        if client is not None:
            client.close()
        await adapter.aclose()
        await upstream.stop()
    assert upstream.connections == 1
    assert len(upstream.requests) == 1
    assert outcomes.of(router.breaker(entry.instance)) == ["neutral"]
    (line,) = lines_starting(all_logs, "stream_abandoned")
    assert line.endswith(f"model={entry.model!r} reason=client_gone")
    assert lines_starting(all_logs, "stream_failed") == []
    _assert_no_content_logged(all_logs)


@pytest.mark.parametrize(
    ("make", "entry", "frames"),
    [
        pytest.param(_real_claude, CLAUDE_REAL, (claude_start(),), id="claude_after_a_200"),
        pytest.param(_real_gemini, GEMINI_REAL, (gemini_thought(),), id="gemini_after_a_thought"),
        pytest.param(_real_claude, CLAUDE_REAL, (), id="claude_with_no_frame"),
    ],
)
@pytest.mark.asyncio
async def test_a_real_client_that_leaves_while_opening_closes_the_real_upstream(
    make: Any,
    entry: Entry,
    frames: tuple[bytes, ...],
    monkeypatch: pytest.MonkeyPatch,
    all_logs: pytest.LogCaptureFixture,
) -> None:
    outcomes = Outcomes(monkeypatch)
    upstream = TcpUpstream(*frames)
    upstream_port = await upstream.start()
    adapter = make(RewritingTransport(upstream_port))
    router = _real_router(adapter, entry, _policy())
    client: asyncio.StreamWriter | None = None
    try:
        with LineWaiter("llmgateway.request", " status=499 ") as finished:
            async with _serving(create_app(router=router)) as port:
                reader, client = await asyncio.open_connection("127.0.0.1", port)
                client.write(_post(port, stream_body()))
                await client.drain()
                async with asyncio.timeout(GUARD):
                    await _upstream_stalls_with_nothing_sent(upstream, reader)
                    client.close()
                    await upstream.closed.wait()
                    await finished.seen.wait()
    finally:
        if client is not None:
            client.close()
        await adapter.aclose()
        await upstream.stop()
    assert upstream.connections == 1
    assert outcomes.of(router.breaker(entry.instance)) == ["neutral"]
    assert lines_starting(all_logs, "attempt_failed") == []
    _assert_no_content_logged(all_logs)


class Flood:
    """Answers with deltas as fast as a client takes them, far past any socket buffer."""

    name: Provider = "mock"
    deltas = 16_384
    piece = "x" * 65_536

    def __init__(self) -> None:
        self.exits: list[str] = []

    async def complete(self, request: ChatRequest) -> ProviderResult:
        raise AssertionError("a stream request must not call complete")

    async def stream(self, request: ChatRequest) -> AsyncIterator[Any]:
        how = "ran out"
        try:
            for _ in range(self.deltas):
                yield StreamDelta(text=self.piece)
                await asyncio.sleep(0)
        except GeneratorExit:
            how = "closed"
            raise
        except asyncio.CancelledError:
            how = "cancelled"
            raise
        finally:
            self.exits.append(how)


@pytest.mark.asyncio
async def test_a_real_client_that_stops_reading_is_dropped_by_the_real_server(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # The socket buffers fill, `send` stops returning, and the limit does the rest. The
    # limit is real time and short, which cannot flake: this client never reads, so a
    # send that blocks stays blocked, and a slow host only makes the limit fire later.
    outcomes = Outcomes(monkeypatch)
    adapter = Flood()
    entry = Entry("flood-instance", "flood-model")
    router = _real_router(adapter, entry, _policy(stream_idle_timeout=1.0))
    received = b""
    client: asyncio.StreamWriter | None = None
    try:
        with LineWaiter("llmgateway.router", "stream_abandoned") as abandoned:
            async with _serving(create_app(router=router)) as port:
                reader, client = await asyncio.open_connection("127.0.0.1", port)
                client.write(_post(port, stream_body()))
                await client.drain()
                async with asyncio.timeout(GUARD):
                    await _head_is_200(reader)
                    await abandoned.seen.wait()
                    # Now the server has dropped the stream: read what it left, to the end.
                    with suppress(ConnectionError):
                        while chunk := await reader.read(1 << 20):
                            received = received[-16:] + chunk[-16:]
    finally:
        if client is not None:
            client.close()
    assert adapter.exits == ["closed"]
    assert outcomes.of(router.breaker(entry.instance)) == ["neutral"]
    (line,) = lines_starting(all_logs, "stream_abandoned")
    assert line.endswith("reason=client_stalled")
    # A chunked body ends in a zero-length chunk. A dropped one never gets there.
    assert not received.endswith(b"0\r\n\r\n")
    assert Flood.piece[:64] not in all_logs.text
