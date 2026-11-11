"""Tests M12's mutation checks called for: claims of D-067 to D-073 no test could see.

Most of what is here needs an upstream that is not a generator. The protocol says only
`AsyncIterator` (D-067), and the router closes whatever it was given (`_close`). A
generator closes itself when it raises or runs out, so a router that forgets to close
one looks the same as one that remembers. `Steps` stays open until `aclose`, and counts
the calls, so "every path closes the upstream" (D-072) and "exactly once" can be read.

The rest are exact moments: the end's success when the client leaves during its own send
(D-071), the watcher and a backoff wait ending in the same turn (D-062, D-072), a cancel
from outside while opening (D-072), and what `send` itself may raise (D-072).

No test here waits on real time or calls a network.
"""

import asyncio
from collections.abc import MutableMapping, Sequence
from typing import Any

import pytest
from stream_support import (
    ANTHROPIC_KEY,
    CLAUDE_ENTRY,
    FIRST,
    HANG,
    SECOND,
    Done,
    Fail,
    FakeClock,
    Gateway,
    Hold,
    Interrupted,
    Outcomes,
    Park,
    ScriptedStream,
    Silence,
    Text,
    Upstream,
    Wire,
    abandoned_line,
    assert_not_chained,
    claude_block_start,
    claude_start,
    claude_text,
    drain,
    lines_starting,
    pass_time,
    provider_end,
    request_record,
    router_lines,
    sse_response,
    stream_body,
    taken_line,
    texts,
    unavailable,
    until,
)

from llmgateway.errors import ErrorBody
from llmgateway.providers.base import ProviderError, ProviderTimeout, ProviderUnavailable
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.schema import ChatRequest, Provider, StreamDelta

NEVER = 3600.0
RETRIES = {"max_attempts": 3, "base_delay": 0.25, "max_delay": 4.0}

Step = Text | Done | Fail | Silence


class Steps:
    """An upstream that is an `AsyncIterator` and not a generator.

    It never closes itself: only `aclose` does, and each call is counted. When the steps
    run out it stops with no end, as a generator that returns early would.
    """

    def __init__(self, steps: Sequence[Step], clock: FakeClock) -> None:
        self.steps = list(steps)
        self.clock = clock

    def __aiter__(self) -> "Steps":
        return self

    async def __anext__(self) -> Any:
        while self.steps:
            step = self.steps.pop(0)
            if isinstance(step, Text):
                return StreamDelta(text=step.text)
            if isinstance(step, Done):
                return provider_end(
                    step.finish_reason, step.input_tokens, step.output_tokens, step.response_id
                )
            if isinstance(step, Fail):
                raise step.error
            await pass_time(self.clock, step.seconds)
        raise StopAsyncIteration


class ClosableSteps(Steps):
    def __init__(self, steps: Sequence[Step], clock: FakeClock) -> None:
        super().__init__(steps, clock)
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1


class StepsAdapter:
    """A stream adapter whose `stream` returns `Steps`; each call plays the next script."""

    def __init__(self, *scripts: Sequence[Step], closable: bool = True) -> None:
        self.scripts = scripts
        self.closable = closable
        self.clock = FakeClock()
        self.upstreams: list[Steps] = []

    @property
    def name(self) -> Provider:
        return "mock"

    async def complete(self, request: ChatRequest) -> Any:
        raise AssertionError("a stream request must not call complete")

    def stream(self, request: ChatRequest) -> Any:
        script = self.scripts[min(len(self.upstreams), len(self.scripts) - 1)]
        upstream = (ClosableSteps if self.closable else Steps)(script, self.clock)
        self.upstreams.append(upstream)
        return upstream

    @property
    def closes(self) -> list[int]:
        return [getattr(upstream, "closes", 0) for upstream in self.upstreams]


def _gateway(adapter: StepsAdapter, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Gateway:
    gateway = Gateway(adapter, **kwargs)
    adapter.clock = gateway.clock
    gateway.virtual_time(monkeypatch)
    return gateway


# Every path after the first event closes the upstream, once (D-069, D-072)

AFTER_FIRST = [
    pytest.param([Text("one"), Fail(unavailable(503))], ProviderUnavailable, id="a_failure"),
    pytest.param([Text("one")], ProviderUnavailable, id="no_end"),
    pytest.param([Text("one"), Silence(NEVER)], ProviderTimeout, id="the_idle_limit"),
    pytest.param([Text("one"), Fail(RuntimeError("bug"))], RuntimeError, id="a_bug"),
    pytest.param([Text("one"), Fail(TimeoutError())], TimeoutError, id="an_adapters_timeout"),
    pytest.param([Text("one"), Fail(Interrupted())], Interrupted, id="a_base_exception"),
    pytest.param([Text("one"), Done()], None, id="the_end"),
    pytest.param([Done()], None, id="the_end_as_the_first_event"),
]


@pytest.mark.parametrize(("script", "raised"), AFTER_FIRST)
@pytest.mark.asyncio
async def test_iterating_to_the_last_event_closes_the_upstream_without_aclose(
    script: list[Step], raised: type[BaseException] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stream says it is over by raising or by its end; nothing else may be needed
    # for the upstream to close (D-069: "the upstream is closed").
    adapter = StepsAdapter(script)
    served = await _gateway(adapter, monkeypatch, idle=60.0).open()
    if raised is None:
        _, error = await drain(served)
        assert error is None
    else:
        with pytest.raises(raised):
            async for _ in served:
                pass
    assert adapter.closes == [1]


@pytest.mark.parametrize(("script", "raised"), AFTER_FIRST)
@pytest.mark.asyncio
async def test_aclose_after_the_last_event_does_not_close_the_upstream_again(
    script: list[Step], raised: type[BaseException] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = StepsAdapter(script)
    served = await _gateway(adapter, monkeypatch, idle=60.0).open()
    try:
        async for _ in served:
            pass
    except BaseException as error:
        assert raised is not None and isinstance(error, raised)
    await served.aclose()
    await served.aclose()
    assert adapter.closes == [1]


@pytest.mark.asyncio
async def test_aclose_partway_closes_the_upstream_once(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = StepsAdapter([Text("one"), Text("two"), Done()])
    served = await _gateway(adapter, monkeypatch).open()
    assert texts([await served.__anext__()]) == ["one"]
    await served.aclose()
    await served.aclose()
    assert adapter.closes == [1]


# Every path before the first event closes the upstream (D-068, D-072)

BEFORE_FIRST = [
    pytest.param([Fail(unavailable(503))], ProviderUnavailable, id="a_failure"),
    pytest.param([], ProviderUnavailable, id="no_end"),
    pytest.param([Silence(NEVER)], ProviderTimeout, id="the_attempt_limit"),
    pytest.param([Fail(RuntimeError("bug"))], RuntimeError, id="a_bug"),
    pytest.param([Fail(TimeoutError())], TimeoutError, id="an_adapters_timeout"),
    pytest.param([Fail(Interrupted())], Interrupted, id="a_base_exception"),
]


@pytest.mark.parametrize(("script", "raised"), BEFORE_FIRST)
@pytest.mark.asyncio
async def test_every_way_an_attempt_fails_closes_its_upstream(
    script: list[Step], raised: type[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = StepsAdapter(script)
    gateway = _gateway(adapter, monkeypatch)
    with pytest.raises(raised):
        await gateway.open()
    assert adapter.closes == [1]


@pytest.mark.asyncio
async def test_an_upstream_without_aclose_is_still_served_and_failed_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The protocol promises only AsyncIterator, so `aclose` is used when it exists.
    ok = StepsAdapter([Text("one"), Done()], closable=False)
    events, error = await drain(await _gateway(ok, monkeypatch).open())
    assert (texts(events), error) == (["one"], None)
    failing = StepsAdapter([Fail(unavailable(503))], closable=False)
    with pytest.raises(ProviderUnavailable):
        await _gateway(failing, monkeypatch).open()
    mid = StepsAdapter([Text("one"), Fail(unavailable(503))], closable=False)
    _, error = await drain(await _gateway(mid, monkeypatch).open())
    assert isinstance(error, ProviderUnavailable)


class _BrokenStream(ScriptedStream):
    def stream(self, request: ChatRequest) -> Any:
        raise RuntimeError("stream() did work when called")


@pytest.mark.asyncio
async def test_an_adapter_whose_stream_call_raises_propagates_that_bug_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # D-067 says calling `stream` does no work; one that breaks the rule is a bug, and it
    # must surface as itself, with the permit given back.
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(_BrokenStream())
    with pytest.raises(RuntimeError, match="did work"):
        await gateway.open()
    assert outcomes.of(gateway.breaker()) == ["neutral"]


# An adapter's own TimeoutError is a bug, on either side of the line (D-070)


@pytest.mark.asyncio
async def test_an_adapters_own_timeout_error_while_opening_is_a_bug_and_neutral(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # Only the router's own limit becomes `attempt_timeout`; this one propagates.
    outcomes = Outcomes(monkeypatch)
    adapter = StepsAdapter([Fail(TimeoutError())])
    gateway = _gateway(adapter, monkeypatch)
    with pytest.raises(TimeoutError) as caught:
        await gateway.open()
    assert not isinstance(caught.value, ProviderError)
    assert outcomes.of(gateway.breaker()) == ["neutral"]
    assert router_lines(all_logs) == []


@pytest.mark.asyncio
async def test_an_adapters_own_timeout_error_mid_stream_is_internal_error_and_no_line(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # Not `stream_idle`: the idle limit did not fire. A bug gets no router line, so no
    # `stream_failed` and no `stream_abandoned` (D-069), and one neutral outcome.
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text("one"), Fail(TimeoutError())]), idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert [event["type"] for event in wire.events] == ["delta", "error"]
    assert wire.events[-1]["code"] == "internal_error"
    assert router_lines(all_logs) == []
    record = request_record(all_logs)
    assert record.levelname == "ERROR"
    assert record.getMessage().endswith(" error=TimeoutError")
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_a_base_exception_mid_stream_writes_no_router_line(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # It escapes `except Exception`, but it is still a bug: no `stream_abandoned` line.
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(ScriptedStream([Text("one"), Fail(Interrupted())]))
    served = await gateway.open()
    with pytest.raises(Interrupted):
        async for _ in served:
            pass
    await served.aclose()
    assert router_lines(all_logs) == []
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_the_idle_failure_has_nothing_chained(monkeypatch: pytest.MonkeyPatch) -> None:
    # As for `attempt_timeout` (M9): the router's own failure carries no TimeoutError.
    gateway = Gateway(ScriptedStream([Text("one"), HANG]), idle=60.0)
    gateway.virtual_time(monkeypatch)
    _, error = await drain(await gateway.open())
    assert isinstance(error, ProviderTimeout)
    assert error.reason == "stream_idle"
    assert_not_chained(error)


# The line, and the lines


@pytest.mark.asyncio
async def test_a_delta_of_whitespace_is_a_first_event(monkeypatch: pytest.MonkeyPatch) -> None:
    # "A delta with non-empty text" (D-068): a space is text, so the stream is committed
    # and a later failure does not move to the next entry.
    first = ScriptedStream([Text(" "), Fail(unavailable(503))])
    second = ScriptedStream()
    gateway = Gateway(first, second)
    served = await gateway.open()
    assert served.route.provider == FIRST.instance
    events, error = await drain(served)
    assert texts(events) == [" "]
    assert isinstance(error, ProviderUnavailable)
    assert second.calls == 0


@pytest.mark.asyncio
async def test_a_stream_fallback_line_carries_the_request_id(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    gateway = Gateway(ScriptedStream([Fail(unavailable(503))]), ScriptedStream())
    await (await gateway.open(request_id="gw_contract")).aclose()
    assert lines_starting(all_logs, "fallback_taken") == [
        taken_line(FIRST, SECOND, "http_status", "ProviderUnavailable", request_id="gw_contract")
    ]


# The end's success is recorded before it is sent (D-071)


@pytest.mark.asyncio
async def test_the_end_is_a_success_even_if_the_client_leaves_during_its_send(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # Messages: 0 is the start, 1 the delta, 2 the end. The provider finished, so the
    # outcome is a success and the stream was not abandoned, whatever the client does.
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream([Text("one"), Done()])
    gateway = Gateway(adapter, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), plans={2: Park()})
    async with wire.running(gateway.app()):
        await wire.wait_for(wire.send_blocked.is_set, "the end's send to block")
        wire.disconnect()
        await wire.finished()
    assert outcomes.of(gateway.breaker()) == ["success"]
    assert lines_starting(all_logs, "stream_abandoned") == []
    assert adapter.open_generators == 0


# The watcher, while opening (D-062, D-072)


@pytest.mark.asyncio
async def test_the_client_check_after_a_wait_reads_the_watcher_in_the_same_turn(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    # The client leaves during the backoff wait, and the wait ends in the turn the
    # watcher notices. The cancel from the watcher lands a turn later; the D-062 check
    # comes first, so no second attempt is made and nothing more is billed.
    wire = Wire(stream_body())

    async def sleep(seconds: float) -> None:
        wire.disconnect()
        await asyncio.sleep(0)

    first = ScriptedStream([Fail(unavailable(503))])
    second = ScriptedStream()
    gateway = Gateway(first, second, retry=RETRIES, sleep=sleep)
    async with wire.running(gateway.app()):
        await wire.finished()
    assert (first.calls, second.calls) == (1, 0)
    assert len(lines_starting(all_logs, "attempt_failed")) == 1
    assert " status=499 " in request_record(all_logs).getMessage()


@pytest.mark.asyncio
async def test_a_cancel_from_outside_while_opening_closes_the_attempt_and_the_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Server shutdown cancels the app's task mid-attempt: the attempt in flight is
    # cancelled, its generator closed, the watcher stopped, and the outcome is neutral.
    outcomes = Outcomes(monkeypatch)
    hold = Hold()
    adapter = ScriptedStream([hold, Done()])
    gateway = Gateway(adapter)
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.wait_for(hold.reached.is_set, "the attempt to be in flight")
        assert wire.task is not None
        wire.task.cancel()
        await until(wire.task.done, "the app to stop")
    assert adapter.exits == ["cancelled"]
    assert wire.receives_waiting == 0
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.asyncio
async def test_the_watcher_ends_when_an_adapter_raises_cancelled_error_while_opening() -> None:
    # The watcher runs "until the response ends" (D-072), however the opening ended.
    gateway = Gateway(ScriptedStream([Fail(asyncio.CancelledError())]))
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert wire.receives_waiting == 0


@pytest.mark.asyncio
async def test_a_client_gone_while_opening_gets_the_499_body_of_d_062() -> None:
    hold = Hold()
    gateway = Gateway(ScriptedStream([hold, Done()]))
    wire = Wire(stream_body())
    async with wire.running(gateway.app()):
        await wire.wait_for(hold.reached.is_set, "the attempt to be in flight")
        wire.disconnect()
        await wire.finished()
    (start,) = [m for m in wire.sent_after_gone if m["type"] == "http.response.start"]
    assert start["status"] == 499
    body = b"".join(m.get("body", b"") for m in wire.sent_after_gone)
    error = ErrorBody.model_validate_json(body).error
    assert (error.code, error.message) == ("client_disconnected", "The client disconnected.")


# What send itself may raise (D-072)


class SendRaises(Wire):
    """A connection whose `send` raises on one message, as a dead socket's can."""

    def __init__(self, error: BaseException, at: int) -> None:
        super().__init__(stream_body())
        self.error = error
        self.at = at
        self.attempts = 0

    async def send(self, message: MutableMapping[str, Any]) -> None:
        index = self.attempts
        self.attempts += 1
        if index == self.at:
            raise self.error
        await super().send(message)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ConnectionResetError(), id="connection_reset"),
        pytest.param(BrokenPipeError(), id="broken_pipe"),
        pytest.param(TimeoutError(), id="timeout_error_is_an_oserror"),
    ],
)
@pytest.mark.parametrize("at", [0, 1], ids=["the_start", "a_delta"])
@pytest.mark.asyncio
async def test_an_oserror_from_send_is_the_client_leaving(
    error: OSError, at: int, monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # "An OSError from send means the client has gone" (D-072): handled, not raised, and
    # closed as client_gone. TimeoutError is an OSError, and only the gateway's own limit
    # is a stall.
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream([Text("one"), Text("two"), Done()])
    gateway = Gateway(adapter, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = SendRaises(error, at)
    async with wire.running(gateway.app()):
        await wire.finished()
    request_id = request_record(all_logs).getMessage().split()[0].removeprefix("request_id=")
    assert lines_starting(all_logs, "stream_abandoned") == [
        abandoned_line(FIRST, "client_gone", request_id=request_id)
    ]
    assert adapter.open_generators == 0
    assert outcomes.of(gateway.breaker()) == ["neutral"]


@pytest.mark.parametrize("at", [0, 1], ids=["the_start", "a_delta"])
@pytest.mark.asyncio
async def test_any_other_exception_from_send_closes_the_upstream_and_propagates(
    at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = Outcomes(monkeypatch)
    adapter = ScriptedStream([Text("one"), Text("two"), Done()])
    gateway = Gateway(adapter, idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = SendRaises(RuntimeError("send broke"), at)
    async with wire.running(gateway.app()):
        with pytest.raises(RuntimeError, match="send broke"):
            await wire.finished()
    assert adapter.open_generators == 0
    assert outcomes.of(gateway.breaker()) == ["neutral"]


# A real adapter's key, when the client leaves mid-stream (D-072, the leak rule)


@pytest.mark.asyncio
async def test_a_client_gone_from_a_real_claude_stream_puts_the_key_in_no_record(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    # In process, with the key in the adapter's headers: the real-socket test was the only
    # one that could see a key written beside `stream_abandoned`.
    stall = asyncio.Event()

    async def wait_forever() -> None:
        await stall.wait()

    upstream = Upstream(
        lambda _: sse_response(
            claude_start(), claude_block_start(), claude_text("one"), wait_forever
        )
    )
    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=upstream.transport)
    try:
        gateway = Gateway(adapter, entries=(CLAUDE_ENTRY,), idle=NEVER)
        wire = Wire(stream_body(), plans={2: Park()})
        async with wire.running(gateway.app()):
            await wire.wait_for(lambda: wire.event_count >= 1, "the first delta")
            wire.disconnect()
            await wire.finished()
    finally:
        await adapter.aclose()
    assert len(lines_starting(all_logs, "stream_abandoned")) == 1
    written = all_logs.text + "".join(str(vars(record)) for record in all_logs.records)
    assert ANTHROPIC_KEY not in written
    assert upstream.open_bodies == 0
