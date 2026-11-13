"""Fakes and helpers for the usage tests (M15, D-095 to D-100). Not a test module.

Everything M15 adds is reached at call time, as `stream_support` and `auth_support` do:
`llmgateway.usage`, `llmgateway.prices`, `llmgateway.usagestore` and
`providers.base.ProviderUsage` are imported inside functions. A module-level import of code
that does not exist before M15 would turn every test file that needs a helper into a
collection error. This way each test fails on its own, for its own reason, on the commit that
is run red. The names under `TYPE_CHECKING` give mypy the real types.

- `FakeUsageStore`: the `UsageStore` protocol in memory. It records every `write` call (the
  batches it was asked to write and those that returned), can raise, can hang until released,
  counts the cancels and `aclose` calls, and keeps the order of its calls.
- `PRICES_TEXT` and `make_prices()`: a small price table that the recorder and app tests share
  (haiku 100 and 500 nano-dollars per token, a Gemini model whose price changes on a date).
- `Meters`: a `Meter` that records every `AttemptUsage`, and can raise after recording.
- `UsageAdapter`: a scripted adapter for the router tests. `stream` plays the steps of
  `stream_support`'s `ScriptedStream` plus `Report`, which yields a `ProviderUsage`;
  `complete` plays `Answer`, `Fail`, `Silence` and `Hold`.
- `attempt()`, `make_record()`: the expected `AttemptUsage` and a ready `UsageRecord`.
- `virtual_loop_time()`: points the event loop at a `FakeClock`, so a write or drain limit fires
  on its exact fake second, and `pass_time` is what moves it.
- `usage_lines()`: what the recorder wrote to the `llmgateway.usage` logger.
"""

import asyncio
import importlib
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from auth_support import TEST_KEY_ID, TEST_TENANT
from stream_support import (
    CLAUDE_ENTRY,
    FIRST,
    SECOND,
    WALL_START,
    Done,
    Entry,
    Fail,
    FakeClock,
    Hold,
    ScriptedStream,
    Silence,
    Text,
    pass_time,
    provider_end,
)

from llmgateway.auth import Principal
from llmgateway.providers.base import ProviderResult
from llmgateway.schema import ChatRequest, FinishReason, Provider, StreamDelta, Usage

if TYPE_CHECKING:
    from llmgateway.prices import PriceTable
    from llmgateway.usage import AttemptUsage, Outcome, UsageRecord, UsageRecorder

__all__ = [
    "CLAUDE_ENTRY",
    "FIRST",
    "SECOND",
    "WALL_START",
    "Answer",
    "Entry",
    "FakeUsageStore",
    "Meters",
    "Report",
    "UsageAdapter",
]

# A moment on which every price in `PRICES_TEXT` is in effect (2026-11-12 12:00 UTC).
PRICED_NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC).timestamp()

REQUEST_ID = "gw_usage_test_request"
ALIAS = "fast"
PRINCIPAL = Principal(key_id=TEST_KEY_ID, tenant_id=TEST_TENANT)

# A planted value for every kind of text the usage lines must never hold (Lesson 9).
MESSAGE_MARKER = "usage-exception-message-marker"
DSN_MARKER = "usage-dsn-password-marker"
TENANT_MARKER = "usage-tenant-marker"
REQUEST_MARKER = "usage-request-id-marker"
ALIAS_MARKER = "usage-alias-marker"

PRICES_TEXT = """\
[[claude."claude-haiku-5-5"]]
from = 2026-11-11
input = "0.10"
output = "0.50"
long_prompt_over = 100000
long_input = "0.50"
long_output = "2.50"
source = "https://example.test/claude"
checked = 2026-11-11

[[gemini."gemini-3.6-flash"]]
from = 2026-11-11
input = "0.75"
output = "3.75"
source = "https://example.test/gemini"
checked = 2026-11-11

[[gemini."gemini-3.6-flash"]]
from = 2027-01-01
input = "1.50"
output = "7.50"
source = "https://example.test/gemini"
checked = 2026-11-11
"""


def usage_module() -> Any:
    return importlib.import_module("llmgateway.usage")


def prices_module() -> Any:
    return importlib.import_module("llmgateway.prices")


def usagestore_module() -> Any:
    return importlib.import_module("llmgateway.usagestore")


def base_module() -> Any:
    return importlib.import_module("llmgateway.providers.base")


def provider_usage(input_tokens: int, output_tokens: int) -> Any:
    return base_module().ProviderUsage(
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens)
    )


def is_provider_usage(event: Any) -> bool:
    return isinstance(event, base_module().ProviderUsage)


def make_prices() -> "PriceTable":
    table: PriceTable = prices_module().parse_prices(PRICES_TEXT)
    return table


def make_recorder(
    store: "FakeUsageStore | None" = None, **kwargs: Any
) -> "tuple[UsageRecorder, FakeUsageStore]":
    held = store if store is not None else FakeUsageStore()
    # A fixed clock on a priced day, unless the test brings its own.
    kwargs.setdefault("wall_clock", lambda: PRICED_NOW)
    recorder: UsageRecorder = usage_module().UsageRecorder(held, make_prices(), **kwargs)
    return recorder, held


def make_record(
    n: int = 1,
    *,
    request_id: str | None = None,
    outcome: "Outcome" = "success",
    stream: bool = False,
    reason: str | None = None,
    provider_status: int | None = None,
    partial: bool = False,
    counts: tuple[int, int] | None = (3, 4),
    cost: int | None = 5,
    provider_type: str = "mock",
    model: str = "mock-1",
    alias: str = ALIAS,
    tenant_id: str = TEST_TENANT,
    delivered_chars: int | None = None,
    estimated_output_tokens: int | None = None,
    cache_hit: bool = False,
) -> "UsageRecord":
    """A `UsageRecord` whose request id is `n` unless given: cheap to make a batch of."""
    record: UsageRecord = usage_module().UsageRecord(
        recorded_at=datetime.fromtimestamp(WALL_START + n, UTC),
        request_id=request_id if request_id is not None else f"gw_{n}",
        tenant_id=tenant_id,
        key_id=TEST_KEY_ID,
        alias=alias,
        provider_instance="mock",
        provider_type=provider_type,
        model=model,
        stream=stream,
        attempt=1,
        outcome=outcome,
        reason=reason,
        provider_status=provider_status,
        partial=partial,
        input_tokens=None if counts is None else counts[0],
        output_tokens=None if counts is None else counts[1],
        cost_nanousd=cost,
        delivered_chars=delivered_chars,
        estimated_output_tokens=estimated_output_tokens,
        cache_hit=cache_hit,
    )
    return record


class _Unset:
    """The marker for "the test did not say": see `attempt`."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


def expected_estimate(provider_type: str, chars: int) -> int:
    """D-101's estimate, integer arithmetic: claude ceil(chars * 13 / 35), else ceil(chars / 4)."""
    if provider_type == "claude":
        return -(-(chars * 13) // 35)
    return -(-chars // 4)


def attempt(
    *,
    instance: str = FIRST.instance,
    provider_type: Provider = "mock",
    model: str = FIRST.model,
    stream: bool = False,
    number: int = 1,
    outcome: "Outcome" = "success",
    reason: str | None = None,
    provider_status: int | None = None,
    partial: bool = False,
    usage: tuple[int, int] | None = (1, 2),
    delivered_chars: "int | _Unset | None" = UNSET,
    cache_hit: bool = False,
) -> "AttemptUsage":
    """The `AttemptUsage` a router call should report. `usage` is (input, output) or None.

    `delivered_chars` is None for a non-streaming attempt and for a stream success (D-101), so
    that is what it is when a test does not say. For a stream that failed or was abandoned the
    router reports an int, and a test must say which: leaving it out is an error here, so no
    expectation about a cut stream can be written without its exact count.
    """
    if isinstance(delivered_chars, _Unset):
        assert not (stream and outcome != "success"), (
            "a stream attempt that failed or was abandoned reports an int delivered_chars (D-101): "
            "the test must say which"
        )
        delivered_chars = None
    value = None if usage is None else Usage(input_tokens=usage[0], output_tokens=usage[1])
    # `cache_hit` is passed only when true: D-113 gives `AttemptUsage` a default of false, so every
    # attempt a router reports without one is built exactly as before, and the default is pinned
    # by its own test (`test_usage_cache_hit.py`).
    extra: dict[str, Any] = {"cache_hit": True} if cache_hit else {}
    report: AttemptUsage = usage_module().AttemptUsage(
        provider_instance=instance,
        provider_type=provider_type,
        model=model,
        stream=stream,
        attempt=number,
        outcome=outcome,
        reason=reason,
        provider_status=provider_status,
        partial=partial,
        usage=value,
        delivered_chars=delivered_chars,
        **extra,
    )
    return report


# The store and the meters


class FakeUsageStore:
    """A `UsageStore` in memory.

    `write` records its batch in `attempted`, then raises `error` if one is set, then waits for
    `release()` if `hang()` was called, and only then adds the batch to `batches`. A cancel while
    it waits is counted. `log` keeps the order of `write` and `close` calls.
    """

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.attempted: list[list[UsageRecord]] = []
        self.batches: list[list[UsageRecord]] = []
        self.closed = 0
        self.cancelled = 0
        self.error = error
        self.gate: asyncio.Event | None = None
        self.log: list[str] = []

    def hang(self) -> None:
        self.gate = asyncio.Event()

    def release(self) -> None:
        assert self.gate is not None
        self.gate.set()
        self.gate = None

    @property
    def records(self) -> "list[UsageRecord]":
        return [record for batch in self.batches for record in batch]

    async def write(self, records: "Sequence[UsageRecord]") -> None:
        batch = list(records)
        self.attempted.append(batch)
        self.log.append("write")
        try:
            if self.error is not None:
                raise self.error
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.batches.append(batch)

    async def aclose(self) -> None:
        self.closed += 1
        self.log.append("close")


class Meters:
    """A `Meter` that keeps every `AttemptUsage`, then raises `error` if one is set."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[AttemptUsage] = []
        self.error = error

    def __call__(self, report: "AttemptUsage") -> None:
        self.calls.append(report)
        if self.error is not None:
            raise self.error


# Adapters


@dataclass(frozen=True)
class Answer:
    """`complete` returns this result."""

    content: str = "answer"
    input_tokens: int = 1
    output_tokens: int = 2
    finish_reason: FinishReason = "stop"


@dataclass(frozen=True)
class Report:
    """A stream yields `ProviderUsage` with these counts (D-097)."""

    input_tokens: int
    output_tokens: int


UStep = Text | Done | Fail | Silence | Hold | Report
UScript = Sequence[UStep]
CompleteStep = Answer | Fail | Silence | Hold


class UsageAdapter(ScriptedStream):
    """A scripted adapter on both halves of the protocol, for the router's meter tests.

    `stream` call n plays script n (the last repeats), like `ScriptedStream`, and a step may be
    a `Report`. `complete` call n plays `results[n]` (the last repeats): an `Answer` returns, a
    `Fail` raises, a `Silence` lets fake time pass and then answers, a `Hold` parks until
    released and then answers.
    """

    def __init__(
        self,
        *scripts: UScript,
        results: Sequence[CompleteStep] = (),
        provider_type: Provider = "mock",
    ) -> None:
        super().__init__(provider_type=provider_type)
        self.plans: tuple[UScript, ...] = scripts or ((Text("Hello"), Done()),)
        self.results: Sequence[CompleteStep] = results or (Answer(),)
        self.complete_requests: list[ChatRequest] = []

    @property
    def complete_calls(self) -> int:
        return len(self.complete_requests)

    async def complete(self, request: ChatRequest) -> ProviderResult:
        index = len(self.complete_requests)
        self.complete_requests.append(request)
        step = self.results[min(index, len(self.results) - 1)]
        if isinstance(step, Fail):
            raise step.error
        if isinstance(step, Silence):
            await pass_time(self.clock, step.seconds)
            step = Answer()
        elif isinstance(step, Hold):
            step.reached.set()
            await step.release.wait()
            step = Answer()
        assert isinstance(step, Answer)
        return ProviderResult(
            content=step.content,
            finish_reason=step.finish_reason,
            usage=Usage(input_tokens=step.input_tokens, output_tokens=step.output_tokens),
            provider_response_id="fake-complete-1",
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[Any]:
        index = len(self.requests)
        self.requests.append(request)
        self.started.append(self.clock())
        self.order.append(f"call {self.label}")
        script = self.plans[min(index, len(self.plans) - 1)]
        how = "ran out"
        try:
            for step in script:
                if isinstance(step, Text):
                    yield StreamDelta(text=step.text)
                elif isinstance(step, Report):
                    yield provider_usage(step.input_tokens, step.output_tokens)
                elif isinstance(step, Done):
                    how = "ended"
                    yield provider_end(
                        step.finish_reason,
                        step.input_tokens,
                        step.output_tokens,
                        step.response_id,
                    )
                    return
                elif isinstance(step, Fail):
                    how = "raised"
                    raise step.error
                elif isinstance(step, Silence):
                    await pass_time(self.clock, step.seconds)
                else:
                    step.reached.set()
                    await step.release.wait()
        except asyncio.CancelledError:
            how = "cancelled"
            raise
        except GeneratorExit:
            how = "closed"
            raise
        finally:
            self.exits.append(how)
            self.exited_at.append(self.clock())
            self.order.append(f"exit {self.label}")


# Time


def virtual_loop_time(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Points the running loop's clock at a new `FakeClock`; `pass_time` then moves it."""
    clock = FakeClock()
    monkeypatch.setattr(asyncio.get_running_loop(), "time", clock)
    return clock


# Logs


def usage_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """(level, message) of every record the `llmgateway.usage` logger wrote, in order."""
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "llmgateway.usage"]


def usage_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [message for _, message in usage_lines(caplog)]


def router_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The ERROR lines of `llmgateway.router`."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "llmgateway.router" and r.levelno == logging.ERROR
    ]


def everything_written(caplog: pytest.LogCaptureFixture) -> str:
    """All text a capture holds: messages, arguments, formatted traces and record fields."""
    return caplog.text + "".join(
        f"{r.getMessage()}{r.args}{r.exc_text}{r.exc_info}{r.__dict__}" for r in caplog.records
    )
