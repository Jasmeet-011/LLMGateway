"""Tests for a stream's time limits after the first event (M12, D-070).

Before the first event D-050 and D-061 apply unchanged; `test_stream_open.py` pins that.
After it the attempt limit and the 90 s deadline stop applying, and one thing limits a
stream: `stream_idle_timeout`, the longest wait on the adapter for the next event. What
needs catching is silence, not length, so a long answer that keeps arriving must never be
cut. And the limit must run only while the router waits on the adapter: time a slow
client costs is not the provider's fault. `test_stream_disconnect.py` has the client's
side of that line, since it takes a `send` that blocks.

Claude's `ping`s are why the router needs a limit of its own: they reset httpx's read
timer without producing an event. So one test here runs a real `ClaudeAdapter` against a
body that pings for ever.

Time is the fake clock, with the event loop pointed at it (see `stream_support`), so
every limit fires on its exact second and a broken limit fails instead of hanging.
Names M12 adds are reached at call time, so this file collects and each test fails on
its own.
"""

import math
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from stream_support import (
    ANTHROPIC_KEY,
    CLAUDE_ENTRY,
    CLAUDE_PING,
    CLOCK_START,
    FIRST,
    GEMINI_ENTRY,
    GEMINI_KEY,
    HANG,
    HANG_LIMIT,
    SECOND,
    TICK,
    Body,
    Done,
    Fail,
    FakeClock,
    Gateway,
    Outcomes,
    ScriptedStream,
    Silence,
    Text,
    Upstream,
    Wire,
    at_about,
    claude_start,
    claude_text,
    drain,
    event_texts,
    event_types,
    failed_line,
    gemini_text,
    gemini_thought,
    lines_starting,
    pass_time,
    request_line,
    router_lines,
    stream_body,
    texts,
    timeout_error,
)

from llmgateway.providers.base import ProviderTimeout
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter
from llmgateway.router import RetryPolicy

# RetryPolicy (D-070): validated like `attempt_timeout`


def _policy(**arguments: Any) -> RetryPolicy:
    return RetryPolicy(**arguments)


def _idle_of(policy: Any) -> Any:
    return policy.stream_idle_timeout


def test_the_idle_limit_defaults_to_60_seconds() -> None:
    assert _idle_of(RetryPolicy()) == 60.0


@pytest.mark.parametrize("value", [1e-9, 0.5, 60, 60.0, 3600.0])
def test_any_finite_number_above_zero_is_a_valid_idle_limit(value: float) -> None:
    assert _idle_of(_policy(stream_idle_timeout=value)) == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="zero"),
        pytest.param(0.0, id="zero_float"),
        pytest.param(-1.0, id="negative"),
        pytest.param(math.inf, id="infinite"),
        pytest.param(-math.inf, id="minus_infinite"),
        pytest.param(math.nan, id="nan"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param("60", id="a_string"),
        pytest.param(None, id="none"),
    ],
)
def test_a_bad_idle_limit_raises_value_error(value: Any) -> None:
    with pytest.raises(ValueError):
        _policy(stream_idle_timeout=value)


# The limit (D-070)


@pytest.mark.asyncio
async def test_silence_after_the_first_event_fails_with_stream_idle_at_60_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ScriptedStream([Text("one"), HANG, Text("late"), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    events, failure = await drain(await gateway.open())
    assert texts(events) == ["one"]
    assert type(failure) is ProviderTimeout
    assert (failure.status, failure.reason) == (None, "stream_idle")
    assert at_about(gateway.clock(), CLOCK_START + 60.0)
    assert adapter.exits == ["cancelled"]


@pytest.mark.asyncio
async def test_the_idle_limit_is_the_policys_value(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = ScriptedStream([Text("one"), HANG, Text("late"), Done()])
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    _, failure = await drain(await gateway.open())
    assert failure is not None
    assert failure.reason == "stream_idle"
    assert at_about(gateway.clock(), CLOCK_START + 10.0)


@pytest.mark.parametrize("seconds", [60.0 - TICK, 60.0 - 2 * TICK])
@pytest.mark.asyncio
async def test_a_gap_just_under_the_limit_is_not_silence(
    seconds: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ScriptedStream([Text("one"), Silence(seconds), Text("two"), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    events, failure = await drain(await gateway.open())
    assert failure is None
    assert texts(events) == ["one", "two"]


@pytest.mark.asyncio
async def test_each_wait_has_its_own_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    gap = Silence(50.0)
    adapter = ScriptedStream([Text("a"), gap, Text("b"), gap, Text("c"), gap, Text("d"), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    events, failure = await drain(await gateway.open())
    assert failure is None
    assert texts(events) == ["a", "b", "c", "d"]


@pytest.mark.asyncio
async def test_the_wait_for_the_end_event_has_the_limit_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ScriptedStream([Text("one"), HANG, Done()])
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    _, failure = await drain(await gateway.open())
    assert failure is not None
    assert failure.reason == "stream_idle"


@pytest.mark.asyncio
async def test_a_stream_may_outlive_the_attempt_limit_and_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 232 fake seconds in all, against 60 and 90.
    adapter = ScriptedStream(
        [
            Silence(55.0),
            Text("a"),
            Silence(59.0),
            Text("b"),
            Silence(59.0),
            Text("c"),
            Silence(59.0),
            Done(),
        ]
    )
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    events, failure = await drain(await gateway.open())
    assert failure is None
    assert texts(events) == ["a", "b", "c"]
    assert gateway.clock() - CLOCK_START >= 232.0


@pytest.mark.asyncio
async def test_a_stream_opened_late_in_the_deadline_is_not_cut_by_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ScriptedStream([HANG, Text("late"), Done()])
    second = ScriptedStream([Silence(29.0), Text("a"), Silence(50.0), Text("b"), Done()])
    gateway = Gateway(first, second)
    gateway.virtual_time(monkeypatch)
    events, failure = await drain(await gateway.open())
    assert failure is None
    assert texts(events) == ["a", "b"]
    assert gateway.clock() - CLOCK_START > 90.0


@pytest.mark.asyncio
async def test_a_long_answer_has_no_total_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    script: list[Any] = []
    for index in range(100):
        script += [Text(str(index)), Silence(30.0)]
    adapter = ScriptedStream([*script, Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    events, failure = await drain(await gateway.open())
    assert failure is None
    assert len(texts(events)) == 100
    assert gateway.clock() - CLOCK_START >= 3000.0


@pytest.mark.asyncio
async def test_time_the_consumer_takes_between_pulls_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The limit runs only while the router waits on the adapter, so the longest pause
    # here, between one pull and the next, costs nothing.
    adapter = ScriptedStream([Text("one"), Text("two"), Silence(9.0), Text("three"), Done()])
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    served = await gateway.open()
    seen: list[Any] = []
    async for event in served:
        seen.append(event)
        await pass_time(gateway.clock, 100.0)
    assert texts(seen) == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_empty_deltas_do_not_reset_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty delta is not an event (D-068), and the limit is on the wait for an event.
    gap = (Silence(4.0), Text(""))
    adapter = ScriptedStream([Text("one"), *gap * 10, Text("late"), Done()])
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    events, failure = await drain(await gateway.open())
    assert texts(events) == ["one"]
    assert failure is not None
    assert failure.reason == "stream_idle"
    assert at_about(gateway.clock(), CLOCK_START + 10.0)


# What the idle limit does (D-069, D-071)


@pytest.mark.asyncio
async def test_the_idle_limit_ends_the_stream_as_a_failure_does(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    outcomes = Outcomes(monkeypatch)
    first = ScriptedStream([Text("one"), HANG, Text("late"), Done()])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry={"max_attempts": 3, "base_delay": 0.25})
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), clock=gateway.clock)
    async with wire.running(gateway.app()):
        await wire.finished()
    assert wire.status == 200
    assert wire.ended
    events = wire.events
    assert event_types(events) == ["delta", "error"]
    request_id = wire.headers["x-request-id"]
    assert events[-1] == {
        "type": "error",
        "code": "provider_timeout",
        "message": "The provider did not respond in time.",
        "request_id": request_id,
    }
    assert (first.calls, second.calls) == (1, 0)
    assert first.open_generators == 0
    assert gateway.sleep.calls == []
    assert router_lines(all_logs) == [
        failed_line(FIRST, "-", "stream_idle", "ProviderTimeout", request_id=request_id)
    ]
    assert " status=200 " in request_line(all_logs)
    assert outcomes.of(gateway.breaker()) == ["failure"]


@pytest.mark.asyncio
async def test_an_adapters_own_read_timeout_keeps_its_own_reason(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # D-070: an httpx read timeout is `timeout`, before or after the line.
    gateway = Gateway(ScriptedStream([Text("one"), Fail(timeout_error())]))
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert wire.events[-1]["code"] == "provider_timeout"
    assert lines_starting(all_logs, "stream_failed") == [
        failed_line(
            FIRST, "-", "timeout", "ProviderTimeout", request_id=wire.headers["x-request-id"]
        )
    ]


@pytest.mark.asyncio
async def test_a_stream_is_not_cut_when_the_idle_limit_is_longer_than_the_attempt_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ScriptedStream([Text("one"), Silence(70.0), Text("two"), Done()])
    gateway = Gateway(adapter, idle=80.0)
    gateway.virtual_time(monkeypatch)
    events, failure = await drain(await gateway.open())
    assert failure is None
    assert texts(events) == ["one", "two"]


# Pings (D-070): real adapters, whose keep-alives reset httpx's timer but make no event


def _ticking(clock: FakeClock, frame: bytes) -> AsyncIterator[bytes]:
    """A body sending `frame` every tick of fake time, up to HANG_LIMIT."""

    async def body() -> AsyncIterator[bytes]:
        for _ in range(round(HANG_LIMIT / TICK)):
            await pass_time(clock, TICK)
            yield frame

    return body()


class Ticking(Body):
    """Starts like any `Body`, then repeats a frame for as long as it is read."""

    def __init__(self, clock: FakeClock, frame: bytes, *lead: bytes) -> None:
        super().__init__(*lead)
        self.clock = clock
        self.frame = frame

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for piece in super().__aiter__():
            yield piece
        async for piece in _ticking(self.clock, self.frame):
            yield piece


@pytest.mark.asyncio
async def test_claude_pings_do_not_reset_the_routers_limit(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    clock_holder: dict[str, FakeClock] = {}
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            stream=Ticking(clock_holder["clock"], CLAUDE_PING, claude_start(), claude_text("one")),
        )
    )
    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=upstream.transport)
    gateway = Gateway(adapter, entries=(CLAUDE_ENTRY, SECOND), idle=10.0)
    clock_holder["clock"] = gateway.clock
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), clock=gateway.clock)
    async with wire.running(gateway.app()):
        await wire.finished()
    assert event_types(wire.events) == ["delta", "error"]
    assert event_texts(wire.events) == "one"
    assert wire.events[-1]["code"] == "provider_timeout"
    assert at_about(gateway.clock(), CLOCK_START + 10.0)
    assert upstream.open_bodies == 0
    assert lines_starting(all_logs, "stream_failed") == [
        failed_line(
            CLAUDE_ENTRY,
            "-",
            "stream_idle",
            "ProviderTimeout",
            request_id=wire.headers["x-request-id"],
        )
    ]


@pytest.mark.asyncio
async def test_gemini_thoughts_do_not_reset_the_routers_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_holder: dict[str, FakeClock] = {}
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            stream=Ticking(clock_holder["clock"], gemini_thought(), gemini_text("one")),
        )
    )
    adapter = GeminiAdapter(GEMINI_KEY, transport=upstream.transport)
    gateway = Gateway(adapter, entries=(GEMINI_ENTRY, SECOND), idle=10.0)
    clock_holder["clock"] = gateway.clock
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), clock=gateway.clock)
    async with wire.running(gateway.app()):
        await wire.finished()
    assert event_types(wire.events) == ["delta", "error"]
    assert wire.events[-1]["code"] == "provider_timeout"
    assert upstream.open_bodies == 0
