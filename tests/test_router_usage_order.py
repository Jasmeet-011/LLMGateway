"""Order of the usage report against the breaker outcome and the upstream's close (M15, D-095).

Added by the mutation checks, for mutants the first tests did not stop. The router reports an
attempt "just after its breaker outcome" (FLOW.md section 5): in every path, before anything the
router awaits afterwards, in particular before it closes the upstream. A close can take as long
as the provider's connection takes to drop, or be cancelled; the record must not wait for it.

- the paths of `_attempt` and `_open_attempt` that were not yet pinned against the breaker: a
  `TimeoutError` that is not the router's own, a bug, and a cancellation while opening, each
  neutral for the breaker and then reported;
- an upstream that is NOT a generator, whose `aclose` waits until released (Lesson 10): at the
  moment the router is inside that close, the attempt has already been reported, for a failure
  before the first event, a cancellation while opening, and a failure after the first event.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from stream_support import Fail, Gateway, Hold, Outcomes, Text, chat_request, unavailable, until
from usage_support import Answer, Meters, UsageAdapter

from llmgateway.providers.base import ProviderError, ProviderUnavailable


class StubbornStream:
    """An async iterator that is not a generator. `aclose` waits until `release` is set."""

    def __init__(self, steps: list[Any]) -> None:
        self.steps = list(steps)
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.parked = asyncio.Event()

    def __aiter__(self) -> "StubbornStream":
        return self

    async def __anext__(self) -> Any:
        if not self.steps:
            self.parked.set()
            await asyncio.Event().wait()
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    async def aclose(self) -> None:
        self.closing.set()
        await self.release.wait()


class StubbornAdapter:
    name = "mock"

    def __init__(self, upstream: StubbornStream) -> None:
        self.upstream = upstream

    async def complete(self, request: Any) -> Any:
        raise AssertionError("a stream request must not call complete")

    def stream(self, request: Any) -> AsyncIterator[Any]:
        return self.upstream


# Against the breaker, on the paths not yet pinned


@pytest.mark.asyncio
async def test_a_timeout_error_that_is_not_the_routers_is_neutral_then_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(UsageAdapter(results=[Fail(TimeoutError("adapter's own"))]))
    breaker = gateway.breaker()

    def meter(_: Any) -> None:
        outcomes.calls.append((breaker, "meter"))

    with pytest.raises(TimeoutError):
        await gateway.router.serve(chat_request(), meter=meter)
    assert outcomes.of(breaker) == ["neutral", "meter"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(TimeoutError("adapter's own"), id="timeout_error_that_is_not_the_routers"),
        pytest.param(RuntimeError("bug"), id="bug"),
    ],
)
async def test_a_stream_that_stops_on_a_bug_while_opening_is_neutral_then_reported(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    outcomes = Outcomes(monkeypatch)
    gateway = Gateway(UsageAdapter([Fail(error)]))
    breaker = gateway.breaker()

    def meter(_: Any) -> None:
        outcomes.calls.append((breaker, "meter"))

    with pytest.raises(type(error)):
        await gateway.router.open_stream(chat_request(), meter=meter)
    assert outcomes.of(breaker) == ["neutral", "meter"]


@pytest.mark.asyncio
async def test_a_cancellation_while_opening_is_neutral_then_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = Outcomes(monkeypatch)
    hold = Hold()
    gateway = Gateway(UsageAdapter([hold]))
    breaker = gateway.breaker()

    def meter(_: Any) -> None:
        outcomes.calls.append((breaker, "meter"))

    task = asyncio.ensure_future(gateway.router.open_stream(chat_request(), meter=meter))
    await until(hold.reached.is_set, "the adapter to be called", task=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert outcomes.of(breaker) == ["neutral", "meter"]


# Against the upstream's close


async def _while_closing(task: "asyncio.Future[Any]", upstream: StubbornStream) -> None:
    await until(upstream.closing.is_set, "the router to close the upstream", task=task)


@pytest.mark.asyncio
async def test_a_failure_before_the_first_event_is_reported_before_the_upstream_is_closed() -> None:
    upstream = StubbornStream([unavailable()])
    gateway = Gateway(StubbornAdapter(upstream))
    meters = Meters()
    task = asyncio.ensure_future(gateway.router.open_stream(chat_request(), meter=meters))
    try:
        await _while_closing(task, upstream)
        assert [(c.outcome, c.reason) for c in meters.calls] == [("failed", "http_status")]
    finally:
        upstream.release.set()
        with contextlib.suppress(ProviderError):
            await task


@pytest.mark.asyncio
async def test_a_cancellation_while_opening_is_reported_before_the_upstream_is_closed() -> None:
    upstream = StubbornStream([])
    gateway = Gateway(StubbornAdapter(upstream))
    meters = Meters()
    task = asyncio.ensure_future(gateway.router.open_stream(chat_request(), meter=meters))
    try:
        await until(upstream.parked.is_set, "the router to wait for the first event", task=task)
        task.cancel()
        await _while_closing(task, upstream)
        assert [(c.outcome, c.reason) for c in meters.calls] == [("abandoned", "cancelled")]
    finally:
        upstream.release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_failure_after_the_first_event_is_reported_before_the_upstream_is_closed() -> None:
    from llmgateway.schema import StreamDelta

    upstream = StubbornStream([StreamDelta(text="a"), unavailable()])
    gateway = Gateway(StubbornAdapter(upstream))
    meters = Meters()
    served = await gateway.router.open_stream(chat_request(), meter=meters)

    async def read_all() -> None:
        async for _ in served:
            pass

    task = asyncio.ensure_future(read_all())
    try:
        await _while_closing(task, upstream)
        assert [(c.outcome, c.reason) for c in meters.calls] == [("failed", "http_status")]
    finally:
        upstream.release.set()
        with pytest.raises(ProviderUnavailable):
            await task


# With no meter, nothing is said at all


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run",
    ["serve_ok", "serve_failure", "stream_ok", "stream_failure"],
)
async def test_a_call_with_no_meter_writes_no_usage_report_line_and_no_error(
    caplog: pytest.LogCaptureFixture, run: str
) -> None:
    if run.startswith("serve"):
        step = Answer() if run == "serve_ok" else Fail(unavailable())
        gateway = Gateway(UsageAdapter(results=[step]))
        with contextlib.suppress(ProviderError):
            await gateway.router.serve(chat_request())
    else:
        script = [Text("a")] if run == "stream_ok" else [Fail(unavailable())]
        gateway = Gateway(UsageAdapter(script))
        with contextlib.suppress(ProviderError):
            served = await gateway.router.open_stream(chat_request())
            await served.aclose()
    assert [r for r in caplog.records if "usage_report_failed" in r.getMessage()] == []
    router_errors = [
        r for r in caplog.records if r.name == "llmgateway.router" and r.levelname == "ERROR"
    ]
    assert router_errors == []
